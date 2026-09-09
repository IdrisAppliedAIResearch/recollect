"""Speech waiters must not occupy workers needed by memory and storage."""

import asyncio
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request

from recollect.voice_api import install_voice_routes
from tests.test_voice_api import completed_trace


def test_concurrent_speech_keeps_other_worker_available():
    async def scenario():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        entered, release = threading.Event(), threading.Event()
        count = 0

        class Voice:
            def synthesize(self, text, *, cancelled):
                nonlocal count
                count += 1
                entered.set()
                assert release.wait(3)
                return b"RIFFfixture"

        app = FastAPI()
        state = SimpleNamespace(voice=Voice(), sessions=SimpleNamespace(
            find_trace=lambda _: completed_trace(),
        ))
        install_voice_routes(app, lambda: state)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080",
        ) as client:
            requests = [asyncio.create_task(client.post(
                "/api/voice/speech", json={"turn_id": "turn-1"},
            )) for _ in range(4)]
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                probe = await asyncio.wait_for(asyncio.to_thread(lambda: "alive"), 1)
                assert probe == "alive"
                assert count == 1
            finally:
                release.set()
                responses = await asyncio.gather(*requests)
            assert all(response.status_code == 200 for response in responses)
            assert count == 4

    asyncio.run(scenario())


async def test_disconnected_queued_speech_is_removed_before_native_synthesis(
    monkeypatch,
):
    entered, release = threading.Event(), threading.Event()
    calls = 0

    class Voice:
        def synthesize(self, text, *, cancelled):
            nonlocal calls
            calls += 1
            entered.set()
            assert release.wait(3)
            return b"RIFFfixture"

    async def disconnected(request):
        return request.headers.get("x-test-client") == "disconnected"

    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    app = FastAPI()
    state = SimpleNamespace(voice=Voice(), sessions=SimpleNamespace(
        find_trace=lambda _: completed_trace(),
    ))
    install_voice_routes(app, lambda: state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080",
    ) as client:
        first = asyncio.create_task(client.post(
            "/api/voice/speech", json={"turn_id": "turn-1"},
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            rejected = await asyncio.wait_for(client.post(
                "/api/voice/speech", json={"turn_id": "turn-1"},
                headers={"x-test-client": "disconnected"},
            ), 1)
            assert rejected.status_code == 499
            assert calls == 1
        finally:
            release.set()
            assert (await first).status_code == 200
        assert (await client.post(
            "/api/voice/speech", json={"turn_id": "turn-1"},
        )).status_code == 200
        assert calls == 2


async def test_cancelled_full_wav_request_holds_gate_until_native_worker_returns():
    entered, release = threading.Event(), threading.Event()
    calls = 0

    class Voice:
        def synthesize(self, text, *, cancelled):
            nonlocal calls
            calls += 1
            entered.set()
            assert release.wait(3)
            return b"RIFFfixture"

    app = FastAPI()
    state = SimpleNamespace(voice=Voice(), sessions=SimpleNamespace(
        find_trace=lambda _: completed_trace(),
    ))
    install_voice_routes(app, lambda: state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080",
    ) as client:
        first = asyncio.create_task(client.post(
            "/api/voice/speech", json={"turn_id": "turn-1"},
        ))
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first
            second = asyncio.create_task(client.post(
                "/api/voice/speech", json={"turn_id": "turn-1"},
            ))
            await asyncio.sleep(0.05)
            assert calls == 1 and not second.done()
        finally:
            release.set()
            if second is not None:
                assert (await second).status_code == 200
            with contextlib.suppress(asyncio.CancelledError):
                await first
        assert calls == 2
