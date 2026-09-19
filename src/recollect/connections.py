"""Connected accounts for worker sandboxes, without handing them credentials.

The user authorizes an account once, outside the app; its refresh token stays in
a private directory outside the repository. This loopback service turns it into
short-lived access tokens for the worker's tool process only. The worker model
has no shell and cannot read outside its workspace, so neither the service key
nor any token reaches a transcript unless a tool returns it, which the guide
forbids. Implementation agents learn the convention but get no live access.

Google: ``%LOCALAPPDATA%/recollect/selfmod-google`` holds ``worker.json``
(``calendar.events``) and ``verifier.json`` (``calendar.readonly``), each with
a refresh token and the path of the OAuth client JSON. The read-only token finds
the test calendar by name once; the worker token is refreshed on demand.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import socket
import time
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
DEFAULT_CALENDAR = "recollect-selfmod-test"
URL_VARIABLE = "RECOLLECT_CONNECTIONS_URL"
KEY_VARIABLE = "RECOLLECT_CONNECTIONS_TOKEN"

GUIDE = f"""The user has connected a Google account. Worker tools reach it through a
local connection service, never with stored credentials:
- Read the environment variables {URL_VARIABLE} and {KEY_VARIABLE} when the tool
  runs, not at import time.
- GET {{{URL_VARIABLE}}}/google with the header
  "Authorization: Bearer {{{KEY_VARIABLE}}}" returns JSON:
  {{"access_token": "...", "expires_in": 3599, "scope": "https://www.googleapis.com/auth/calendar.events",
   "calendar_id": "...", "calendar_time_zone": "America/Chicago"}}
- Call Google APIs with "Authorization: Bearer <access_token>". The scope allows
  reading and writing events on that calendar only; use calendar_id for it.
- If either variable is missing, or the service returns an error, return a clear
  error that the Google connection is unavailable. Never invent a result.
- Never return, print or log the access token or the service key.
- Checks have no network: fake the connection service and Google in tests."""


def google_store() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return root / "recollect" / "selfmod-google"


class GoogleAccount:
    """Refreshes the worker's access token and finds the test calendar."""

    def __init__(self, store: Path, *, calendar: str = DEFAULT_CALENDAR,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._store = Path(store)
        self._calendar = calendar
        self._client = httpx.AsyncClient(timeout=30, trust_env=False,
                                         transport=transport)
        self._tokens: dict[str, tuple[str, float, str]] = {}
        self._calendar_info: tuple[str, str] | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def available(store: Path) -> bool:
        return all((Path(store) / f"{role}.json").is_file()
                   for role in ("worker", "verifier"))

    def _grant(self, role: str) -> dict:
        grant = json.loads((self._store / f"{role}.json").read_text(encoding="utf-8"))
        client = json.loads(Path(grant["client_secret_path"]).read_text(
            encoding="utf-8"))["installed"]
        return {"client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "refresh_token": grant["refresh_token"],
                "scope": grant["scope"]}

    async def _access_token(self, role: str) -> tuple[str, int, str]:
        cached = self._tokens.get(role)
        if cached and cached[1] - time.monotonic() > 120:
            return cached[0], int(cached[1] - time.monotonic()), cached[2]
        grant = await asyncio.to_thread(self._grant, role)
        response = await self._client.post(TOKEN_URL, data={
            "client_id": grant["client_id"], "client_secret": grant["client_secret"],
            "refresh_token": grant["refresh_token"], "grant_type": "refresh_token"})
        if response.status_code != 200:
            raise RuntimeError(f"Google refused the {role} token refresh "
                               f"(HTTP {response.status_code}).")
        value = response.json()
        expires = int(value.get("expires_in", 3600))
        self._tokens[role] = (value["access_token"], time.monotonic() + expires,
                              value.get("scope", grant["scope"]))
        return value["access_token"], expires, value.get("scope", grant["scope"])

    async def calendar(self) -> tuple[str, str]:
        """(calendar ID, time zone) of the test calendar, found by its name."""
        if self._calendar_info is None:
            token, _, _ = await self._access_token("verifier")
            response = await self._client.get(
                CALENDAR_LIST_URL, headers={"Authorization": f"Bearer {token}"},
                params={"minAccessRole": "writer"})
            if response.status_code != 200:
                raise RuntimeError(
                    f"Could not list calendars (HTTP {response.status_code}).")
            matches = [item for item in response.json().get("items", [])
                       if item.get("summary") == self._calendar]
            if len(matches) != 1:
                raise RuntimeError(f"Expected one calendar named {self._calendar!r}, "
                                   f"found {len(matches)}.")
            self._calendar_info = (matches[0]["id"], matches[0].get("timeZone", ""))
        return self._calendar_info

    async def connection(self) -> dict:
        async with self._lock:
            token, expires, scope = await self._access_token("worker")
            calendar_id, time_zone = await self.calendar()
        return {"access_token": token, "expires_in": expires, "scope": scope,
                "calendar_id": calendar_id, "calendar_time_zone": time_zone}

    async def close(self) -> None:
        await self._client.aclose()


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        # The main Recollect server owns process signals.
        yield


class ConnectionService:
    """Serves connected-account and connector access to holders of its key."""

    def __init__(self, google: GoogleAccount | None = None, connectors=None,
                 scheduler=None, store=None, channels=None) -> None:
        self.google = google
        #: A recollect.connectors.ConnectorManager, or None while none exists.
        #: Duck-typed (``connection``/``status``) to keep this module a leaf.
        self.connectors = connectors
        #: The host's recollect.scheduling.Scheduler, or None while none runs.
        self.scheduler = scheduler
        #: The host's recollect.agents_store.AgentStore (durable state seam),
        #: or None while it is not wired. Duck-typed, leaf again.
        self.store = store
        #: The host's recollect.channels.ChannelHub (off-device send seam),
        #: or None while it is not wired.
        self.channels = channels
        self.key = secrets.token_urlsafe(32)
        self.base_url = ""
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self._server: _Server | None = None
        self._worker: asyncio.Task | None = None

        def require_key(request: Request) -> None:
            if not secrets.compare_digest(request.headers.get("authorization", ""),
                                          f"Bearer {self.key}"):
                raise HTTPException(401, "The connection service requires its key.")

        @self.app.get("/google")
        async def google_connection(request: Request):
            require_key(request)
            if self.google is None:
                raise HTTPException(404, "No Google account is connected.")
            try:
                return await self.google.connection()
            except (RuntimeError, OSError, KeyError, ValueError,
                    httpx.HTTPError) as error:
                raise HTTPException(503, f"Google connection unavailable: "
                                         f"{type(error).__name__}: {error}") from None

        @self.app.get("/connectors")
        async def connectors_list(request: Request):
            require_key(request)
            if self.connectors is None:
                return []
            try:
                entries = self.connectors.status()
            except (RuntimeError, OSError, KeyError) as error:
                raise HTTPException(503, f"connectors unavailable: "
                                         f"{type(error).__name__}: {error}") from None
            return [entry for entry in entries if entry["connected"]]

        @self.app.get("/connectors/{connector_id}")
        async def connector_connection(connector_id: str, request: Request):
            require_key(request)
            if self.connectors is None:
                raise HTTPException(404, "No connectors are available.")
            try:
                return await self.connectors.connection(connector_id)
            except ValueError as error:  # unknown or not connected
                raise HTTPException(404, str(error)) from None
            except (RuntimeError, OSError, KeyError, httpx.HTTPError) as error:
                raise HTTPException(503, f"{connector_id} connection unavailable: "
                                         f"{type(error).__name__}: {error}") from None

        # The generic "do this at time T" primitive (recollect.scheduling).
        # The payload travels untouched: what a due job means is the host
        # deliverer's contract, never this relay's.
        @self.app.post("/schedules")
        async def schedule_job(request: Request):
            require_key(request)
            if self.scheduler is None:
                raise HTTPException(404, "No scheduler is available.")
            try:
                body = await request.json()
                job = self.scheduler.schedule(
                    datetime.fromisoformat(body["due_at"]), body["payload"])
            except (json.JSONDecodeError, KeyError, TypeError,
                    ValueError) as error:
                raise HTTPException(
                    400, f"A job needs a timezone-aware due_at and an object "
                         f"payload: {error}") from None
            return job

        @self.app.get("/schedules")
        async def schedules_list(request: Request):
            require_key(request)
            if self.scheduler is None:
                return []
            return self.scheduler.pending()

        @self.app.delete("/schedules/{job_id}")
        async def schedule_cancel(job_id: str, request: Request):
            require_key(request)
            if self.scheduler is None:
                raise HTTPException(404, "No scheduler is available.")
            try:
                return self.scheduler.cancel(job_id)
            except KeyError as error:
                raise HTTPException(404, str(error)) from None
            except ValueError as error:  # already fired or canceled
                raise HTTPException(409, str(error)) from None

        # The durable-state seam (recollect.agents_store): namespaced JSON
        # for worker-built capabilities, over the same key. Values travel
        # untouched — their meaning belongs to the capability that wrote
        # them, exactly as job payloads belong to the deliverer.
        @self.app.get("/agents")
        async def agents_namespaces(request: Request):
            require_key(request)
            if self.store is None:
                return []
            return await asyncio.to_thread(self.store.namespaces)

        @self.app.get("/agents/{namespace}/entries")
        async def agents_entries(namespace: str, request: Request):
            require_key(request)
            if self.store is None:
                return []
            try:
                return await asyncio.to_thread(self.store.list, namespace)
            except ValueError as error:
                raise HTTPException(400, str(error)) from None

        @self.app.get("/agents/{namespace}/entries/{key}")
        async def agent_entry(namespace: str, key: str, request: Request):
            require_key(request)
            if self.store is None:
                raise HTTPException(404, "No agent store is available.")
            try:
                return {"data": await asyncio.to_thread(
                    self.store.get, namespace, key)}
            except KeyError as error:
                raise HTTPException(404, f"no such entry: {error}") from None
            except ValueError as error:
                raise HTTPException(400, str(error)) from None

        @self.app.put("/agents/{namespace}/entries/{key}")
        async def agent_entry_put(namespace: str, key: str, request: Request):
            require_key(request)
            if self.store is None:
                raise HTTPException(404, "No agent store is available.")
            try:
                body = await request.json()
                return await asyncio.to_thread(
                    self.store.put, namespace, key, body["data"])
            except (json.JSONDecodeError, KeyError, TypeError,
                    ValueError) as error:
                raise HTTPException(
                    400, f"an entry needs an object with a JSON 'data' "
                         f"value: {error}") from None

        @self.app.delete("/agents/{namespace}/entries/{key}")
        async def agent_entry_delete(namespace: str, key: str,
                                     request: Request):
            require_key(request)
            if self.store is None:
                raise HTTPException(404, "No agent store is available.")
            try:
                await asyncio.to_thread(self.store.delete, namespace, key)
            except KeyError as error:
                raise HTTPException(404, f"no such entry: {error}") from None
            except ValueError as error:
                raise HTTPException(400, str(error)) from None
            return {"deleted": True}

        # The off-device send seam (recollect.channels): what exists, and a
        # test message the user can verify. Delivery itself happens at job
        # time in the host deliverer, never from the sandbox; the listing
        # never carries config values — a topic URL is a credential.
        @self.app.get("/channels")
        async def channels_list(request: Request):
            require_key(request)
            if self.channels is None:
                return []
            return await asyncio.to_thread(self.channels.configured)

        @self.app.post("/channels/{name}/test")
        async def channel_test(name: str, request: Request):
            require_key(request)
            if self.channels is None:
                raise HTTPException(404, "No channels are available.")
            try:
                body = await request.json()
                text = body["text"]
            except (json.JSONDecodeError, KeyError, TypeError,
                    ValueError) as error:
                raise HTTPException(400, f"a test needs text: {error}") from None
            if not isinstance(text, str) or not text.strip():
                raise HTTPException(400, "a test needs text")
            if len(text.encode("utf-8")) > 4096:
                raise HTTPException(400, "a test message is limited to "
                                         "4096 bytes")
            try:
                await self.channels.send(name, text.strip())
            except KeyError as error:
                raise HTTPException(404, str(error)) from None
            except (ValueError, RuntimeError) as error:
                raise HTTPException(503, f"{name} test failed: {error}") \
                    from None
            return {"sent": True}

    async def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Docker must reach this port; the key exposes only connected accounts.
        listener.bind(("0.0.0.0", 0))
        listener.listen(16)
        self.base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        self._server = _Server(uvicorn.Config(self.app, access_log=False,
                                              log_level="error", lifespan="off"))
        self._worker = asyncio.create_task(self._server.serve(sockets=[listener]))
        for _ in range(100):
            if self._worker.done():
                self._worker.result()
                raise RuntimeError("The connection service stopped during startup.")
            if self._server.started:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("The connection service did not start.")

    async def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._worker is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._worker), 5)
        if self.scheduler is not None:
            await self.scheduler.close()
        if self.google is not None:
            await self.google.close()
        if self.connectors is not None:
            await self.connectors.close()
