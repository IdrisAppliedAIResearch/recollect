"""Deployment boundaries that do not import the model or memory runtime."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .boundary import LocalBrowserMiddleware, is_loopback
from .pairing import PairingCredential, validate_token

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class DeploymentConfig:
    mode: str = "standalone"
    host: str = "127.0.0.1"
    port: int = 8080
    desktop_url: str | None = None
    token: str | None = field(default=None, repr=False)
    ui_dir: Path | None = None
    certificate_pem: str | None = field(default=None, repr=False)
    ssl_certfile: Path | None = None
    ssl_keyfile: Path | None = None
    allow_http_loopback: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"standalone", "host", "client"}:
            raise ValueError("deployment mode must be standalone, host, or client")
        if not self.host.strip():
            raise ValueError("deployment host must be non-empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("deployment port must be between 1 and 65535")
        if self.mode in {"host", "client"}:
            validate_token(self.token)
        if self.mode in {"standalone", "client"} and not is_loopback(self.host):
            raise ValueError(f"{self.mode} must bind to a loopback address")
        if self.mode == "client" and not self.desktop_url:
            raise ValueError("client requires RECOLLECT_DESKTOP_URL")
        if (self.ssl_certfile is None) != (self.ssl_keyfile is None):
            raise ValueError("Provide both TLS certificate and private key files")
        if self.mode == "host" and not is_loopback(self.host) and not self.ssl_certfile:
            raise ValueError(
                "A network host requires a TLS certificate and private key"
            )
        if self.certificate_pem is not None:
            self.pairing()
        if self.desktop_url is not None:
            try:
                parsed = urlsplit(self.desktop_url)
                valid = (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname is not None
                    and parsed.username is None and parsed.password is None
                    and parsed.path in {"", "/"}
                    and not parsed.query and not parsed.fragment
                    and parsed.port != 0
                    and not any(
                        char.isspace() or ord(char) < 32 or ord(char) == 127
                        for char in self.desktop_url
                    )
                    and "?" not in self.desktop_url
                    and "#" not in self.desktop_url
                    and "\\" not in self.desktop_url
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(
                    "desktop URL must be an HTTP(S) origin without credentials, "
                    "a path, query, or fragment"
                )
            url = self.desktop_url.rstrip("/")
            if self.mode == "client" and self.certificate_pem:
                url = parsed._replace(scheme="https", path="").geturl()
            elif self.mode == "client" and parsed.scheme == "http" and not (
                self.allow_http_loopback and is_loopback(parsed.hostname)
            ):
                raise ValueError(
                    "A client requires HTTPS or a secure pairing bundle; "
                    "HTTP is allowed only with explicit loopback tunnel opt-in"
                )
            object.__setattr__(self, "desktop_url", url)

    def pairing(self) -> PairingCredential:
        return PairingCredential(self.token, self.certificate_pem)

    @classmethod
    def from_env(cls, *, env_file: str | Path | None = ".env") -> DeploymentConfig:
        if env_file is not None and Path(env_file).is_file():
            load_dotenv(env_file)
        return cls(
            mode=os.environ.get("RECOLLECT_DEPLOYMENT_MODE", "standalone").lower(),
            host=os.environ.get("RECOLLECT_HOST", "127.0.0.1"),
            port=int(os.environ.get("RECOLLECT_PORT", "8080")),
            desktop_url=os.environ.get("RECOLLECT_DESKTOP_URL") or None,
            token=os.environ.get("RECOLLECT_DEPLOYMENT_TOKEN") or None,
            ui_dir=(Path(os.environ["RECOLLECT_UI_DIR"])
                    if os.environ.get("RECOLLECT_UI_DIR") else None),
            certificate_pem=os.environ.get("RECOLLECT_TRUSTED_CERTIFICATE_PEM") or None,
            ssl_certfile=(Path(os.environ["RECOLLECT_SSL_CERTFILE"])
                          if os.environ.get("RECOLLECT_SSL_CERTFILE") else None),
            ssl_keyfile=(Path(os.environ["RECOLLECT_SSL_KEYFILE"])
                         if os.environ.get("RECOLLECT_SSL_KEYFILE") else None),
            allow_http_loopback=os.environ.get(
                "RECOLLECT_ALLOW_HTTP_LOOPBACK", "",
            ).lower() in {"1", "true", "yes"},
        )


class BearerTokenMiddleware:
    """Authenticate both HTTP and WebSocket requests before route execution."""

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        self.app = app
        self._token = token.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        authorization = [
            value for name, value in scope.get("headers", [])
            if name.lower() == b"authorization"
        ]
        scheme, _, supplied = (
            authorization[0].partition(b" ") if len(authorization) == 1
            else (b"", b"", b"")
        )
        if scheme.lower() == b"bearer" and secrets.compare_digest(
            supplied, self._token,
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b'{"detail":"Invalid or missing deployment token."}'
        await send({
            "type": "http.response.start", "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def create_app() -> ASGIApp:
    config = DeploymentConfig.from_env(
        env_file=os.environ.get("RECOLLECT_ENV_FILE", ".env"),
    )
    if config.mode == "client":
        from .client import create_app as create_client_app

        return create_client_app(config)

    from .api import create_app as create_backend_app
    from .config import RecollectConfig
    from .limits import ResourceLimitsMiddleware

    app = create_backend_app(
        RecollectConfig.from_env(env_file=None), serve_ui=config.mode == "standalone",
    )

    @app.get("/api/deployment")
    async def deployment_status() -> dict:
        return {"mode": config.mode}

    app.add_middleware(ResourceLimitsMiddleware)
    if config.mode == "host":
        app.add_middleware(BearerTokenMiddleware, token=config.token)
    else:
        app.add_middleware(LocalBrowserMiddleware, allow_development=True)
    return app
