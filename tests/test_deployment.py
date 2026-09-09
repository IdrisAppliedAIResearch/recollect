"""Deployment boundaries keep models on the host and authenticate both transports."""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from recollect.deployment import (
    BearerTokenMiddleware,
    DeploymentConfig,
    create_app,
)

TOKEN = "deployment-test-secret-0123456789abcdef"


@pytest.fixture
def clean_deployment_env(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for name in (
        "RECOLLECT_DEPLOYMENT_MODE", "RECOLLECT_HOST", "RECOLLECT_PORT",
        "RECOLLECT_DESKTOP_URL", "RECOLLECT_DEPLOYMENT_TOKEN", "RECOLLECT_UI_DIR",
        "RECOLLECT_EMBEDDING_MODEL_PATH", "CDW_EMBEDDING_MODEL_PATH",
        "RECOLLECT_ENV_FILE", "RECOLLECT_TRUSTED_CERTIFICATE_PEM",
        "RECOLLECT_SSL_CERTFILE", "RECOLLECT_SSL_KEYFILE",
        "RECOLLECT_ALLOW_HTTP_LOOPBACK",
    ):
        monkeypatch.delenv(name, raising=False)


def test_standalone_defaults_do_not_require_model_configuration(
    clean_deployment_env,
):
    assert DeploymentConfig.from_env(env_file=None) == DeploymentConfig()


def test_client_environment_is_independent_of_model_settings(
    clean_deployment_env, monkeypatch, tmp_path,
):
    env_file = tmp_path / "client.env"
    env_file.write_text(
        "RECOLLECT_DEPLOYMENT_MODE=client\n"
        "RECOLLECT_HOST=localhost\n"
        "RECOLLECT_PORT=8081\n"
        "RECOLLECT_DESKTOP_URL=https://192.168.1.20:8080/\n"
        f"RECOLLECT_DEPLOYMENT_TOKEN={TOKEN}\n"
        "RECOLLECT_UI_DIR=ui/dist\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RECOLLECT_PORT", "9000")
    config = DeploymentConfig.from_env(env_file=env_file)
    assert config == DeploymentConfig(
        mode="client", host="localhost", port=9000,
        desktop_url="https://192.168.1.20:8080", token=TOKEN,
        ui_dir=Path("ui/dist"),
    )


@pytest.mark.parametrize("mode", ["host", "client"])
@pytest.mark.parametrize("token", [None, "short", "x" * 31, " " * 32,
                                  "x" * 32 + "\n", "é" * 32])
def test_network_modes_require_a_usable_long_token(mode, token):
    with pytest.raises(ValueError, match="TOKEN"):
        DeploymentConfig(
            mode=mode, token=token, desktop_url="https://192.168.1.20:8080",
        )


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "desktop"])
def test_client_cannot_bind_a_network_interface(host):
    with pytest.raises(ValueError, match="loopback"):
        DeploymentConfig(
            mode="client", host=host, token=TOKEN,
            desktop_url="https://192.168.1.20:8080",
        )


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.0.0.2", "::1"])
def test_client_accepts_only_local_listener_addresses(host):
    assert DeploymentConfig(
        mode="client", host=host, token=TOKEN,
        desktop_url="https://desktop.example:8443",
    ).host == host


@pytest.mark.parametrize("url", [
    "file:///desktop", "ws://desktop:8080", "//desktop:8080", "http://",
    "http://user:secret@desktop:8080", "http://desktop:8080/api",
    "http://desktop:8080/?token=secret", "http://desktop:8080/#fragment",
    "http://desktop:8080?", "http://desktop:8080#",
    "http://desktop:70000", "http://desktop:0", "http://[",
    "http://desktop:8080\n", "\x00http://desktop", "http://desktop\\other",
])
def test_client_requires_a_fixed_http_origin(url):
    with pytest.raises(ValueError, match="HTTP\\(S\\) origin"):
        DeploymentConfig(mode="client", token=TOKEN, desktop_url=url)


def test_client_requires_an_upstream():
    with pytest.raises(ValueError, match="DESKTOP_URL"):
        DeploymentConfig(mode="client", token=TOKEN)


@pytest.mark.parametrize("changes", [
    {"mode": "remote"}, {"host": ""}, {"port": 0}, {"port": 65536},
])
def test_invalid_listener_settings_fail_before_startup(changes):
    with pytest.raises(ValueError):
        DeploymentConfig(**changes)


@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("headers", [
    [], [(b"authorization", b"Bearer wrong")],
    [(b"authorization", f"Basic {TOKEN}".encode())],
    [(b"authorization", f"Bearer  {TOKEN}".encode())],
    [(b"authorization", f"Bearer {TOKEN}".encode())] * 2,
])
async def test_auth_rejects_requests_before_any_backend_code(transport, headers):
    called = []
    sent = []

    async def backend(scope, receive, send):
        called.append(scope)

    async def receive():
        raise AssertionError("Authentication must not consume the request body")

    async def send(message):
        sent.append(message)

    app = BearerTokenMiddleware(backend, token=TOKEN)
    await app({"type": transport, "headers": headers}, receive, send)
    assert called == []
    if transport == "websocket":
        assert sent == [{"type": "websocket.close", "code": 1008}]
    else:
        assert sent[0]["status"] == 401
        assert (b"www-authenticate", b"Bearer") in sent[0]["headers"]
        assert TOKEN.encode() not in sent[1]["body"]


@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_authenticated_transports_reach_the_backend_unchanged(transport):
    called = []

    async def backend(scope, receive, send):
        called.append((scope, receive, send))

    async def channel():
        raise AssertionError("The middleware must not consume channel messages")

    scope = {
        "type": transport,
        "headers": [(b"authorization", f"bearer {TOKEN}".encode())],
    }
    await BearerTokenMiddleware(backend, token=TOKEN)(scope, channel, channel)
    assert called == [(scope, channel, channel)]


async def test_auth_preserves_asgi_lifespan():
    called = []

    async def backend(scope, receive, send):
        called.append(scope["type"])

    await BearerTokenMiddleware(backend, token=TOKEN)(
        {"type": "lifespan"}, None, None,
    )
    assert called == ["lifespan"]


@pytest.mark.parametrize("mode", ["standalone", "host"])
def test_factory_selects_ui_and_auth_without_loading_models(monkeypatch, mode):
    config = DeploymentConfig(mode=mode, token=TOKEN if mode == "host" else None)
    monkeypatch.setattr(
        DeploymentConfig, "from_env", classmethod(lambda cls, **kwargs: config),
    )
    backend_config = object()
    calls = []

    def backend_factory(config, *, serve_ui):
        calls.append((config, serve_ui))
        return FastAPI()

    monkeypatch.setitem(sys.modules, "recollect.api", SimpleNamespace(
        create_app=backend_factory,
    ))
    def backend_settings(*, env_file):
        assert env_file is None
        return backend_config

    monkeypatch.setitem(sys.modules, "recollect.config", SimpleNamespace(
        RecollectConfig=SimpleNamespace(from_env=backend_settings),
    ))
    app = create_app()
    assert calls == [(backend_config, mode == "standalone")]
    client = TestClient(app, base_url="http://localhost")
    response = client.get("/api/deployment")
    if mode == "host":
        assert response.status_code == 401
        response = client.get("/api/deployment", headers={
            "Authorization": f"Bearer {TOKEN}",
        })
    assert response.json() == {"mode": mode}


def test_client_factory_never_imports_backend_configuration(monkeypatch):
    config = DeploymentConfig(
        mode="client", token=TOKEN, desktop_url="https://192.168.1.20:8080",
    )
    monkeypatch.setattr(
        DeploymentConfig, "from_env", classmethod(lambda cls, **kwargs: config),
    )
    marker = object()
    received = []

    def client_factory(config):
        received.append(config)
        return marker

    monkeypatch.setitem(sys.modules, "recollect.client", SimpleNamespace(
        create_app=client_factory,
    ))
    monkeypatch.setitem(sys.modules, "recollect.api", None)
    monkeypatch.setitem(sys.modules, "recollect.config", None)
    assert create_app() is marker
    assert received == [config]


def test_backend_can_omit_ui_without_requiring_models(monkeypatch, tmp_path):
    import recollect.api as api
    from recollect.config import RecollectConfig

    monkeypatch.setattr(api, "__file__", str(tmp_path / "src/recollect/api.py"))
    dist = tmp_path / "ui/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<title>test inspector</title>")
    config = RecollectConfig(embedding_model_path=tmp_path / "unused.gguf")
    assert TestClient(api.create_app(config)).get("/").status_code == 200
    host = TestClient(api.create_app(config, serve_ui=False))
    assert host.get("/").status_code == 404


def test_standalone_metadata_remains_available_with_built_ui(monkeypatch, tmp_path):
    import recollect.api as api
    from recollect.config import RecollectConfig

    monkeypatch.setattr(api, "__file__", str(tmp_path / "src/recollect/api.py"))
    dist = tmp_path / "ui/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<title>test inspector</title>")
    config = RecollectConfig(
        embedding_model_path=tmp_path / "unused.gguf", subagent_enabled=False,
    )
    monkeypatch.setattr(
        RecollectConfig, "from_env", classmethod(lambda cls, **kwargs: config),
    )
    monkeypatch.setattr(
        DeploymentConfig, "from_env",
        classmethod(lambda cls, **kwargs: DeploymentConfig()),
    )
    warmed = []

    async def close():
        pass

    state = SimpleNamespace(
        embedder=SimpleNamespace(warm_up=lambda: warmed.append(True) or {}),
        sandboxes=SimpleNamespace(close_all=close),
        generator=SimpleNamespace(aclose=close),
        web_client=SimpleNamespace(aclose=close),
    )
    monkeypatch.setattr(api, "AppState", lambda config: state)
    with TestClient(create_app(), base_url="http://localhost") as client:
        assert "test inspector" in client.get("/").text
        response = client.get("/api/deployment")
        assert response.status_code == 200
        assert response.json() == {"mode": "standalone"}
    assert warmed == [True]


def test_factory_respects_selected_env_file_without_loading_default_dotenv(
    clean_deployment_env, monkeypatch, tmp_path,
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "RECOLLECT_HOST=0.0.0.0\nRECOLLECT_PORT=9090\n",
        encoding="utf-8",
    )
    env_file = tmp_path / "client.env"
    env_file.write_text(
        "RECOLLECT_DEPLOYMENT_MODE=client\n"
        "RECOLLECT_DESKTOP_URL=https://192.168.1.20:8080\n"
        f"RECOLLECT_DEPLOYMENT_TOKEN={TOKEN}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RECOLLECT_ENV_FILE", str(env_file))
    received = []
    monkeypatch.setitem(sys.modules, "recollect.client", SimpleNamespace(
        create_app=lambda config: received.append(config),
    ))
    create_app()
    assert len(received) == 1
    assert received[0].host == "127.0.0.1"
    assert received[0].port == 8080


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20"])
def test_standalone_cannot_expose_unauthenticated_backend(host):
    with pytest.raises(ValueError, match="loopback"):
        DeploymentConfig(host=host)


def test_network_host_requires_tls_files():
    with pytest.raises(ValueError, match="TLS certificate"):
        DeploymentConfig(mode="host", host="0.0.0.0", token=TOKEN)
    with pytest.raises(ValueError, match="both TLS"):
        DeploymentConfig(mode="host", token=TOKEN, ssl_certfile=Path("cert.pem"))


def test_standalone_browser_boundary_covers_http_and_voice(monkeypatch):
    from fastapi.middleware.cors import CORSMiddleware

    from recollect.boundary import DEVELOPMENT_ORIGINS

    called = []
    backend = FastAPI()
    backend.add_middleware(CORSMiddleware, allow_origins=list(DEVELOPMENT_ORIGINS),
                           allow_methods=["*"], allow_headers=["*"])

    @backend.get("/api/history")
    async def history():
        called.append("history")
        return {"messages": []}

    @backend.websocket("/api/voice/listen")
    async def voice(socket):
        pytest.fail("An untrusted browser must never reach voice initialization")

    monkeypatch.setattr(DeploymentConfig, "from_env", classmethod(
        lambda cls, **kwargs: DeploymentConfig(),
    ))
    monkeypatch.setitem(sys.modules, "recollect.api", SimpleNamespace(
        create_app=lambda *args, **kwargs: backend,
    ))
    monkeypatch.setitem(sys.modules, "recollect.config", SimpleNamespace(
        RecollectConfig=SimpleNamespace(from_env=lambda **kwargs: object()),
    ))
    local = TestClient(create_app(), base_url="http://localhost")
    assert local.get("/api/history", headers={
        "Origin": "https://attacker.invalid",
    }).status_code == 403
    assert local.get("/api/history", headers={
        "Host": "attacker.invalid", "Origin": "http://attacker.invalid",
    }).status_code == 403
    assert not called
    with pytest.raises(WebSocketDisconnect) as failure, local.websocket_connect(
        "ws://attacker.invalid/api/voice/listen",
        headers={"Origin": "http://attacker.invalid"},
    ):
        pytest.fail("A hostile Host/Origin pair reached voice")
    assert failure.value.code == 1008
    for origin in DEVELOPMENT_ORIGINS:
        response = local.options("/api/history", headers={
            "Origin": origin, "Access-Control-Request-Method": "GET",
        })
        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == origin
    assert local.get("/api/history").json() == {"messages": []}
    assert called == ["history"]


async def test_websocket_admission_is_bounded_and_released_on_cancel():
    from recollect.limits import ResourceLimitsMiddleware

    entered = asyncio.Event()
    sent = []

    async def backend(scope, receive, send):
        entered.set()
        await asyncio.Event().wait()

    async def unexpected_receive():
        pytest.fail("Excess voice sockets must not consume packets")

    async def send(message):
        sent.append(message)

    app = ResourceLimitsMiddleware(backend, max_websockets=1)
    first = asyncio.create_task(app({"type": "websocket"}, unexpected_receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await app({"type": "websocket"}, unexpected_receive, send)
        assert sent == [{"type": "websocket.close", "code": 1013}]
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    assert app._websockets == 0


@pytest.mark.parametrize("mode", ["standalone", "host"])
def test_factory_rejects_untrusted_requests_before_admission(monkeypatch, mode):
    from recollect.limits import ResourceLimitsMiddleware

    monkeypatch.setattr(DeploymentConfig, "from_env", classmethod(
        lambda cls, **kwargs: DeploymentConfig(mode=mode, token=TOKEN),
    ))
    monkeypatch.setitem(sys.modules, "recollect.api", SimpleNamespace(
        create_app=lambda *args, **kwargs: FastAPI(),
    ))
    monkeypatch.setitem(sys.modules, "recollect.config", SimpleNamespace(
        RecollectConfig=SimpleNamespace(from_env=lambda **kwargs: object()),
    ))
    app = create_app()
    for middleware in app.user_middleware:
        if middleware.cls is ResourceLimitsMiddleware:
            middleware.kwargs["max_requests"] = 0
    local = TestClient(app, base_url="http://localhost")
    headers = {"Origin": "http://attacker.invalid"} if mode == "standalone" else {}
    response = local.get("/api/deployment", headers=headers)
    assert response.status_code == (403 if mode == "standalone" else 401)
    headers = {} if mode == "standalone" else {"Authorization": f"Bearer {TOKEN}"}
    assert local.get("/api/deployment", headers=headers).status_code == 429
