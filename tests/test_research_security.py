"""No external sockets: exercise the production policy above a fake connector."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import socket

import httpx
import pytest

from recollect.engine import webtools


def _dns(*addresses: str):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
        for address in addresses
    ]


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1",
        "224.0.0.1", "0.0.0.0", "192.0.2.1", "::1", "fe80::1",
        "ff02::1", "::ffff:127.0.0.1",
    ],
)
async def test_nonpublic_literal_never_reaches_connector(monkeypatch, address):
    def resolve(*args):
        pytest.fail("literal addresses must not be resolved again")

    def connect(request):
        pytest.fail("nonpublic destination reached the connector")

    monkeypatch.setattr(webtools.socket, "getaddrinfo", resolve)
    host = f"[{address}]" if ":" in address else address
    transport = webtools.PublicWebTransport(httpx.MockTransport(connect))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.RequestError, match="nonpublic"):
            await client.get(f"http://{host}/")


@pytest.mark.parametrize("answers", [[], _dns("93.184.216.34", "127.0.0.1")])
async def test_dns_rejects_empty_or_mixed_public_private_answers(monkeypatch, answers):
    monkeypatch.setattr(webtools.socket, "getaddrinfo", lambda *args: answers)

    def connect(request):
        pytest.fail("unvetted DNS answer reached the connector")

    transport = webtools.PublicWebTransport(httpx.MockTransport(connect))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.RequestError):
            await client.get("https://source.example/")


async def test_dns_is_pinned_at_connect_with_original_host_and_tls_name(monkeypatch):
    resolutions = []
    connections = []

    def resolve(host, port):
        resolutions.append(host)
        return _dns("93.184.216.34" if len(resolutions) == 1 else "127.0.0.1")

    def connect(request):
        connections.append(request)
        return httpx.Response(200, content=b"public source")

    monkeypatch.setattr(webtools.socket, "getaddrinfo", resolve)
    transport = webtools.PublicWebTransport(httpx.MockTransport(connect))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.get("https://source.example:8443/path?q=1")
        assert str(response.url) == "https://source.example:8443/path?q=1"
        assert response.text == "public source"
        with pytest.raises(httpx.RequestError, match="nonpublic"):
            await client.get("https://source.example:8443/second")
    assert resolutions == ["source.example", "source.example"]
    assert len(connections) == 1
    sent = connections[0]
    assert str(sent.url) == "https://93.184.216.34:8443/path?q=1"
    assert sent.headers["Host"] == "source.example:8443"
    assert sent.headers["Accept-Encoding"] == "identity"
    assert sent.headers["Connection"] == "close"
    assert sent.extensions["sni_hostname"] == "source.example"


async def test_rebinding_between_fetch_guard_and_transport_is_blocked(monkeypatch):
    resolutions = 0

    def resolve(*args):
        nonlocal resolutions
        resolutions += 1
        return _dns("93.184.216.34" if resolutions == 1 else "127.0.0.1")

    def connect(request):
        pytest.fail("rebinding reached the connector")

    monkeypatch.setattr(webtools.socket, "getaddrinfo", resolve)
    transport = webtools.PublicWebTransport(httpx.MockTransport(connect))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        result = json.loads(await webtools.web_fetch(client, "https://source.example/"))
    assert result["error_kind"] == "blocked_address"
    assert result["retryable"] is False
    assert resolutions == 2


async def test_public_fetch_preserves_content_and_original_source_url(monkeypatch):
    monkeypatch.setattr(
        webtools.socket, "getaddrinfo", lambda *args: _dns("93.184.216.34")
    )
    extracted = []

    def extract(content, url):
        extracted.append((content, url))
        return "A useful public source."

    monkeypatch.setattr(webtools, "_extract_article", extract)
    body = Body([b"<p>A useful ", b"public source.</p>"])
    transport = webtools.PublicWebTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
    )
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        result = json.loads(await webtools.web_fetch(client, "https://source.example/"))
    assert result["text"] == "A useful public source."
    assert result["final_url"] == "https://source.example/"
    assert extracted == [(b"<p>A useful public source.</p>", "https://source.example/")]
    assert body.closed


async def test_redirects_retain_url_identity_and_revalidate_each_destination(
    monkeypatch,
):
    monkeypatch.setattr(
        webtools.socket, "getaddrinfo", lambda *args: _dns("93.184.216.34")
    )
    connections = []

    def connect(request):
        connections.append((request.url.path, request.headers["Host"]))
        location = "/next" if request.url.path == "/start" else "http://127.0.0.1/"
        return httpx.Response(302, headers={"Location": location})

    transport = webtools.PublicWebTransport(httpx.MockTransport(connect))
    async with httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=True
    ) as client:
        with pytest.raises(httpx.RequestError, match="nonpublic"):
            await client.get("https://source.example/start")
    assert connections == [("/start", "source.example"), ("/next", "source.example")]


async def test_default_connector_disables_implicit_proxy_and_shared_ip_keepalive(
    monkeypatch,
):
    options = {}

    def transport_factory(**kwargs):
        options.update(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(200))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport_factory)
    await webtools.PublicWebTransport().aclose()
    assert options["trust_env"] is False
    assert options["limits"].max_keepalive_connections == 0
    assert options["limits"].max_connections == 10


@pytest.mark.parametrize("protected_transport", [False, True])
async def test_compressed_bomb_is_closed_before_body_decode(
    monkeypatch, protected_transport,
):
    monkeypatch.setattr(
        webtools.socket, "getaddrinfo", lambda *args: _dns("93.184.216.34")
    )
    body = Body([gzip.compress(b"x" * 4_000_000)])

    def connect(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=body)

    transport = httpx.MockTransport(connect)
    if protected_transport:
        transport = webtools.PublicWebTransport(transport)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        result = json.loads(await webtools.web_fetch(client, "https://source.example/"))
    assert result["error_kind"] == "unsupported_encoding"
    assert body.reads == 0
    assert body.closed


async def test_search_response_limit_stops_reading_and_closes_body(monkeypatch):
    monkeypatch.setattr(
        webtools.socket, "getaddrinfo", lambda *args: _dns("93.184.216.34")
    )
    body = Body([b"x" * 1_000_000] * 4)
    transport = webtools.PublicWebTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
    )
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.RequestError, match="2 MB"):
            await client.get("https://api.openalex.org/works")
    assert body.reads == 3
    assert body.closed


async def test_cancelled_response_read_closes_upstream(monkeypatch):
    monkeypatch.setattr(
        webtools.socket, "getaddrinfo", lambda *args: _dns("93.184.216.34")
    )
    reading = asyncio.Event()
    closed = asyncio.Event()

    class HangingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            reading.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    transport = webtools.PublicWebTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, stream=HangingBody()))
    )
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        task = asyncio.create_task(client.get("https://source.example/"))
        await asyncio.wait_for(reading.wait(), 1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert closed.is_set()


@pytest.mark.parametrize("url", ["http://[broken", "https://u:p@source.example/"])
async def test_invalid_or_credentialed_fetch_is_a_structured_failure(monkeypatch, url):
    def resolve(*args):
        pytest.fail("invalid URL reached DNS")

    def connect(request):
        pytest.fail("invalid URL reached the connector")

    monkeypatch.setattr(webtools.socket, "getaddrinfo", resolve)
    async with httpx.AsyncClient(transport=httpx.MockTransport(connect)) as client:
        result = json.loads(await webtools.web_fetch(client, url))
    assert result["error_kind"] == "invalid_url"
