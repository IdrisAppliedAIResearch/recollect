"""Amendment 02 experiment profile: no request timeouts or per-response caps."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from recollect import tasks as tasks_module
from recollect.config import RecollectConfig
from recollect.engine import model_admission
from recollect.engine.generator import (
    Generator,
    GeneratorSettings,
    new_generation_trace,
)
from recollect.engine.model_admission import ModelAdmission, ModelIngress
from recollect.engine.sandbox import configgen
from recollect.engine.sandbox import manager as manager_module
from recollect.engine.sandbox.manager import (
    SandboxHandle,
    SandboxManager,
    SandboxStartError,
)
from recollect.engine.sandbox.runner import OpenCodeRunner
from tests.test_model_admission import FakeModel, payload
from tests.test_tasks import environment, submit  # noqa: F401 - pytest fixture

PLAIN_SSE = (
    'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
    "data: [DONE]\n"
)


def make_config(tmp_path, **changes):
    return RecollectConfig(
        embedding_model_path=tmp_path / "embedding.gguf", data_dir=tmp_path / "var",
        sandbox_root=tmp_path / "sandbox",
        generator_base_url="http://configured-model/v1", generator_model="test-model",
        generator_api_key="host-only-key", generator_context_tokens=4096, **changes,
    )


def test_profile_flag_is_explicit_and_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("RECOLLECT_EMBEDDING_MODEL_PATH", str(tmp_path / "e.gguf"))
    monkeypatch.delenv("RECOLLECT_EXPERIMENT_UNBOUNDED", raising=False)
    assert RecollectConfig.from_env(env_file=None).experiment_unbounded is False
    monkeypatch.setenv("RECOLLECT_EXPERIMENT_UNBOUNDED", "1")
    assert RecollectConfig.from_env(env_file=None).experiment_unbounded is True
    with pytest.raises(ValueError, match="experiment_unbounded"):
        make_config(tmp_path, experiment_unbounded="yes")


async def test_unbounded_generator_sends_no_token_cap_and_has_no_timeout():
    settings = GeneratorSettings(base_url="http://127.0.0.1:8000/v1", model="test",
                                 unbounded=True)
    generator = Generator(settings)
    assert generator._client.timeout == httpx.Timeout(None)
    await generator._client.aclose()
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, content=PLAIN_SSE.encode())

    generator._client = httpx.AsyncClient(base_url=settings.base_url,
                                          transport=httpx.MockTransport(handler))
    trace = new_generation_trace(settings=settings, system_prompt="",
                                 context_block="", user_message="hi")
    try:
        async for _ in generator.stream([{"role": "user", "content": "hi"}],
                                        trace=trace, max_tokens=10):
            pass
    finally:
        await generator.aclose()
    assert trace.response_text == "ok"
    assert "max_tokens" not in payloads[0]


@asynccontextmanager
async def gateway(config, admission, **kwargs):
    ingress = ModelIngress(config, admission, **kwargs)
    timeout = ingress.client.timeout
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
        yield ingress, model, client, timeout
    await ingress.close()


async def test_unbounded_ingress_strips_every_cap_and_has_no_timeout(tmp_path):
    config = make_config(tmp_path, experiment_unbounded=True)
    async with gateway(config, ModelAdmission()) as (ingress, model, client, timeout):
        assert timeout == httpx.Timeout(None)
        response = await client.post("/v1/chat/completions", json=payload(
            max_tokens=10, max_completion_tokens=5, max_output_tokens=7))
        assert response.status_code == 200
        forwarded = json.loads(model.calls[-1].content)
        assert not {"max_tokens", "max_completion_tokens",
                    "max_output_tokens"} & forwarded.keys()
        assert not ingress.admission.locked()


async def test_modifier_lane_ingress_pins_its_own_slot_beside_a_busy_worker(tmp_path):
    config = make_config(tmp_path, generator_parallel_slots=3,
                         experiment_unbounded=True)
    admission = ModelAdmission(slots=3)
    await admission.acquire(lane="worker")
    async with gateway(config, admission, lane="modifier") as (_, model, client, _t):
        response = await client.post("/v1/chat/completions", json=payload())
        assert response.status_code == 200
        assert json.loads(model.calls[-1].content)["id_slot"] == 2
        assert not admission._modifier_busy and admission._background_busy
        admission.release(lane="worker")


async def test_unbounded_context_guard_reserves_no_output_but_still_refuses_overflow(
    tmp_path,
):
    config = make_config(tmp_path, experiment_unbounded=True)
    async with gateway(config, ModelAdmission()) as (ingress, model, client, _):
        model.token_count = 4096 - 64
        response = await client.post("/v1/chat/completions", json=payload())
        assert response.status_code == 200
        model.token_count = 4096 - 63
        calls = len(model.calls)
        response = await client.post("/v1/chat/completions", json=payload())
        assert response.status_code == 400 and "0 output tokens" in response.text
        assert [c.url.path for c in model.calls[calls:]] == [
            "/apply-template", "/tokenize"]
        assert not ingress.admission.locked()


def test_ingress_lane_must_exist_in_the_slot_profile(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(ValueError, match="lane"):
        ModelIngress(config, ModelAdmission(slots=1), lane="modifier")
    with pytest.raises(ValueError, match="lane"):
        ModelIngress(config, ModelAdmission(slots=3), lane="conversation")


def test_unbounded_opencode_config_omits_the_chunk_timer(tmp_path):
    bounded = configgen.build_config(tmp_path, base_url="http://m/v1", model="local",
                                     api_key="k", steps=24)
    unbounded = configgen.build_config(tmp_path, base_url="http://m/v1",
                                       model="local", api_key="k", steps=24,
                                       unbounded=True)
    assert bounded["provider"]["recollect"]["options"]["chunkTimeout"] > 0
    options = unbounded["provider"]["recollect"]["options"]
    assert options["timeout"] is False and "chunkTimeout" not in options


class SlowRelay:
    settings = GeneratorSettings(base_url="http://unused", model="fake")

    def build_messages(self, **kwargs):
        return kwargs

    async def stream(self, messages, *, trace, **kwargs):
        await asyncio.sleep(0.05)
        trace.response_text = "Generated relay."
        yield None


@pytest.mark.parametrize("unbounded", [False, True])
async def test_slow_relay_is_replaced_by_raw_text_only_when_bounded(
    environment, monkeypatch, unbounded,  # noqa: F811 - pytest fixture
):
    state = environment
    monkeypatch.setattr(tasks_module, "ANNOUNCEMENT_TIMEOUT", 0.01)
    coordinator = state.coordinator
    coordinator.config = replace(coordinator.config, experiment_unbounded=unbounded)
    coordinator.generator = SlowRelay()
    task = await submit(state)
    key = (state.session_id, task["task_id"])
    coordinator._queue_update(key, "gap", "blocked", "Raw worker text", 1)
    await coordinator._announce_one(key, coordinator._notifications[key])
    delivered = state.store.notifications(state.session_id)[-1]["text"]
    assert delivered == ("Generated relay." if unbounded else "Raw worker text")


def health_handle(tmp_path, unhealthy_polls, process=None):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"healthy": len(calls) > unhealthy_polls})

    return SandboxHandle(
        workdir=tmp_path, port=1, password="p", process=process,
        client=httpx.AsyncClient(base_url="http://sandbox",
                                 transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize("unbounded", [False, True])
async def test_slow_startup_fails_only_under_the_bounded_profile(
    tmp_path, monkeypatch, unbounded,
):
    monkeypatch.setattr(manager_module, "_START_TIMEOUT_S", 0.05)
    monkeypatch.setattr(manager_module, "_HEALTH_POLL_S", 0.01)
    manager = SandboxManager(make_config(tmp_path, experiment_unbounded=unbounded))
    assert manager._control_timeout == (None if unbounded else 10.0)
    handle = health_handle(tmp_path, unhealthy_polls=20)
    try:
        if unbounded:
            await manager._wait_healthy(handle)
        else:
            with pytest.raises(SandboxStartError, match="not healthy"):
                await manager._wait_healthy(handle)
    finally:
        await handle.client.aclose()


async def test_unbounded_startup_still_fails_on_an_observed_exit(tmp_path):
    manager = SandboxManager(make_config(tmp_path, experiment_unbounded=True))
    handle = health_handle(tmp_path, unhealthy_polls=10**9,
                           process=SimpleNamespace(returncode=3))
    try:
        with pytest.raises(SandboxStartError, match="exited early"):
            await manager._wait_healthy(handle)
    finally:
        await handle.client.aclose()


def test_unbounded_runner_reads_and_posts_without_timeouts(tmp_path):
    config = make_config(tmp_path, experiment_unbounded=True)
    runner = OpenCodeRunner(SandboxManager(config), config)
    assert runner._read_timeout is None
    assert runner._request_timeout == httpx.Timeout(None)
    bounded = make_config(tmp_path)
    assert OpenCodeRunner(SandboxManager(bounded), bounded)._read_timeout == 10


def test_model_admission_module_keeps_lane_order():
    assert model_admission.LANES == ("conversation", "worker", "modifier")
