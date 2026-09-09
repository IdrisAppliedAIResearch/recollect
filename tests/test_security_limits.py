"""Ingress is bounded before application side effects, including chunked bodies."""

import asyncio

import pytest

from recollect.limits import ResourceLimitsMiddleware


def scope(headers=()):
    return {"type": "http", "method": "POST", "path": "/api/chat",
            "headers": list(headers)}


async def run_request(app, events, headers=()):
    sent = []

    async def receive():
        if events:
            return events.pop(0)
        await asyncio.Event().wait()

    async def send(event):
        sent.append(event)

    await app(scope(headers), receive, send)
    return sent


@pytest.mark.parametrize("headers,events,status", [
    ([(b"content-length", b"9")], [], 413),
    ([(b"content-length", b"1"), (b"content-length", b"1")], [], 400),
    ([(b"content-length", b"-1")], [], 400),
    ([(b"content-length", b"9" * 5000)], [], 413),
    ([], [{"type": "http.request", "body": b"12345", "more_body": True},
          {"type": "http.request", "body": b"6789"}], 413),
])
async def test_bad_or_oversized_request_never_reaches_route(headers, events, status):
    async def route(*args):
        pytest.fail("The rejected request reached its route")

    middleware = ResourceLimitsMiddleware(route, max_body_bytes=8)
    output = await run_request(middleware, events, headers)
    assert output[0]["status"] == status
    assert middleware._active == 0


async def test_bounded_body_is_replayed_exactly_and_disconnect_is_preserved():
    async def route(scope, receive, send):
        assert await receive() == {"type": "http.request", "body": b"12345678",
                                   "more_body": False}
        assert await receive() == {"type": "http.disconnect"}
        await send({"type": "http.response.start", "status": 200})

    middleware = ResourceLimitsMiddleware(route, max_body_bytes=8)
    output = await run_request(middleware, [
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"5678"}, {"type": "http.disconnect"},
    ])
    assert output[0]["status"] == 200


async def test_request_admission_rejects_overload_and_recovers_after_completion():
    entered, release = asyncio.Event(), asyncio.Event()

    async def route(scope, receive, send):
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200})

    middleware = ResourceLimitsMiddleware(route, max_requests=1)
    first = asyncio.create_task(run_request(middleware, [{"type": "http.request"}]))
    await entered.wait()
    try:
        denied = await run_request(middleware, [])
        assert denied[0]["status"] == 429
        assert middleware._active == 1
    finally:
        release.set()
        await first
    recovered = await run_request(middleware, [{"type": "http.request"}])
    assert recovered[0]["status"] == 200


async def test_slow_or_disconnected_upload_releases_admission():
    async def route(*args):
        pytest.fail("An incomplete upload reached the route")

    middleware = ResourceLimitsMiddleware(route, upload_timeout=0.01)
    assert (await run_request(middleware, []))[0]["status"] == 408
    assert await run_request(middleware, [{"type": "http.disconnect"}]) == []
    assert middleware._active == 0


async def test_websocket_admission_releases_cancelled_connection():
    entered = asyncio.Event()

    async def route(scope, receive, send):
        entered.set()
        await asyncio.Event().wait()

    async def receive():
        return {"type": "websocket.connect"}

    sent = []

    async def send(event):
        sent.append(event)

    middleware = ResourceLimitsMiddleware(route, max_websockets=1)
    first = asyncio.create_task(middleware({"type": "websocket"}, receive, send))
    await entered.wait()
    try:
        await middleware({"type": "websocket"}, receive, send)
        assert sent == [{"type": "websocket.close", "code": 1013}]
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert middleware._websockets == 0
