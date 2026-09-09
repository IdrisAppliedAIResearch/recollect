"""Browser checks shared by the two loopback UI deployments."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

DEVELOPMENT_ORIGINS = frozenset({
    "http://127.0.0.1:5173", "http://localhost:5173",
})


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_request_allowed(scope: dict, *, allow_development: bool = False) -> bool:
    headers = Headers(scope=scope)
    hosts = headers.getlist("host")
    if len(hosts) != 1:
        return False
    host = hosts[0]
    try:
        parsed = urlsplit(f"http://{host}")
        if (not parsed.hostname or not is_loopback(parsed.hostname)
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or any(char.isspace() for char in host)):
            return False
        _ = parsed.port
    except ValueError:
        return False
    origins = headers.getlist("origin")
    if not origins:
        return headers.get("sec-fetch-site") not in {"cross-site", "same-site"}
    if len(origins) != 1:
        return False
    if allow_development and origins[0] in DEVELOPMENT_ORIGINS:
        return True
    scheme = "https" if scope.get("scheme") in {"https", "wss"} else "http"
    return origins[0].lower() == f"{scheme}://{host}".lower()


class LocalBrowserMiddleware:
    def __init__(self, app, *, allow_development: bool = False) -> None:
        self.app = app
        self.allow_development = allow_development

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in {"http", "websocket"} and not local_request_allowed(
            scope, allow_development=self.allow_development,
        ):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse(
                    {"detail": "Open Recollect on localhost; "
                     "cross-origin requests are not allowed."}, status_code=403,
                )(scope, receive, send)
            return
        await self.app(scope, receive, send)
