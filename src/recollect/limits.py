"""Request admission and storage identifiers at the application boundary."""

from __future__ import annotations

import asyncio
import re

from starlette.responses import JSONResponse

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
MAX_MESSAGE_CHARS = 1_048_576
MAX_TITLE_CHARS = 512
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


def validate_identifier(value: str) -> str:
    # Allow older human-readable IDs, but never path/glob syntax or Windows devices.
    if not isinstance(value, str) or not re.fullmatch(IDENTIFIER_PATTERN, value):
        raise ValueError("Invalid session or turn identifier.")
    if value.upper() in _RESERVED:
        raise ValueError("Invalid session or turn identifier.")
    return value


class ResourceLimitsMiddleware:
    """Bound uploads before route side effects, and reject excess work promptly."""

    def __init__(self, app, *, max_body_bytes=2 * 1024 * 1024,
                 max_requests=64, upload_timeout=30.0, max_websockets=8) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_requests = max_requests
        self.upload_timeout = upload_timeout
        self._active = 0
        self.max_websockets = max_websockets
        self._websockets = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "websocket":
            if self._websockets >= self.max_websockets:
                await send({"type": "websocket.close", "code": 1013})
                return
            self._websockets += 1
            try:
                await self.app(scope, receive, send)
            finally:
                self._websockets -= 1
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self._active >= self.max_requests:
            await JSONResponse(
                {"detail": "Recollect is busy. Try again shortly."},
                status_code=429, headers={"Retry-After": "1"},
            )(scope, receive, send)
            return
        self._active += 1
        try:
            lengths = [v for k, v in scope.get("headers", [])
                       if k.lower() == b"content-length"]
            if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
                await JSONResponse(
                    {"detail": "Invalid Content-Length."}, status_code=400,
                )(scope, receive, send)
                return
            if lengths:
                length = lengths[0].lstrip(b"0") or b"0"
                limit = str(self.max_body_bytes).encode("ascii")
                if len(length) > len(limit) or (
                    len(length) == len(limit) and length > limit
                ):
                    await self._too_large(scope, receive, send)
                    return
            chunks = []
            total = 0
            try:
                async with asyncio.timeout(self.upload_timeout):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        total += len(chunk)
                        if total > self.max_body_bytes:
                            await self._too_large(scope, receive, send)
                            return
                        chunks.append(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await JSONResponse(
                    {"detail": "Request upload timed out."}, status_code=408,
                )(scope, receive, send)
                return
            body = b"".join(chunks)
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body,
                            "more_body": False}
                return await receive()

            await self.app(scope, bounded_receive, send)
        finally:
            self._active -= 1

    async def _too_large(self, scope, receive, send) -> None:
        await JSONResponse(
            {"detail": "Request exceeds the 2 MiB upload limit."}, status_code=413,
        )(scope, receive, send)
