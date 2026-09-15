"""Iterative lifecycle with fake process observations, never host candidate code."""

import asyncio
import base64
import threading
from dataclasses import asdict

import httpx
import pytest

from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.development import Stage
from recollect.selfmod.journal import IntegrityError, decode, encode
from recollect.selfmod.roles import LocalRoleModel
from tests.selfmod_round_helpers import submitted
from tests.test_selfmod_executor import Runtime
from tests.test_selfmod_integration import case as case
from tests.test_selfmod_integration import opened
from tests.test_selfmod_roles import finding, response, settings

APPROVE = {"approved": True, "findings": [], "rationale": "Reviewed"}
EDIT = {"edits": [{"path": "editable.py", "text": "value = 2\n"}]}


class Script:
    def __init__(self, case, replies):
        self.case, self.replies = case, iter(replies)
        self.requests, self.runtimes = [], []

    def model(self, profile):
        def handle(request):
            self.requests.append(decode(decode(request.content)["messages"][1][
                "content"
            ].encode()))
            return response(next(self.replies))

        return LocalRoleModel(profile, transport=httpx.MockTransport(handle))

    def runtime(self):
        runtime = Runtime(self.case.root / f"role-{len(self.runtimes)}",
                          self.case.clock)
        runtime.cancel = lambda: None
        self.runtimes.append(runtime)

        def collect(outer):
            files = {f.path: f.content for f in runtime.config.baseline.files}
            context, reply = decode(files["request.json"]), decode(files["reply.json"])
            if context["role"] == "execute":
                for edit in reply["edits"]:
                    files["source/" + edit["path"]] = edit["text"].encode()
                result = {"edited": [e["path"] for e in reply["edits"]]}
            elif context["role"] == "checks":
                result = {"checks": [{
                    "name": name, "passed": passed, "exitcode": 0 if passed else 1,
                    "stdout": "", "stderr": "",
                } for name, passed in (
                    ("unit", files["source/editable.py"] == b"value = 2\n"),
                    ("regression", files["source/protected.py"] ==
                     b"protected original bytes"),
                )]}
            else:
                result = reply
            outer["stdout"] = base64.b64encode(encode({
                "request_id": context["request_id"], "role": context["role"],
                "result": result,
            })).decode()
            outer["files"] = [{"path": p, "base64": base64.b64encode(b).decode()}
                              for p, b in files.items()]
            outer["snapshot_sha256"] = Snapshot(tuple(
                File(p, b) for p, b in files.items()
            )).sha256
            entries = [e for e in outer["entries"] if e["kind"] == "file"]
            for entry in entries:
                entry["bytes"] = len(files[entry["path"]])
            roots = set(runtime.config.policy.create_under)
            dirs = roots | {"/".join(p.split("/")[:i]) for p in files
                            for i in range(1, len(p.split("/")))}
            entries.extend({
                "path": p, "kind": "directory", "uid": 0,
                "gid": 65532 if p in roots else 0,
                "mode": 0o775 if p in roots else 0o555, "links": 2, "bytes": 40,
            } for p in sorted(dirs))
            outer["entries"] = entries
            return outer

        runtime.result_change = collect
        return runtime


def plan(case):
    return {k: v for k, v in asdict(case.plan).items() if k != "contract_sha256"}


async def test_rejection_check_failure_and_code_revision_keep_original_evidence(case):
    rejected = {"approved": False, "findings": [finding()], "rationale": "Narrow it"}
    resolved = {**APPROVE, "findings": [finding(
        status="resolved", resolution="Revised plan addresses the issue",
    )]}
    code_rejected = {**rejected, "findings": [finding(id="F2")]}
    code_resolved = {**APPROVE, "findings": [finding(
        id="F2", status="resolved", resolution="Revised code checked",
    )]}
    script = Script(case, [plan(case), rejected, plan(case), resolved,
                          {"edits": [{"path": "editable.py", "text": "value = 3\n"}]},
                          EDIT, code_rejected, EDIT, code_resolved])
    dev = opened(case)
    await dev.run_until_ready(settings(), script.runtime, model_factory=script.model)
    assert dev.stage == Stage.READY and not case.controller._development_pending
    assert [c["role"] for c in script.requests] == [
        "plan", "review", "plan", "review", "execute", "execute", "review",
        "execute", "review",
    ]
    assert len(script.runtimes) == 12
    assert script.requests[1]["candidate"] == []
    assert script.requests[1]["candidate_sha256"] is None
    assert script.requests[-1]["candidate_sha256"] == dev.binding.artifact_sha256
    assert all(c["contract"] == script.requests[0]["contract"] for c in script.requests)
    assert all(c["baseline"] == script.requests[0]["baseline"] for c in script.requests)
    history = script.requests[-1]["history"]
    assert sum(r["kind"] == "plan" for r in history) == 2
    assert any(r.get("role_report", {}).get("approved") is False for r in history)
    assert any(r["kind"] == "checks" and not all(v for _, v in r["report"]["results"])
               for r in history)
    assert case.controller._number == 0  # Readiness is not submission or success.
    assert dev.submit(dev.authorize("submit"))[0] == 1
    candidate = submitted(case.controller)
    assert candidate["candidate/editable.py"] == b"value = 2\n"
    for record in case.controller.journal.verify():
        if record.value["kind"] == "development_driver":
            assert candidate[f"records/{record.anchor.sequence}.json"] == record.body


async def test_repeated_rejection_has_no_iteration_or_elapsed_quota(case):
    reject = {**APPROVE, "approved": False}
    script = Script(case, [*([plan(case), reject] * 9),
                           plan(case), APPROVE, EDIT, APPROVE])
    dev = opened(case)
    original = script.runtime

    def delayed():
        case.clock.ns += 86400 * 1_000_000_000
        return original()

    await dev.run_until_ready(settings(), delayed, model_factory=script.model)
    assert dev.stage == Stage.READY and len(script.requests) == 22
    assert case.controller._number == 0


@pytest.mark.parametrize("point", ["claim", "between_roles", "finalization"])
async def test_cancel_at_driver_boundaries_is_terminal_after_settlement(
    case, monkeypatch, point,
):
    dev = opened(case)
    script = Script(case, [plan(case), APPROVE, EDIT, APPROVE])
    entered, release = threading.Event(), threading.Event()
    name = {"claim": "_claim_driver", "between_roles": "_next_driver_grant",
            "finalization": "_finish_driver"}[point]
    original = getattr(dev, name)

    def held(*args):
        result = original(*args)
        if not entered.is_set() and (point != "between_roles" or dev._generation == 1):
            entered.set()
            assert release.wait(10), "test barrier not released"
        return result

    monkeypatch.setattr(dev, name, held)
    task = asyncio.create_task(dev.run_until_ready(
        settings(), script.runtime, model_factory=script.model,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert case.controller._development_pending and not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not case.controller._eligible and dev._driver is None
    assert not case.controller._development_pending


@pytest.mark.parametrize("competitor", ["driver", "grant", "reopen", "close"])
async def test_driver_claim_cannot_be_stolen_or_released_by_competitor(
    case, competitor,
):
    dev = opened(case)
    entered, release = threading.Event(), threading.Event()

    def factory():
        entered.set()
        assert release.wait(10)
        return None

    task = asyncio.create_task(dev.run_until_ready(settings(), factory))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        owner = dev._driver
        with pytest.raises(IntegrityError):
            if competitor == "driver":
                await dev.run_until_ready(settings(), factory)
            elif competitor == "grant":
                dev.authorize("plan")
            elif competitor == "reopen":
                case.controller.open_development(
                    baseline=case.fixture.baseline, policy=case.fixture.policy,
                    settings=case.settings)
            else:
                case.controller.close()
        assert dev._driver is owner and case.controller._development_pending
        if competitor == "close":
            task.cancel()  # Close rejects without aborting; explicitly end fixture.
    finally:
        release.set()
        with pytest.raises((IntegrityError, asyncio.CancelledError)):
            await task
    assert not case.controller._eligible and not case.controller._development_pending


async def test_factory_error_is_recorded_and_never_retried(case):
    dev = opened(case)
    calls = []

    def broken():
        calls.append(1)
        raise OSError("fixture factory failed")

    with pytest.raises(OSError, match="factory failed"):
        await dev.run_until_ready(settings(), broken)
    assert calls == [1] and not case.controller._eligible
    assert not case.controller._development_pending
    with pytest.raises(IntegrityError):
        await dev.run_until_ready(settings(), broken)
    assert calls == [1]


async def test_abort_after_ready_record_cannot_deliver_success(case, monkeypatch):
    dev = opened(case)
    script = Script(case, [plan(case), APPROVE, EDIT, APPROVE])
    entered, release = threading.Event(), threading.Event()
    original = dev._finish_driver

    def held(owner, error):
        original(owner, error)
        if error is None:
            entered.set()
            assert release.wait(10)

    monkeypatch.setattr(dev, "_finish_driver", held)
    task = asyncio.create_task(dev.run_until_ready(
        settings(), script.runtime, model_factory=script.model,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        with pytest.raises(IntegrityError, match="cleanup"):
            case.controller.close()
        assert dev._driver is not None and case.controller._development_pending
        case.controller.fail("host aborted the round")
    finally:
        release.set()
        with pytest.raises(IntegrityError, match="revoked"):
            await task
    records = [r.value["data"] for r in case.controller.journal.verify()
               if r.value["kind"] == "development_driver"]
    assert [r["state"] for r in records][-2:] == ["ready", "failed"]
    assert not case.controller._eligible and not case.controller._development_pending
    assert case.controller._number == 0
