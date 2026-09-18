"""OAuth 2.0 authorization-code + PKCE, consented entirely on the provider's page.

The sign-in page is opened by the caller (and only after the user asked), and
this module never renders it: it builds the authorization URL, waits on the
registered loopback redirect for the one code+state the user's browser sends
back, then exchanges the code. The page shown at the end is a dead-end
"return to Recollect" notice; there is no secret and no session in it.

A custom redirect page and the exchanged tokens never reach a transcript:
callers pass them straight to the credential store. Tests fake the token
endpoint with an httpx transport and post the callback themselves.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

_CALLBACK_PATH = "/callback"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge) for the S256 method."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def new_state() -> str:
    return _b64url(secrets.token_bytes(24))


class CallbackCapture:
    """A loopback HTTP server that answers one OAuth redirect, then closes.

    Binds 127.0.0.1 on the client's registered port so the provider's
    redirect (which is fixed to that port) reaches us. Only a redirect whose
    ``state`` matches this flow's settles it; a stray or forged request is
    refused and the listener keeps waiting for the real answer.
    """

    def __init__(self, port: int, expected_state: str,
                 *, host: str = "127.0.0.1") -> None:
        self._host = host
        self._requested_port = port
        self._expected_state = expected_state
        self._server: asyncio.Server | None = None
        self._result: asyncio.Future | None = None

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("The callback listener is not running.")
        return self._server.sockets[0].getsockname()[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://{self._host}:{self.port}{_CALLBACK_PATH}"

    async def start(self) -> None:
        self._result = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(
            self._handle, self._host, self._requested_port)

    async def wait(self, timeout: float) -> dict:
        if self._result is None:
            raise RuntimeError("The callback listener was never started.")
        return await asyncio.wait_for(self._result, timeout)

    async def _handle(self, reader, writer) -> None:
        try:
            request = await asyncio.wait_for(self._read_head(reader), 10)
            params = self._parse(request)
            accepted = (params is not None
                        and params.get("state") == self._expected_state
                        and self._result is not None
                        and not self._result.done())
            body = ("<html><body>Sign-in finished. Return to Recollect.</body></html>"
                    if accepted else "<html><body>Not found.</body></html>")
            head = ("HTTP/1.1 " + ("200 OK" if accepted else "404 Not Found")
                    + "\r\nContent-Type: text/html\r\nContent-Length: "
                    + str(len(body)) + "\r\nConnection: close\r\n\r\n")
            writer.write((head + body).encode())
            await writer.drain()
            if accepted:
                self._result.set_result(params)
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _read_head(reader) -> str:
        lines = []
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            lines.append(line.decode("latin-1"))
        return "".join(lines)

    @staticmethod
    def _parse(request_head: str) -> dict | None:
        first = request_head.split("\r\n", 1)[0]
        parts = first.split()
        if len(parts) < 2 or parts[0] not in ("GET", "get"):
            return None
        target = parts[1]
        parsed = urlparse(target)
        if parsed.path != _CALLBACK_PATH:
            return None
        query = parse_qs(parsed.query)
        return {key: values[0] for key, values in query.items()}

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


def authorization_url(*, auth_url: str, client_id: str, redirect_uri: str,
                      scope: str, state: str, code_challenge: str,
                      auth_params: dict | None = None) -> str:
    query = urlencode({
        "response_type": "code", "client_id": client_id,
        "redirect_uri": redirect_uri, "scope": scope, "state": state,
        "code_challenge": code_challenge, "code_challenge_method": "S256",
        **(auth_params or {}),
    })
    return f"{auth_url}?{query}"


async def exchange_code(token_url: str, *, client_id: str, client_secret: str,
                        code: str, redirect_uri: str, code_verifier: str,
                        client: httpx.AsyncClient) -> dict:
    response = await client.post(token_url, data={
        "grant_type": "authorization_code", "code": code,
        "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "code_verifier": code_verifier})
    if response.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed (HTTP {response.status_code}).")
    return response.json()


async def refresh_access_token(token_url: str, *, client_id: str,
                               client_secret: str, refresh_token: str,
                               client: httpx.AsyncClient) -> dict:
    response = await client.post(token_url, data={
        "grant_type": "refresh_token", "client_id": client_id,
        "client_secret": client_secret, "refresh_token": refresh_token})
    if response.status_code != 200:
        raise RuntimeError(
            f"Token refresh failed (HTTP {response.status_code}).")
    return response.json()


def open_in_browser(url: str) -> None:
    """Open the provider's consent page in the user's browser, on request.

    On Windows ``start`` is the shell verb; it never launches without the
    user's action, so the "sign-in page never pops up on its own" rule holds.
    """
    import webbrowser

    webbrowser.open(url)
