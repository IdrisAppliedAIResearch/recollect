"""Portable pairing pins both HTTPS and voice TLS without model dependencies."""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from recollect.client import create_app
from recollect.deployment import DeploymentConfig
from recollect.host_pairing import ensure_host_pairing
from recollect.pairing import (
    PAIRING_FORMAT,
    TLS_SERVER_NAME,
    load_pairing,
    parse_pairing,
)
from recollect.private_files import read_private, replace_private, write_private

TOKEN = "pairing-test-token-" + "x" * 32


@pytest.fixture
def identity(tmp_path):
    metadata = ensure_host_pairing(tmp_path / "nested" / "desktop.token")
    return metadata, load_pairing(metadata["bundle_path"])


def server_context(metadata):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(metadata["certificate_path"], metadata["private_key_path"])
    return context


def test_provisioning_is_private_portable_and_stable(identity):
    metadata, credential = identity
    before = {name: read_private(Path(metadata[name])) for name in (
        "bundle_path", "certificate_path", "private_key_path",
    )}
    assert "PRIVATE KEY" not in before["bundle_path"].decode()
    assert credential.token not in json.dumps(metadata)
    assert credential.token not in repr(credential)
    assert metadata["server_name"] == TLS_SERVER_NAME
    assert ensure_host_pairing(metadata["bundle_path"]) == metadata
    for name, contents in before.items():
        assert read_private(Path(metadata[name])) == contents
    context = credential.ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.cert_store_stats()["x509"] == 1


def test_plain_token_upgrade_preserves_secret(tmp_path):
    bundle = tmp_path / "legacy.token"
    write_private(bundle, (TOKEN + "\n").encode())
    metadata = ensure_host_pairing(bundle)
    credential = load_pairing(metadata["bundle_path"])
    assert credential.token == TOKEN
    assert credential.certificate_pem is not None
    assert json.loads(read_private(bundle))["format"] == PAIRING_FORMAT


def test_pairing_never_rotates_missing_or_mismatched_identity(identity, tmp_path):
    metadata, credential = identity
    copied = tmp_path / "copied.token"
    write_private(copied, credential.to_bytes())
    with pytest.raises(ValueError, match="identity is missing"):
        ensure_host_pairing(copied)
    other = ensure_host_pairing(tmp_path / "other.token")
    replace_private(Path(metadata["private_key_path"]), read_private(
        Path(other["private_key_path"]),
    ))
    with pytest.raises(ValueError, match="do not match"):
        ensure_host_pairing(metadata["bundle_path"])
    assert load_pairing(metadata["bundle_path"]) == credential


@pytest.mark.parametrize("contents", [
    b"x" * 65_537, b"\xff", b'{"format":"unknown"}',
    b'{"format":"recollect-pairing-v1","token":"x","tls":null}',
    b"{malformed", b"too short",
], ids=["oversized", "non-utf8", "unknown-format", "invalid-tls", "json", "short"])
def test_invalid_pairing_is_refused(contents):
    with pytest.raises(ValueError):
        parse_pairing(contents)


def test_brace_prefixed_legacy_token_migrates_without_changing_secret(tmp_path):
    token = "{" + "legacy-token-character" * 2
    assert parse_pairing(token.encode()).token == token
    bundle = tmp_path / "custom.token"
    write_private(bundle, (token + "\n").encode())
    ensure_host_pairing(bundle)
    credential = load_pairing(bundle)
    assert credential.token == token
    assert credential.certificate_pem is not None


@pytest.mark.parametrize("contents", [
    '{"format":"unsupported-format-is-long-enough-for-token"}',
    '{\n"format":"recollect-pairing-v1",\n"token":"' + TOKEN + '",',
])
def test_invalid_bundle_cannot_fall_back_to_a_legacy_token(contents):
    with pytest.raises(ValueError):
        parse_pairing(contents.encode())


@pytest.mark.parametrize("field,value", [
    ("server_name", "attacker.test"), ("certificate_pem", "bad certificate"),
    ("certificate_pem", "-----BEGIN PRIVATE KEY-----"),
])
def test_pairing_cannot_override_server_identity(identity, field, value):
    _, credential = identity
    document = json.loads(credential.to_bytes())
    document["tls"][field] = value
    with pytest.raises(ValueError):
        parse_pairing(json.dumps(document).encode())


def test_secure_bundle_upgrades_saved_lan_url(identity):
    _, credential = identity
    config = DeploymentConfig(
        mode="client", token=credential.token,
        desktop_url="http://192.168.1.20:8080/",
        certificate_pem=credential.certificate_pem,
    )
    assert config.desktop_url == "https://192.168.1.20:8080"


@pytest.mark.parametrize("allow", [False, True])
def test_legacy_token_cannot_be_sent_over_clear_lan(allow):
    with pytest.raises(ValueError, match="HTTPS"):
        DeploymentConfig(mode="client", token=TOKEN,
                         desktop_url="http://192.168.1.20:8080",
                         allow_http_loopback=allow)


def test_plain_loopback_tunnel_requires_explicit_opt_in():
    with pytest.raises(ValueError, match="explicit loopback"):
        DeploymentConfig(mode="client", token=TOKEN,
                         desktop_url="http://127.0.0.1:8080")
    assert DeploymentConfig(mode="client", token=TOKEN,
                            desktop_url="http://127.0.0.1:8080",
                            allow_http_loopback=True).allow_http_loopback


async def test_client_lifespan_and_relay_use_actual_pinned_tls(identity, tmp_path):
    metadata, credential = identity
    requests = []

    async def respond(reader, writer):
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            requests.append(headers)
            payload = (b'{"mode":"host"}' if b"/api/deployment " in headers
                       else b'{"messages":[]}')
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Connection: close\r\nContent-Length: "
                         + str(len(payload)).encode() + b"\r\n\r\n" + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    (tmp_path / "index.html").write_text("test UI")
    async with await asyncio.start_server(
        respond, "127.0.0.1", 0, ssl=server_context(metadata),
    ) as server:
        port = server.sockets[0].getsockname()[1]
        config = DeploymentConfig(
            mode="client", token=credential.token,
            certificate_pem=credential.certificate_pem,
            desktop_url=f"http://127.0.0.1:{port}", ui_dir=tmp_path,
        )
        app = create_app(config)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost",
        ) as local:
            response = await local.get("/api/history")
            assert response.json() == {"messages": []}
    assert len(requests) == 2
    assert all(f"Bearer {credential.token}".encode() in item for item in requests)


async def test_tls_rejects_unpaired_certificate_and_wrong_server_name(
    identity, tmp_path,
):
    metadata, credential = identity
    other = ensure_host_pairing(tmp_path / "impostor.token")
    impostor = load_pairing(other["bundle_path"])

    async def unused(reader, writer):
        pytest.fail("A failed TLS handshake must not reach HTTP")

    async with await asyncio.start_server(
        unused, "127.0.0.1", 0, ssl=server_context(metadata),
    ) as server:
        url = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with httpx.AsyncClient(
            verify=impostor.ssl_context(), trust_env=False, timeout=5,
        ) as desktop:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await desktop.get(url, extensions=impostor.request_extensions())
        async with httpx.AsyncClient(
            verify=credential.ssl_context(), trust_env=False, timeout=5,
        ) as desktop:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await desktop.get(url)


async def test_voice_tls_uses_same_pinned_identity(identity, tmp_path):
    metadata, credential = identity
    seen = []

    async def echo(socket):
        seen.append(socket.request.headers["Authorization"])
        await socket.send(await socket.recv())

    async with serve(
        echo, "127.0.0.1", 0, ssl=server_context(metadata), close_timeout=1,
    ) as server:
        url = f"wss://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/listen"
        async with connect(
            url, **credential.websocket_options(), proxy=None, open_timeout=5,
            additional_headers={"Authorization": f"Bearer {credential.token}"},
        ) as socket:
            await socket.send(b"\x00\x00" * 800)
            assert await socket.recv() == b"\x00\x00" * 800
        other = ensure_host_pairing(tmp_path / "impostor.token")
        with pytest.raises(ssl.SSLCertVerificationError):
            async with connect(
                url, **load_pairing(other["bundle_path"]).websocket_options(),
                proxy=None, open_timeout=5,
            ):
                pytest.fail("An unpaired certificate accepted a voice connection")
    assert seen == [f"Bearer {credential.token}"]
