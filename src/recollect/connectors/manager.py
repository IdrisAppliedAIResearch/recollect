"""The host-side connector registry: lookup, consent, credentials, status.

Only :meth:`ConnectorManager.connect` talks to a provider's authorization
endpoints, and only after the user said connect. A granted connector refreshes
its access token on demand and caches it exactly like the connected-account
service; the payload served to workers never contains the refresh token or
the OAuth client secret.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import httpx

from ..connections import KEY_VARIABLE, URL_VARIABLE
from .base import (
    Connector,
    ConnectorStore,
    NotConnected,
    UnknownConnector,
    connector_store,
)
from .google_calendar import GoogleCalendar
from .oauth import (
    CallbackCapture,
    authorization_url,
    exchange_code,
    new_pkce,
    new_state,
    open_in_browser,
    refresh_access_token,
)

#: Connectors this build ships with worker tools. The official MCP registry
#: lookup (issue #28) will propose more; only these can be used after a
#: connect until the implementation agent writes the glue for new ones.
CONNECTORS: tuple[Connector, ...] = (GoogleCalendar(),)

#: How long a browser tab may take to come back with the user's answer. This
#: is human pacing, not a limit on agent work: an unfinished sign-in fails
#: the connect alone, and the request stays blocked and askable.
CALLBACK_TIMEOUT = 300.0

GUIDE = f"""External services connect through Recollect. Worker tools reach a
connected service through the local connection service, never with stored
credentials:
- Read the environment variables {URL_VARIABLE} and {KEY_VARIABLE} when the
  tool runs, not at import time.
- GET {{{URL_VARIABLE}}}/connectors with the header
  "Authorization: Bearer {{{KEY_VARIABLE}}}" lists the connected services
  (connector_id, name, scope). GET {{{URL_VARIABLE}}}/connectors/{{connector_id}}
  returns its short-lived access: {{"access_token": "...", "expires_in": 3599,
  "scope": "...", ...connector fields such as calendar_id}}.
- If a service is not listed, or the service errors, return a clear error that
  the connection is unavailable. Never invent a result.
- Never return, print or log an access token or the service key.
- Checks have no network: fake the connection service in tests."""


async def _open(url: str) -> None:
    await asyncio.to_thread(open_in_browser, url)


class ConnectorManager:
    """One object owns every connector: definitions, files, and token cache."""

    def __init__(self, store: ConnectorStore | None = None, *,
                 connectors: tuple[Connector, ...] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 browser=None,
                 callback_timeout: float = CALLBACK_TIMEOUT) -> None:
        self._store = store or ConnectorStore(connector_store())
        self._connectors = tuple(connectors if connectors is not None
                                 else CONNECTORS)
        self._client = httpx.AsyncClient(timeout=30, trust_env=False,
                                         transport=transport)
        self._browser = browser or _open
        self._callback_timeout = callback_timeout
        self._tokens: dict[str, tuple[str, float, str]] = {}
        self._lock = asyncio.Lock()

    def __contains__(self, connector_id: str) -> bool:
        return any(c.id == connector_id for c in self._connectors)

    def get(self, connector_id: str) -> Connector:
        for connector in self._connectors:
            if connector.id == connector_id:
                return connector
        raise UnknownConnector(f"No connector named {connector_id!r}.")

    # -- lookup ------------------------------------------------------------

    def find(self, gap: dict) -> Connector | None:
        """A connectable, not-yet-connected connector for this capability gap.

        ``None`` means fall through to the build proposal: an unconfigured
        connector cannot be connected, and a connected one needs no offer.
        """
        text = " ".join(str(gap.get(key) or "") for key in
                        ("missing_capability", "modification_request"))
        connected = set(self._store.connected())
        for connector in self._connectors:
            if connector.id in connected:
                continue
            if self._store.connectable(connector.id) and connector.matches(text):
                return connector
        return None

    def status(self) -> list[dict]:
        return [self.entry(connector.id) for connector in self._connectors]

    def entry(self, connector_id: str) -> dict:
        connector = self.get(connector_id)
        grant = self._store.grant(connector_id)
        return {"connector_id": connector.id, "name": connector.name,
                "description": connector.description,
                "scopes": list(connector.scopes),
                "tools": list(connector.tools),
                "connectable": self._store.connectable(connector_id),
                "connected": grant is not None,
                "connected_at": (grant or {}).get("connected_at")}

    def connected(self) -> list[str]:
        return [connector.id for connector in self._connectors
                if connector.id in set(self._store.connected())]

    # -- consent and credentials --------------------------------------------

    async def connect(self, connector_id: str, *, open_browser: bool = True) \
            -> dict:
        """Run the consent flow once. The caller invokes this only on the
        user's explicit yes; the provider's own page collects the Allow."""
        connector = self.get(connector_id)
        client = self._store.client(connector_id) or {}
        if not (client.get("client_id") and client.get("client_secret")):
            raise ValueError(f"{connector.name} has no OAuth client configured; "
                             f"register one at {connector_id}.client.json first.")
        if self._store.grant(connector_id) is not None:
            raise ValueError(f"{connector.name} is already connected.")
        port = client.get("redirect_port") or connector.redirect_port
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ValueError(f"{connector.name}'s client.json has a "
                             "non-numeric redirect_port; fix it and ask me "
                             "to connect again.") from None
        verifier, challenge = new_pkce()
        state = new_state()
        capture = CallbackCapture(port, state)
        await capture.start()
        redirect_uri = capture.redirect_uri
        url = authorization_url(auth_url=connector.auth_url,
                                client_id=client["client_id"],
                                redirect_uri=redirect_uri,
                                scope=" ".join(connector.scopes), state=state,
                                code_challenge=challenge,
                                auth_params=connector.auth_params)
        try:
            if open_browser:
                await self._browser(url)
            try:
                params = await capture.wait(self._callback_timeout)
            except TimeoutError:
                raise RuntimeError(
                    f"The {connector.name} sign-in was not completed in time."
                ) from None
        finally:
            await capture.close()
        if params.get("state") != state:
            raise RuntimeError("The sign-in response did not match this request.")
        if "code" not in params:
            reason = str(params.get("error") or "denied")
            raise RuntimeError(f"The {connector.name} sign-in did not complete "
                               f"({reason}).")
        tokens = await exchange_code(
            connector.token_url, client_id=client["client_id"],
            client_secret=client["client_secret"], code=params["code"],
            redirect_uri=redirect_uri, code_verifier=verifier,
            client=self._client)
        if not tokens.get("access_token"):
            raise RuntimeError("The provider returned no access token.")
        if not tokens.get("refresh_token"):
            raise RuntimeError("The provider returned no refresh token, so the "
                               "connection could not outlive this session.")
        scope = tokens.get("scope", " ".join(connector.scopes))
        self._store.save(connector_id, {
            "refresh_token": tokens["refresh_token"], "scope": scope,
            "connected_at": datetime.now(UTC).isoformat()})
        self._tokens[connector_id] = (tokens["access_token"],
                                      time.monotonic()
                                      + int(tokens.get("expires_in", 3600)),
                                      scope)
        return self.entry(connector_id)

    async def _access(self, connector_id: str) -> tuple[str, int, str]:
        cached = self._tokens.get(connector_id)
        if cached and cached[1] - time.monotonic() > 120:
            return cached[0], int(cached[1] - time.monotonic()), cached[2]
        connector = self.get(connector_id)
        grant = self._store.grant(connector_id)
        client = self._store.client(connector_id)
        if grant is None or not client:
            raise NotConnected(f"{connector.name} is not connected.")
        refreshed = await refresh_access_token(
            connector.token_url, client_id=client["client_id"],
            client_secret=client["client_secret"],
            refresh_token=grant["refresh_token"], client=self._client)
        token = refreshed["access_token"]
        expires = int(refreshed.get("expires_in", 3600))
        scope = refreshed.get("scope", grant.get("scope", ""))
        self._tokens[connector_id] = (token, time.monotonic() + expires, scope)
        return token, expires, scope

    async def connection(self, connector_id: str) -> dict:
        """Short-lived access for worker tools: never the refresh token."""
        self.get(connector_id)
        if connector_id not in set(self._store.connected()):
            raise NotConnected(f"{self.get(connector_id).name} is not connected.")
        async with self._lock:
            # Only the refresh is serialized; the provider lookup for one
            # worker must not hold up another worker's access.
            token, expires, scope = await self._access(connector_id)
        extras = await self.get(connector_id).connection(token, self._client)
        return {"access_token": token, "expires_in": expires, "scope": scope,
                **extras}

    def disconnect(self, connector_id: str) -> bool:
        self._tokens.pop(connector_id, None)
        return self._store.forget(connector_id)

    async def close(self) -> None:
        await self._client.aclose()
