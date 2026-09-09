"""Shared-model fairness and the isolated worker inference capability."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from recollect.config import RecollectConfig
from recollect.engine.model_admission import ModelAdmission, ModelIngress


async def test_foreground_preference_does_not_starve_background():
    admission = ModelAdmission()
    await admission.acquire()
    order = []

    async def run(name, background=False):
        await admission.acquire(background=background)
        order.append(name)
        admission.release()

    tasks = [asyncio.create_task(run(name, background)) for name, background in [
        ("f1", False), ("f2", False), ("f3", False), ("b1", True), ("b2", True),
    ]]
    await asyncio.sleep(0)
    admission.release()
    await asyncio.gather(*tasks)
    assert order == ["f1", "b1", "f2", "f3", "b2"]
    assert not admission.locked()


async def test_cancel_before_and_after_lease_transfer_does_not_leak():
    admission = ModelAdmission()
    await admission.acquire()
    queued = asyncio.create_task(admission.acquire(background=True))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert admission.locked()
    transferred = asyncio.create_task(admission.acquire(background=True))
    await asyncio.sleep(0)
    admission.release()
    transferred.cancel()
    with pytest.raises(asyncio.CancelledError):
        await transferred
    assert not admission.locked()
    assert not admission._background


async def test_close_rejects_waiters_without_releasing_active_owner():
    admission = ModelAdmission()
    await admission.acquire()
    queued = asyncio.create_task(admission.acquire(background=True))
    await asyncio.sleep(0)
    admission.close()
    with pytest.raises(RuntimeError, match="closing"):
        await queued
    assert admission.locked()
    admission.release()
    with pytest.raises(RuntimeError, match="closing"):
        await admission.acquire()


class FakeModel:
    def __init__(self):
        self.calls = []
        self.token_count = 3
        self.response = httpx.Response(200, content=b"data: [DONE]\n\n")
        self.error = None
        self.properties = {
            "total_slots": 1, "default_generation_settings": {"n_ctx": 4096},
        }

    async def handle(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        if request.url.path == "/props":
            return httpx.Response(200, json=self.properties)
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "formatted prompt"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1] * self.token_count})
        assert request.url.path == "/v1/chat/completions"
        return self.response


@pytest.fixture
async def gateway(tmp_path):
    config = RecollectConfig(
        embedding_model_path=tmp_path / "embedding.gguf", data_dir=tmp_path / "var",
        generator_base_url="http://configured-model/v1", generator_model="test-model",
        generator_api_key="host-only-key", generator_context_tokens=4096,
    )
    admission = ModelAdmission()
    ingress = ModelIngress(config, admission)
    await ingress.client.aclose()
    model = FakeModel()
    ingress.client = httpx.AsyncClient(
        transport=httpx.MockTransport(model.handle),
        base_url="http://configured-model/v1",
        headers={"Authorization": "Bearer host-only-key"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(ingress.app), base_url="http://ingress",
        headers={"Authorization": f"Bearer {ingress.token}"},
    ) as client:
        yield ingress, model, client
    await ingress.close()


def payload(**extra):
    return {
        "model": "test-model", "messages": [{"role": "user", "content": "hello"}],
        "stream": True, **extra,
    }


async def test_worker_capability_exposes_only_model_routes(gateway):
    ingress, model, client = gateway
    response = await client.get("/v1/models", headers={"Authorization": "wrong"})
    assert response.status_code == 401
    response = await client.get("/v1/models")
    assert response.json()["data"][0]["id"] == "test-model"
    for path in ("/api/sessions", "/docs", "/openapi.json", "/v1/embeddings"):
        assert (await client.get(path)).status_code == 404
    response = await client.post("/v1/chat/completions", json=payload(model="other"))
    assert response.status_code == 400
    assert not model.calls
    assert "host-only-key" not in response.text
    assert ingress.token != "host-only-key"


@pytest.mark.parametrize("extra", [
    {"max_tokens": 0}, {"max_tokens": "100"}, {"max_tokens": True},
    {"max_completion_tokens": -1}, {"n": 2}, {"stream": "yes"},
])
async def test_invalid_inference_options_are_rejected_before_dispatch(gateway, extra):
    _, model, client = gateway
    response = await client.post("/v1/chat/completions", json=payload(**extra))
    assert response.status_code == 400
    assert not model.calls


async def test_inference_bound_and_context_guard_preserve_valid_request(gateway):
    ingress, model, client = gateway
    response = await client.post("/v1/chat/completions", json=payload(
        max_tokens=10000, max_completion_tokens=3000,
        chat_template_kwargs={"enable_thinking": True},
    ))
    assert response.status_code == 200
    forwarded = json.loads(model.calls[-1].content)
    assert forwarded["max_tokens"] == 2048
    assert "max_completion_tokens" not in forwarded
    assert forwarded["chat_template_kwargs"] == {"enable_thinking": False}
    assert [call.url.path for call in model.calls] == [
        "/apply-template", "/tokenize", "/v1/chat/completions",
    ]
    assert model.calls[-1].headers["Authorization"] == "Bearer host-only-key"
    assert ingress.measurements[-1]["prompt_tokens"] == 3
    assert not ingress.admission.locked()


async def test_context_overflow_never_reaches_model_inference(gateway):
    ingress, model, client = gateway
    model.token_count = 4096
    response = await client.post("/v1/chat/completions", json=payload())
    assert response.status_code == 400
    assert "context" in response.text
    assert len(model.calls) == 2
    assert not ingress.admission.locked()


async def test_upstream_errors_keep_http_status_and_release_model(gateway):
    ingress, model, client = gateway
    model.response = httpx.Response(429, json={"error": {"message": "model busy"}})
    response = await client.post("/v1/chat/completions", json=payload())
    assert response.status_code == 429
    assert response.json()["error"]["message"] == "model busy"
    assert not ingress.admission.locked()


async def test_nonstreamed_native_requests_keep_json_response(gateway):
    ingress, model, client = gateway
    model.response = httpx.Response(200, json={"choices": [{"text": "answer"}]})
    response = await client.post("/v1/chat/completions", json=payload(stream=False))
    assert response.json()["choices"][0]["text"] == "answer"
    assert not json.loads(model.calls[-1].content)["stream"]
    assert not ingress.admission.locked()


async def test_canceled_queued_http_request_releases_waiter_only(gateway):
    ingress, model, client = gateway
    await ingress.admission.acquire()
    pending = asyncio.create_task(client.post("/v1/chat/completions", json=payload()))
    for _ in range(100):
        if ingress.admission._background:
            break
        await asyncio.sleep(0.01)
    assert ingress.admission._background
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert ingress.admission.locked()
    assert not ingress.admission._background
    assert not ingress._requests
    ingress.admission.release()
    assert len(model.calls) == 2


async def test_context_transport_timeout_is_explicit_and_does_not_take_slot(gateway):
    ingress, model, client = gateway
    model.error = httpx.ReadTimeout("upstream delayed")
    response = await client.post("/v1/chat/completions", json=payload())
    assert response.status_code == 504
    assert not ingress.admission.locked()


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices": []}\n\n'
        raise httpx.ReadError("connection dropped")


async def test_late_stream_failure_is_explicit_and_releases_lease(gateway):
    ingress, model, client = gateway
    model.response = httpx.Response(200, stream=BrokenStream())
    response = await client.post("/v1/chat/completions", json=payload())
    assert response.status_code == 200
    assert '"type": "upstream_error"' in response.text
    assert response.text.endswith("data: [DONE]\n\n")
    assert not ingress.admission.locked()


async def test_real_socket_disconnect_closes_upstream_and_releases_lease(gateway):
    ingress, model, _ = gateway
    closed = asyncio.Event()

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices": []}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    model.response = httpx.Response(200, stream=WaitingStream())
    await ingress.start()
    async with httpx.AsyncClient(timeout=2) as client, client.stream(
        "POST", ingress.base_url + "/chat/completions", json=payload(),
        headers={"Authorization": f"Bearer {ingress.token}"},
    ) as response:
        async for _ in response.aiter_bytes():
            break
    await asyncio.wait_for(closed.wait(), 2)
    assert not ingress.admission.locked()
    assert not ingress._requests


async def test_two_lanes_reserve_conversation_during_native_fanout():
    admission = ModelAdmission(slots=2)
    await admission.acquire(background=True)
    child = asyncio.create_task(admission.acquire(background=True))
    await asyncio.sleep(0)
    await asyncio.wait_for(admission.acquire(), 0.1)
    assert not child.done()
    assert admission._foreground_busy and admission._background_busy
    admission.release()
    # A second conversational turn still cannot hand its lane to native fan-out.
    await asyncio.wait_for(admission.acquire(), 0.1)
    assert not child.done()
    admission.release(background=True)
    await asyncio.wait_for(child, 0.1)
    assert admission._foreground_busy and admission._background_busy
    admission.release(background=True)
    admission.release()
    assert not admission.locked()


async def test_two_lane_transfer_cancellation_preserves_other_owner():
    admission = ModelAdmission(slots=2)
    await admission.acquire()
    await admission.acquire(background=True)
    pending = asyncio.create_task(admission.acquire(background=True))
    await asyncio.sleep(0)
    admission.release(background=True)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert admission._foreground_busy
    assert not admission._background_busy
    admission.release()


async def test_two_lane_shutdown_rejects_both_queues_and_preserves_owners():
    admission = ModelAdmission(slots=2)
    await admission.acquire()
    await admission.acquire(background=True)
    pending = [asyncio.create_task(admission.acquire(background=background))
               for background in (False, True)]
    await asyncio.sleep(0)
    admission.close()
    for waiter in pending:
        with pytest.raises(RuntimeError, match="closing"):
            await waiter
    assert admission._foreground_busy and admission._background_busy
    admission.release()
    admission.release(background=True)
    assert not admission.locked()


@pytest.mark.parametrize("properties", [
    {"total_slots": 2, "default_generation_settings": {"n_ctx": 4096}},
    {"total_slots": 1, "default_generation_settings": {"n_ctx": 2048}},
    {},
])
async def test_start_refuses_unverified_slot_or_context_capacity(gateway, properties):
    ingress, model, _ = gateway
    model.properties = properties
    with pytest.raises(RuntimeError, match="capacity does not match"):
        await ingress.start()
    assert ingress.worker is None
    assert ingress._listener is None


async def test_start_accepts_verified_two_slot_profile(gateway):
    ingress, model, _ = gateway
    ingress.config = replace(ingress.config, generator_parallel_slots=2)
    ingress.admission = ModelAdmission(slots=2)
    model.properties["total_slots"] = 2
    await ingress.start()
    assert ingress.server.started


def test_parallel_slot_configuration_is_explicit(monkeypatch, tmp_path):
    model = tmp_path / "embedding.gguf"
    assert RecollectConfig(embedding_model_path=model).generator_parallel_slots == 1
    monkeypatch.setenv("RECOLLECT_EMBEDDING_MODEL_PATH", str(model))
    monkeypatch.setenv("RECOLLECT_GENERATOR_PARALLEL_SLOTS", "2")
    assert RecollectConfig.from_env().generator_parallel_slots == 2
    with pytest.raises(ValueError, match="generator_parallel_slots"):
        RecollectConfig(embedding_model_path=model, generator_parallel_slots=3)
