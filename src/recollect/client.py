"""A localhost UI and streaming relay; model runtimes live on the desktop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from anyio import CancelScope
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from .boundary import LocalBrowserMiddleware
from .deployment import DeploymentConfig
from .limits import ResourceLimitsMiddleware

_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
_REQUEST_PRIVATE_HEADERS = {
    "host", "authorization", "origin", "forwarded", "x-forwarded-host",
    "x-forwarded-for", "x-forwarded-proto",
}


def _headers(raw: list[tuple[bytes, bytes]], *, request: bool) -> list:
    excluded = set(_HOP_HEADERS)
    for name, value in raw:
        if name.lower() == b"connection":
            excluded.update(part.strip().lower() for part in value.decode(
                "latin-1",
            ).split(","))
    if request:
        excluded.update(_REQUEST_PRIVATE_HEADERS)
    return [(name, value) for name, value in raw
            if name.decode("latin-1").lower() not in excluded]


class _DesktopResponse(Response):
    """Keep cancellation active even while waiting for desktop response headers."""

    def __init__(self, request: Request, client: httpx.AsyncClient,
                 config: DeploymentConfig, extensions: dict | None = None) -> None:
        super().__init__()
        self.request = request
        self.client = client
        self.config = config
        self.extensions = extensions or {}

    async def __call__(self, scope, receive, send) -> None:
        body_read = asyncio.Event()

        async def body() -> AsyncIterator[bytes]:
            async for chunk in self.request.stream():
                yield chunk
            body_read.set()

        async def disconnected() -> None:
            # Request streaming owns receive until the upload has been consumed.
            await body_read.wait()
            while (await receive())["type"] != "http.disconnect":
                pass

        async def forward() -> None:
            raw_path = scope.get("raw_path", scope["path"].encode("utf-8"))
            query = scope.get("query_string", b"")
            if query:
                raw_path += b"?" + query
            url = httpx.URL(self.config.desktop_url).copy_with(raw_path=raw_path)
            headers = _headers(scope["headers"], request=True)
            headers.extend([
                (b"authorization", f"Bearer {self.config.token}".encode("ascii")),
                (b"origin", str(httpx.URL(self.config.desktop_url)).rstrip(
                    "/",
                ).encode("ascii")),
            ])
            started = False
            try:
                async with self.client.stream(
                    self.request.method, url, headers=headers, content=body(),
                    extensions=self.extensions,
                ) as upstream:
                    await send({
                        "type": "http.response.start", "status": upstream.status_code,
                        "headers": _headers(upstream.headers.raw, request=False),
                    })
                    started = True
                    async for chunk in upstream.aiter_raw():
                        await send({"type": "http.response.body", "body": chunk,
                                    "more_body": True})
                    await send({"type": "http.response.body", "body": b"",
                                "more_body": False})
            except httpx.HTTPError as error:
                if started:
                    # Ending a broken stream normally would falsely complete a turn.
                    raise RuntimeError("The desktop response stream disconnected.") \
                        from None
                response = JSONResponse(
                    {"detail": "Cannot reach the Recollect desktop host. "
                     "Check its launch command and configured address."},
                    status_code=(
                        504 if isinstance(error, httpx.TimeoutException) else 502
                    ),
                )
                await response(scope, receive, send)

        forwarding = asyncio.create_task(forward())
        watching = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait(
                [forwarding, watching], return_when=asyncio.FIRST_COMPLETED,
            )
            if forwarding in done:
                await forwarding
        finally:
            for task in (forwarding, watching):
                if not task.done():
                    task.cancel()
            with CancelScope(shield=True):
                await asyncio.gather(forwarding, watching, return_exceptions=True)


async def _voice(socket: WebSocket, config: DeploymentConfig) -> None:
    origin = httpx.URL(config.desktop_url)
    url = origin.copy_with(
        scheme="wss" if origin.scheme == "https" else "ws",
        raw_path=b"/api/voice/listen",
    )
    accepted = False
    tls_options = (
        config.pairing().websocket_options() if origin.scheme == "https" else {}
    )
    try:
        async with connect(
            str(url), additional_headers={"Authorization": f"Bearer {config.token}"},
            origin=str(origin).rstrip("/"), proxy=None, compression=None,
            open_timeout=10, close_timeout=2, max_size=65_536,
            max_queue=1, write_limit=32_768,
            **tls_options,
        ) as upstream:
            await socket.accept()
            accepted = True

            async def upload() -> int:
                try:
                    while True:
                        packet = await socket.receive()
                        if packet["type"] == "websocket.disconnect":
                            return packet.get("code", 1000)
                        if packet.get("bytes") is not None:
                            if len(packet["bytes"]) > 32_000:
                                return 1009
                            await upstream.send(packet["bytes"])
                        elif packet.get("text") is not None:
                            if len(packet["text"]) > 256:
                                return 1009
                            await upstream.send(packet["text"])
                except ConnectionClosed as error:
                    return error.rcvd.code if error.rcvd is not None else 1011

            async def download() -> int:
                try:
                    while True:
                        packet = await upstream.recv()
                        if isinstance(packet, bytes):
                            await socket.send_bytes(packet)
                        else:
                            await socket.send_text(packet)
                except ConnectionClosed as error:
                    return error.rcvd.code if error.rcvd is not None else 1011

            uploading = asyncio.create_task(upload())
            downloading = asyncio.create_task(download())
            code = 1000
            try:
                done, _ = await asyncio.wait(
                    [uploading, downloading], return_when=asyncio.FIRST_COMPLETED,
                )
                codes = [task.result() for task in done]
                code = codes[0] if codes[0] not in {1005, 1006, 1015} else 1011
                if socket.client_state != WebSocketState.DISCONNECTED:
                    await socket.close(code=code)
            finally:
                for task in (uploading, downloading):
                    if not task.done():
                        task.cancel()
                # ASGI cancellation may remain active at every await; finish
                # cancelling both relays before releasing their desktop socket.
                with CancelScope(shield=True):
                    await asyncio.gather(uploading, downloading, return_exceptions=True)
                    await upstream.close(code=code)
    except (OSError, TimeoutError, InvalidHandshake):
        if not accepted:
            await socket.accept()
        await socket.send_json({"type": "error", "message":
                                "Cannot connect to desktop voice. Check the host "
                                "address, launch command, and client token."})
        await socket.close(code=1011)
    except WebSocketDisconnect:
        pass


def create_app(config: DeploymentConfig | None = None) -> FastAPI:
    config = config or DeploymentConfig.from_env()
    if config.mode != "client":
        raise ValueError("The UI client requires client deployment mode.")
    dist = config.ui_dir or Path(__file__).resolve().parents[2] / "ui" / "dist"
    if not (dist / "index.html").is_file():
        raise ValueError(
            "The built UI is missing. Build ui/ or set RECOLLECT_UI_DIR "
            "to its built directory before launching the client."
        )
    credentials = config.pairing()
    tls_context = credentials.ssl_context()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10),
            trust_env=False, follow_redirects=False,
            verify=tls_context,
        ) as desktop:
            try:
                async with asyncio.timeout(10):
                    response = await desktop.get(
                        f"{config.desktop_url}/api/deployment",
                        headers={"Authorization": f"Bearer {config.token}"},
                        timeout=5,
                        extensions=credentials.request_extensions(),
                    )
                    if response.status_code != 200 or response.json() != {
                        "mode": "host",
                    }:
                        raise ValueError("Desktop host identity was not confirmed.")
            except (httpx.HTTPError, TimeoutError, ValueError):
                raise RuntimeError(
                    "Cannot verify the Recollect desktop host. Check its address, "
                    "client token, and that it was launched in host mode."
                ) from None
            app.state.desktop = desktop
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(ResourceLimitsMiddleware)
    app.add_middleware(LocalBrowserMiddleware)

    @app.get("/api/deployment")
    async def deployment() -> dict:
        return {"mode": "client", "desktop_url": config.desktop_url}

    @app.api_route("/api/{path:path}", methods=[
        "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS",
    ])
    @app.api_route("/v1/{path:path}", methods=[
        "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS",
    ])
    async def relay(request: Request, path: str) -> Response:
        return _DesktopResponse(
            request, app.state.desktop, config, credentials.request_extensions(),
        )

    @app.websocket("/api/voice/listen")
    async def listen(socket: WebSocket) -> None:
        await _voice(socket, config)

    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    async def index() -> Response:
        return FileResponse(dist / "index.html")

    return app
