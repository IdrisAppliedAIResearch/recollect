"""The Surface relay preserves streams and owns no model runtime."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from recollect import client
from recollect.deployment import DeploymentConfig

TOKEN = "test-desktop-token-" + "x" * 32
LOCAL_URL = "http://localhost"
VOICE_URL = "ws://localhost/api/voice/listen"


@pytest.fixture
def deployment(tmp_path):
    (tmp_path / "index.html").write_text("<h1>Recollect client</h1>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("client assets", encoding="utf-8")
    return DeploymentConfig(
        mode="client", desktop_url="https://desktop.test:8080", token=TOKEN,
        ui_dir=tmp_path,
    )


def mock_desktop(monkeypatch, handler, *, startup=None):
    calls = []
    clients = []
    settings = []
    original = httpx.AsyncClient

    async def dispatch(request):
        calls.append(request)
        if request.url.path == "/api/deployment":
            if startup is not None:
                return await startup(request)
            return httpx.Response(200, json={"mode": "host"})
        return await handler(request)

    def factory(**kwargs):
        settings.append(kwargs)
        result = original(transport=httpx.MockTransport(dispatch), **kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(client.httpx, "AsyncClient", factory)
    return calls, clients, settings


async def empty_response(request):
    return httpx.Response(200, stream=httpx.ByteStream(b"ok"))


def test_client_serves_ui_without_dialing_models(deployment, monkeypatch):
    calls, clients, settings = mock_desktop(monkeypatch, empty_response)
    with TestClient(client.create_app(deployment), base_url=LOCAL_URL) as local:
        assert local.get("/").text == "<h1>Recollect client</h1>"
        assert local.get("/assets/app.js").text == "client assets"
        assert local.get("/api/deployment").json() == {
            "mode": "client", "desktop_url": deployment.desktop_url,
        }
        assert local.get("/docs").status_code == 404
    assert len(calls) == 1
    assert calls[0].url.path == "/api/deployment"
    assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert settings[0]["trust_env"] is False
    assert settings[0]["follow_redirects"] is False
    assert settings[0]["timeout"].read is None
    assert settings[0]["timeout"].connect == 10
    assert clients[0].is_closed


@pytest.mark.parametrize("status, payload", [
    (401, {"detail": "unauthorized"}), (200, {"mode": "standalone"}),
    (200, {"mode": "client"}), (503, {"detail": "unavailable"}),
])
def test_startup_requires_authenticated_host(
    deployment, monkeypatch, status, payload,
):
    async def startup(request):
        return httpx.Response(status, json=payload)

    _, clients, _ = mock_desktop(monkeypatch, empty_response, startup=startup)
    with (
        pytest.raises(RuntimeError, match="Cannot verify") as failure,
        TestClient(client.create_app(deployment), base_url=LOCAL_URL),
    ):
        pytest.fail("An invalid desktop must prevent client startup")
    assert TOKEN not in str(failure.value)
    assert clients[0].is_closed


def test_missing_ui_fails_before_upstream_connection(deployment):
    with pytest.raises(ValueError, match="built UI is missing"):
        client.create_app(replace(deployment, ui_dir=deployment.ui_dir / "missing"))


@pytest.mark.parametrize("path", ["/api/chat", "/v1/chat/completions"])
def test_relay_preserves_request_and_stream_headers(deployment, monkeypatch, path):
    async def upstream(request):
        return httpx.Response(
            202, headers={
                "Content-Type": "application/x-ndjson", "X-Trace": "yes",
                "Connection": "x-private", "X-Private": "remove",
            }, stream=httpx.ByteStream(b'{"type":"done"}\n'),
        )

    calls, _, _ = mock_desktop(monkeypatch, upstream)
    with TestClient(client.create_app(deployment), base_url=LOCAL_URL) as local:
        response = local.post(path + "?q=%2F%3F", content=b"raw payload", headers={
            "Authorization": "Bearer browser-value", "Origin": "http://localhost",
            "Connection": "keep-alive, x-private", "X-Private": "remove",
            "X-Forwarded-Host": "attacker.invalid", "X-Request": "preserve",
            "Content-Type": "application/json",
        })
    request = calls[-1]
    assert request.url == "https://desktop.test:8080" + path + "?q=%2F%3F"
    assert request.content == b"raw payload"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["origin"] == deployment.desktop_url
    assert request.headers["host"] == "desktop.test:8080"
    assert request.headers["x-request"] == "preserve"
    assert "x-private" not in request.headers
    assert "x-forwarded-host" not in request.headers
    assert response.status_code == 202
    assert response.content == b'{"type":"done"}\n'
    assert response.headers["content-type"] == "application/x-ndjson"
    assert response.headers["x-trace"] == "yes"
    assert "x-private" not in response.headers
    assert "connection" not in response.headers
    assert TOKEN not in response.text


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.invalid"}, {"Host": "192.168.1.2"},
    {"Host": "localhost@attacker.invalid"}, {"Host": "localhost:bad"},
    {"Origin": "http://attacker.invalid"}, {"Origin": "null"},
    {"Origin": "http://localhost:9000"}, {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_foreign_requests_refused_before_dial(deployment, monkeypatch, headers):
    calls, _, _ = mock_desktop(monkeypatch, empty_response)
    with TestClient(client.create_app(deployment), base_url=LOCAL_URL) as local:
        assert local.post("/api/chat", headers=headers).status_code == 403
    assert len(calls) == 1


def test_redirect_is_returned_without_following_it(deployment, monkeypatch):
    async def redirect(request):
        return httpx.Response(307, headers={"Location": "http://other.invalid/api/chat"},
                              stream=httpx.ByteStream(b""))

    calls, _, _ = mock_desktop(monkeypatch, redirect)
    with TestClient(client.create_app(deployment), base_url=LOCAL_URL) as local:
        response = local.post("/api/chat", follow_redirects=False)
    assert response.status_code == 307
    assert len(calls) == 2


def test_upstream_errors_are_sanitized_and_not_retried(deployment, monkeypatch):
    async def failed(request):
        raise httpx.ConnectError(f"private diagnostic {TOKEN}", request=request)

    calls, _, _ = mock_desktop(monkeypatch, failed)
    with TestClient(client.create_app(deployment), base_url=LOCAL_URL) as local:
        response = local.post("/api/chat")
    assert response.status_code == 502
    assert "desktop host" in response.text
    assert TOKEN not in response.text
    assert len(calls) == 2


def http_scope(path="/api/chat"):
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 80),
    }


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closed = False
        self.next_chunk = asyncio.Event()

    async def __aiter__(self):
        self.reads += 1
        yield b"event: first\n\n"
        self.reads += 1
        await self.next_chunk.wait()
        yield b"event: done\n\n"

    async def aclose(self):
        self.closed = True


async def test_stream_backpressure_and_disconnect_close_upstream(
    deployment, monkeypatch,
):
    stream = TrackedStream()

    async def upstream(request):
        return httpx.Response(200, stream=stream)

    mock_desktop(monkeypatch, upstream)
    app = client.create_app(deployment)
    received = asyncio.Queue()
    received.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    first_chunk = asyncio.Event()
    release_send = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()
            await release_send.wait()

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(http_scope(), received.get, send))
        try:
            await asyncio.wait_for(first_chunk.wait(), 1)
            assert stream.reads == 1
            received.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert stream.closed
    assert stream.reads == 1


async def test_disconnect_cancels_desktop_before_headers(deployment, monkeypatch):
    waiting = asyncio.Event()
    cancelled = asyncio.Event()

    async def upstream(request):
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    mock_desktop(monkeypatch, upstream)
    app = client.create_app(deployment)
    received = asyncio.Queue()
    received.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    sent = []

    async def send(message):
        sent.append(message)

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(http_scope(), received.get, send))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            received.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set()
    assert not sent


async def test_send_failure_closes_desktop_stream(deployment, monkeypatch):
    stream = TrackedStream()

    async def upstream(request):
        return httpx.Response(200, stream=stream)

    mock_desktop(monkeypatch, upstream)
    app = client.create_app(deployment)
    received = asyncio.Queue()
    received.put_nowait({"type": "http.request", "body": b"", "more_body": False})

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("Browser socket disappeared")

    async with app.router.lifespan_context(app):
        with pytest.raises(OSError, match="disappeared"):
            await app(http_scope(), received.get, send)
    assert stream.closed
    assert stream.reads == 1


class FakeVoicePeer:
    def __init__(self):
        self.queue = None
        self.sent = []
        self.closed = False
        self.cancelled = False
        self.close_code = None
        self.failure = None

    async def __aenter__(self):
        self.queue = asyncio.Queue()
        self.queue.put_nowait('{"type":"state","state":"waiting"}')
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, packet):
        if self.failure == "send":
            raise ConnectionClosedOK(Close(1001, "leaving"), None)
        self.sent.append(packet)
        await self.queue.put(packet)

    async def recv(self):
        try:
            if self.failure == "receive" and self.queue.empty():
                raise ConnectionClosedOK(Close(1001, "leaving"), None)
            return await self.queue.get()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def close(self, *, code):
        self.closed = True
        self.close_code = code


def mock_voice(monkeypatch):
    calls = []
    peer = FakeVoicePeer()

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return peer

    monkeypatch.setattr(client, "connect", connect)
    return calls, peer


def test_voice_relays_pcm_controls_and_releases_peer(deployment, monkeypatch):
    mock_desktop(monkeypatch, empty_response)
    calls, peer = mock_voice(monkeypatch)
    with (
        TestClient(client.create_app(deployment), base_url=LOCAL_URL) as local,
        local.websocket_connect(VOICE_URL, headers={
            "Origin": "http://localhost", "Authorization": "browser-token",
        }) as socket,
    ):
        assert socket.receive_json()["state"] == "waiting"
        pcm = b"\x01\x00" * 800
        socket.send_bytes(pcm)
        assert socket.receive_bytes() == pcm
        pause = {"type": "pause", "control_id": 7}
        socket.send_json(pause)
        assert socket.receive_json() == pause
        playback = {"type": "playback", "active": True}
        socket.send_json(playback)
        assert socket.receive_json() == playback
    assert peer.closed
    assert peer.cancelled
    assert peer.sent == [pcm, json.dumps(pause, separators=(",", ":")),
                         json.dumps(playback, separators=(",", ":"))]
    url, settings = calls[0]
    assert url == "wss://desktop.test:8080/api/voice/listen"
    assert settings["origin"] == deployment.desktop_url
    assert settings["additional_headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert settings["proxy"] is None
    assert settings["max_queue"] == 1


@pytest.mark.parametrize("headers", [
    {"Origin": "http://attacker.invalid"}, {"Host": "attacker.invalid"},
])
def test_voice_rejects_foreign_browser_before_dial(deployment, monkeypatch, headers):
    mock_desktop(monkeypatch, empty_response)
    calls, _ = mock_voice(monkeypatch)
    with (
        TestClient(client.create_app(deployment), base_url=LOCAL_URL) as local,
        pytest.raises(WebSocketDisconnect) as failure,
        local.websocket_connect(VOICE_URL, headers=headers),
    ):
        pytest.fail("Foreign voice connection accepted")
    assert failure.value.code == 1008
    assert not calls


def test_voice_rejects_oversized_audio_and_closes_both_peers(deployment, monkeypatch):
    mock_desktop(monkeypatch, empty_response)
    _, peer = mock_voice(monkeypatch)
    with (
        TestClient(client.create_app(deployment), base_url=LOCAL_URL) as local,
        local.websocket_connect(VOICE_URL) as socket,
    ):
        socket.receive_json()
        socket.send_bytes(b"x" * 32_001)
        with pytest.raises(WebSocketDisconnect) as failure:
            socket.receive_json()
        assert failure.value.code == 1009
    assert not peer.sent
    assert peer.closed


def test_voice_connection_error_is_visible_without_token(deployment, monkeypatch):
    mock_desktop(monkeypatch, empty_response)

    def fail_connect(*args, **kwargs):
        raise OSError(f"private diagnostic {TOKEN}")

    monkeypatch.setattr(client, "connect", fail_connect)
    with (
        TestClient(client.create_app(deployment), base_url=LOCAL_URL) as local,
        local.websocket_connect(VOICE_URL) as socket,
    ):
        error = socket.receive_json()
        assert error["type"] == "error"
        assert "client token" in error["message"]
        assert TOKEN not in error["message"]
        with pytest.raises(WebSocketDisconnect) as failure:
            socket.receive_json()
        assert failure.value.code == 1011


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_desktop_voice_close_propagates_during_either_direction(
    deployment, monkeypatch, direction,
):
    mock_desktop(monkeypatch, empty_response)
    _, peer = mock_voice(monkeypatch)
    peer.failure = direction
    with (
        TestClient(client.create_app(deployment), base_url=LOCAL_URL) as local,
        local.websocket_connect(VOICE_URL) as socket,
    ):
        assert socket.receive_json()["state"] == "waiting"
        if direction == "send":
            socket.send_bytes(b"\x00\x00")
        with pytest.raises(WebSocketDisconnect) as failure:
            socket.receive_json()
        assert failure.value.code == 1001
    assert peer.closed
    assert peer.close_code == 1001


def test_secure_pairing_pins_startup_relay_and_voice(deployment, monkeypatch):
    import ssl

    from recollect.host_pairing import ensure_host_pairing
    from recollect.pairing import TLS_SERVER_NAME, load_pairing

    metadata = ensure_host_pairing(deployment.ui_dir / "desktop.token")
    credential = load_pairing(metadata["bundle_path"])
    deployment = replace(deployment, token=credential.token,
                         certificate_pem=credential.certificate_pem)
    requests, _, settings = mock_desktop(monkeypatch, empty_response)
    voice_calls, _ = mock_voice(monkeypatch)
    with TestClient(client.create_app(deployment), base_url=LOCAL_URL) as local:
        assert local.get("/api/history").status_code == 200
        with local.websocket_connect(VOICE_URL) as socket:
            assert socket.receive_json()["state"] == "waiting"
    assert len(requests) == 2
    assert all(request.extensions["sni_hostname"] == TLS_SERVER_NAME
               for request in requests)
    assert settings[0]["verify"].verify_mode == ssl.CERT_REQUIRED
    voice_options = voice_calls[0][1]
    assert voice_options["server_hostname"] == TLS_SERVER_NAME
    assert voice_options["ssl"].check_hostname
    assert voice_options["ssl"].verify_mode == ssl.CERT_REQUIRED


def test_client_boundary_rejects_before_upload_admission(deployment, monkeypatch):
    from recollect.limits import ResourceLimitsMiddleware

    requests, _, _ = mock_desktop(monkeypatch, empty_response)
    app = client.create_app(deployment)
    for middleware in app.user_middleware:
        if middleware.cls is ResourceLimitsMiddleware:
            middleware.kwargs["max_requests"] = 0
    with TestClient(app, base_url=LOCAL_URL) as local:
        assert local.post("/api/chat", headers={
            "Origin": "http://attacker.invalid",
        }).status_code == 403
        assert local.post("/api/chat").status_code == 429
    assert len(requests) == 1
