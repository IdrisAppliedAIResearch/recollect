"""Offline role protocol/model-transport checks; never executes candidate code."""

import asyncio
import base64
import gzip
import json
from dataclasses import asdict, replace

import httpx
import pytest

from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.executor import Deadline
from recollect.selfmod.journal import IntegrityError, decode
from recollect.selfmod.role_worker import parse
from recollect.selfmod.roles import (
    LocalRoleModel,
    RoleSettings,
    check_result,
    make_context,
    model_payload,
    plan_result,
    review_result,
    role_spec,
)
from tests.selfmod_round_helpers import FakeClock
from tests.test_selfmod_integration import case as case
from tests.test_selfmod_integration import opened, planned

CHECKS = (
    File("unit.py", b"from pathlib import Path\n"
         b"assert Path('editable.py').read_bytes() == b'value = 2\\n'\n"),
    File("regression.py", b"from pathlib import Path\n"
         b"assert Path('protected.py').read_bytes() == b'protected original bytes'\n"),
)


def settings(**kwargs):
    return replace(RoleSettings("http://127.0.0.1:8001/v1", "fixture-model",
                                CHECKS), **kwargs)


class RawStream(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        for start in range(0, len(self.content), 8192):
            yield self.content[start:start + 8192]


def response_body(content, **kwargs):
    value = {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(content),
    }}], "usage": {"completion_tokens": 10}}
    value.update(kwargs)
    return value


def response(content, **kwargs):
    return httpx.Response(200, stream=RawStream(json.dumps(
        response_body(content, **kwargs),
    ).encode()))


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8001/v1", "http://example.com:8001/v1",
    "http://127.0.0.1/v1", "http://user@127.0.0.1:8001/v1",
    "http://127.0.0.1:8001/v1?x=y", "http://127.0.0.1:8001/v1#fragment",
    "http://127.0.0.1:8001/embeddings",
])
def test_model_endpoint_must_be_explicit_loopback_chat(url):
    with pytest.raises(ValueError):
        settings(base_url=url)


@pytest.mark.parametrize("field,value", [
    ("checks", ()),
    ("checks", (File("nested/check.py", b"pass"),)),
])
def test_role_limits_are_frozen(field, value):
    with pytest.raises(ValueError):
        settings(**{field: value})


@pytest.mark.parametrize("raw", [b'{} garbage', b'[]', b'{"x":1,"x":2}',
                                 b'{"x":NaN}', b'{"x":Infinity}', b' ' * 131073,
                                 b'```json\n{}\n```'],
                         ids=["garbage", "array", "duplicate", "nan", "inf",
                              "large", "markdown"])
def test_ambiguous_or_oversize_model_json_rejected(raw):
    with pytest.raises(ValueError):
        parse(raw)


async def test_model_fixed_request_and_raw_evidence(case):
    dev = opened(case)
    profile = settings()
    context = make_context(dev, dev.authorize("plan"), profile)
    calls = []

    def handle(request):
        calls.append(request)
        assert str(request.url) == "http://127.0.0.1:8001/v1/chat/completions"
        payload = json.loads(request.content)
        assert "max_tokens" not in payload and "max_completion_tokens" not in payload
        assert payload["stream"] is False
        assert len(payload["messages"]) == 2 and "tools" not in payload
        return response({"changes": [], "verification": []})

    model = LocalRoleModel(profile, transport=httpx.MockTransport(handle))
    reply, raw = await model.complete(model_payload(context, profile),
                                     Deadline(20_000_000_000, case.clock.boot),
                                     clock=case.clock)
    assert decode(reply) == {"changes": [], "verification": []}
    assert model.response_complete and len(calls) == 1
    assert model.evidence().files[1].content == raw
    with pytest.raises(IntegrityError, match="single-use"):
        await model.complete({}, Deadline(20_000_000_000, case.clock.boot),
                             clock=case.clock)


@pytest.mark.parametrize("fault", ["truncated", "usage", "tool", "overflow",
                                  "redirect", "late"])
async def test_model_failures_cannot_supply_a_role_reply(fault):
    clock = FakeClock()

    def handle(request):
        value = response_body({})
        if fault == "truncated":
            value["choices"][0]["finish_reason"] = "length"
        elif fault == "usage":
            value["usage"]["completion_tokens"] = True
        elif fault == "tool":
            value["choices"][0]["message"]["tool_calls"] = [{}]
        elif fault == "overflow":
            return httpx.Response(200, stream=RawStream(b"x" * 150000))
        elif fault == "redirect":
            return httpx.Response(307, headers={"Location": "http://example.com"})
        else:
            clock.ns = 30_000_000_000
        return httpx.Response(200, stream=RawStream(json.dumps(value).encode()))

    model = LocalRoleModel(settings(), transport=httpx.MockTransport(handle))
    with pytest.raises((IntegrityError, httpx.HTTPStatusError)):
        await model.complete({}, Deadline(20_000_000_000, clock.boot), clock=clock)
    assert len(model.evidence().files[1].content) <= 128 * 1024


@pytest.mark.parametrize("count", [0, 999_999])
async def test_completion_usage_is_audit_data_without_a_token_ceiling(count):
    clock = FakeClock()
    model = LocalRoleModel(settings(), transport=httpx.MockTransport(
        lambda _: response({}, usage={"completion_tokens": count}),
    ))
    reply, raw = await model.complete({}, Deadline(20_000_000_000, clock.boot),
                                     clock=clock)
    assert decode(reply) == {}
    assert json.loads(raw)["usage"]["completion_tokens"] == count
    assert model.evidence().files[1].content == raw


@pytest.mark.parametrize("usage", [None, {}, {"completion_tokens": None},
                                  {"completion_tokens": -1},
                                  {"completion_tokens": True},
                                  {"completion_tokens": "10"},
                                  {"completion_tokens": 1.5}])
async def test_malformed_usage_is_not_accepted_as_audit_evidence(usage):
    clock = FakeClock()
    model = LocalRoleModel(settings(), transport=httpx.MockTransport(
        lambda _: response({}, usage=usage),
    ))
    with pytest.raises(IntegrityError, match="unaccounted"):
        await model.complete({}, Deadline(20_000_000_000, clock.boot), clock=clock)
    assert model.evidence().files[1].content


def test_role_profile_exposes_no_token_or_call_quota():
    assert {"max_tokens", "max_model_calls"}.isdisjoint(asdict(settings()))


async def test_model_cancellation_settles_locally_without_retry():
    entered, settled = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(request):
        calls.append(request)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    clock = FakeClock()
    model = LocalRoleModel(settings(), transport=httpx.MockTransport(handle))
    task = asyncio.create_task(model.complete(
        {}, Deadline(20_000_000_000, clock.boot), clock=clock,
    ))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert settled.is_set() and len(calls) == 1 and not model.response_complete


async def test_encoded_model_response_rejected_before_decompression():
    def handle(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"},
                              stream=RawStream(gzip.compress(b"x" * 1_000_000)))

    clock = FakeClock()
    broker = LocalRoleModel(settings(), transport=httpx.MockTransport(handle))
    with pytest.raises(IntegrityError, match="Encoded"):
        await broker.complete({}, Deadline(20_000_000_000, clock.boot), clock=clock)
    assert broker.evidence().files[1].content == b""


async def test_repeated_cancel_cannot_interrupt_model_transport_cleanup(case):
    entered, closing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closing.set()
            await finish.wait()

    dev = opened(case)
    broker = LocalRoleModel(settings(), transport=Transport())
    task = asyncio.create_task(dev.run_role(dev.authorize("plan"), settings(),
                                           None, model=broker))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and dev._busy and case.controller._development_pending
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not dev._busy and not case.controller._development_pending


def test_driver_and_prompt_capture_cannot_drift_during_build(case, monkeypatch):
    from recollect.selfmod import roles

    dev = planned(case)
    profile = settings()
    identity = profile.identity
    context = make_context(dev, dev.authorize("execute"), profile)
    monkeypatch.setattr(roles, "driver_bytes", lambda: b"substituted driver")
    monkeypatch.setitem(roles.PROMPTS, "execute", "substituted prompt")
    fixture = role_spec(dev, context, b"{}\n", profile, 1000)
    assert next(f.content for f in fixture.baseline.files
                if f.path == "driver.py") == profile.driver
    assert model_payload(context, profile)["messages"][0]["content"] != (
        "substituted prompt"
    )
    assert profile.identity == identity


def test_read_only_role_envelope_has_no_candidate_writable_paths(case):
    dev = planned(case)
    grant = dev.authorize("execute")
    context = make_context(dev, grant, settings())
    fixture = role_spec(dev, context, b"{}\n", settings(), 1000)
    assert fixture.policy.modify == ("source/editable.py",)
    assert fixture.policy.create_under == ("source/generated",)
    context["role"] = "review"
    fixture = role_spec(dev, context, b"{}\n", settings(), 1000)
    assert fixture.policy.modify == fixture.policy.create_under == ()
    assert fixture.baseline.sha256 != case.fixture.baseline.sha256
    assert fixture.binding.baseline_sha256 == fixture.baseline.sha256


def test_plan_cannot_redefine_contract_or_add_out_of_scope_change(case):
    value = asdict(case.plan)
    del value["contract_sha256"]
    assert plan_result(value, case.controller.config.contract,
                       case.fixture.policy) == case.plan
    value["changes"][0]["path"] = "protected.py"
    with pytest.raises(ValueError, match="scope"):
        plan_result(value, case.controller.config.contract, case.fixture.policy)


def test_check_results_are_exit_statuses_not_model_assertions():
    value = {"checks": [
        {"name": file.path[:-3], "passed": True, "exitcode": 0,
         "stdout": base64.b64encode(b"check output").decode(), "stderr": ""}
        for file in CHECKS
    ]}
    assert check_result(value, settings()) == (("unit", True), ("regression", True))
    value["checks"][0]["exitcode"] = 1
    with pytest.raises(IntegrityError):
        check_result(value, settings())


def test_profile_identity_binds_model_checks_and_driver():
    original = settings()
    assert original.identity != settings(model="another-model").identity
    assert original.identity != settings(checks=(File("unit.py", b"pass"),)).identity
    assert original.identity == settings().identity


def finding(**kwargs):
    return {"id": "F1", "severity": "blocking", "status": "open",
            "requirement_id": "value", "path": "editable.py",
            "detail": "Need a narrower implementation", "resolution": "", **kwargs}


@pytest.mark.parametrize("change", ["drop", "retarget", "rewrite", "downgrade"])
def test_review_must_preserve_blocking_finding_identity(case, change):
    dev = planned(case)
    context = make_context(dev, dev.authorize("execute"), settings())
    context["history"] = [{"role_report": {"findings": [finding()]}}]
    updated = finding(status="resolved", resolution="Fixed in the revision")
    if change == "retarget":
        updated["path"] = "protected.py"
    elif change == "rewrite":
        updated["detail"] = "An unrelated easier issue"
    elif change == "downgrade":
        updated["severity"] = "advisory"
    report = {"approved": True, "findings": [] if change == "drop" else [updated],
              "rationale": "Review"}
    with pytest.raises(IntegrityError):
        review_result(report, context)


def test_removed_unnecessary_planned_file_can_resolve_original_finding(case):
    dev = planned(case)
    context = make_context(dev, dev.authorize("execute"), settings())
    previous = finding(path="generated/unnecessary.py")
    context["history"] = [{"role_report": {"findings": [previous]}}]
    report = {"approved": True, "findings": [
        {**previous, "status": "resolved", "resolution": "Removed from the plan"},
    ], "rationale": "The revised plan no longer adds the unnecessary abstraction"}
    assert review_result(report, context) == ()
    context["history"] = []
    with pytest.raises(IntegrityError):
        review_result(report, context)


def test_revision_modifies_original_baseline_not_previous_created_files(case):
    dev = planned(case)
    dev._development._artifact = Snapshot((
        *case.fixture.baseline.files, File("generated/new.py", b"old candidate"),
    ))
    context = make_context(dev, dev.authorize("execute"), settings())
    fixture = role_spec(dev, context, b"{}\n", settings(), 1000)
    assert "source/generated/new.py" not in {f.path for f in fixture.baseline.files}
    context["role"] = "review"
    fixture = role_spec(dev, context, b"{}\n", settings(), 1000)
    assert "source/generated/new.py" in {f.path for f in fixture.baseline.files}
    assert fixture.policy.create_under == fixture.policy.modify == ()


async def test_equal_but_foreign_role_grant_never_calls_model_or_runtime(case):
    dev = opened(case)
    grant = replace(dev.authorize("plan"))
    calls = []
    broker = LocalRoleModel(settings(), transport=httpx.MockTransport(
        lambda request: calls.append(request) or response({})
    ))
    with pytest.raises(IntegrityError, match="grant"):
        await dev.run_role(grant, settings(), None, model=broker)
    assert not calls
    assert not case.controller.eligible


async def test_cancel_during_inference_preserves_role_evidence_and_lease(case):
    dev = opened(case)
    entered, settle = asyncio.Event(), asyncio.Event()

    async def handle(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            settle.set()

    broker = LocalRoleModel(settings(), transport=httpx.MockTransport(handle))
    task = asyncio.create_task(dev.run_role(dev.authorize("plan"), settings(),
                                           None, model=broker))
    await asyncio.wait_for(entered.wait(), 3)
    assert dev._busy and case.controller._development_pending
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert settle.is_set() and not dev._busy
    assert not case.controller._development_pending
    archived = [f.path for r in case.controller.journal.verify()
                if r.value["kind"] == "development_role" for f in r.files.files]
    assert "model-request.json" in archived and "role-request.json" in archived
    with pytest.raises(IntegrityError, match="ineligible"):
        dev.authorize("plan")


async def test_unbounded_model_reply_survives_elapsed_hours(case):
    def handle(request):
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool")
        )
        case.clock.ns += 7200_000_000_000
        return response({})

    broker = LocalRoleModel(settings(), transport=httpx.MockTransport(handle))
    reply, raw = await broker.complete({}, Deadline(None, case.clock.boot),
                                       clock=case.clock)
    assert decode(reply) == {} and raw == broker.evidence().files[1].content
