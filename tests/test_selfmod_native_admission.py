"""Native admission authority only: no Docker, network, models or candidate import."""

import asyncio
import threading
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from recollect.selfmod.contracts import (
    Plan,
    PlannedChange,
    Requirement,
    TaskContract,
    Verification,
)
from recollect.selfmod.development import Review, Stage
from recollect.selfmod.integration import DevelopmentSettings
from recollect.selfmod.journal import IntegrityError, decode, encode, sha256
from recollect.selfmod.native_admission import NativeStop
from recollect.selfmod.round import ModificationRound
from tests.selfmod_containment_helpers import spec
from tests.selfmod_round_helpers import (
    EVIDENCE,
    FakeClock,
    Fault,
    round_config,
    values,
)


@pytest.fixture
def case(tmp_path):
    source = spec()
    contract = TaskContract(
        "Preserve the original task and change the allowed value", (
            Requirement("value", "value equals 2", "independent evaluator"),
        ), ("unit", "regression"), source.policy.sha256,
    )
    plan = Plan(contract.sha256, (
        PlannedChange("editable.py", "modify", ("value",), "original task"),
    ), (Verification("value", "unit and independent evaluation"),))
    clock, fault = FakeClock(), Fault()
    controller = ModificationRound.create(
        tmp_path / "controller", round_config(contract, source.baseline.sha256),
        clock=clock, fault=fault,
    )
    dev = controller.open_development(
        baseline=source.baseline, policy=source.policy,
        settings=DevelopmentSettings(source.image_id, source.image_environment,
                                     source.entrypoint),
    )
    dev.propose(dev.authorize("plan"), plan)
    review = dev.authorize("review")
    dev.review(review, Review(dev.binding, review.actor_id, False,
                             ("F1: retain task identity",), EVIDENCE.sha256), EVIDENCE)
    dev.propose(dev.authorize("plan"), plan)
    review = dev.authorize("review")
    dev.review(review, Review(dev.binding, review.actor_id, True, (),
                             EVIDENCE.sha256), EVIDENCE)
    yield SimpleNamespace(controller=controller, dev=dev, clock=clock, fault=fault)
    # Some faults intentionally retain the production lease. These adapters are
    # inert: close only the test-owned archive, without claiming runtime cleanup.
    controller.journal.close()


def admit(case, grant=None, **kwargs):
    return case.dev.admit_native(
        grant if grant is not None else case.dev.authorize("execute"),
        **({"model": "fixture-model", "context_limit": 32768, "output_limit": 4096,
            "base_url": "http://127.0.0.1:8001/v1"} | kwargs),
    )


class Resources:
    def __init__(self, admission):
        self.admission = admission
        self.releases = self.closes = self.verifications = 0
        self.close_error = self.release_error = None
        self.entered = self.finish = None
        self.stop = NativeStop(admission.run, sha256(admission.settings.authority),
                               True, True, EVIDENCE)

    def release(self):
        self.releases += 1
        if self.release_error:
            raise self.release_error

    async def close(self):
        self.closes += 1
        if self.entered:
            self.entered.set()
            await self.finish.wait()
        if self.close_error:
            raise self.close_error

    def verify_stop(self):
        self.verifications += 1
        return self.stop


def started(admission):
    resource = Resources(admission)
    admission.start(lambda owner: resource)
    return resource


async def test_context_comes_from_controller_and_cannot_admit_candidate(case):
    admission = admit(case)
    context = decode(admission.settings.authority)
    expected_contract = decode(encode(asdict(case.controller.config.contract)))
    assert context["contract"] == expected_contract
    assert context["run"]["binding"] == asdict(case.dev.binding)
    assert context["run"]["generation"] == case.dev._generation
    assert context["expected_actor"] == case.dev._actors["author"]
    assert context["policy"]["modify"] == ["editable.py"]
    assert context["plan"]["changes"][0]["path"] == "editable.py"
    assert any(r.get("report", {}).get("unresolved_blockers") == [
        "F1: retain task identity",
    ] for r in context["history"])
    assert admission.settings.policy is case.dev._policy
    assert admission.broker_settings.base_url == "http://127.0.0.1:8001/v1"
    before = admission.settings.authority
    decode(before)["history"].clear()
    assert admission.settings.authority == before
    resource = started(admission)
    admission.release()
    admission.guard()
    await admission.close()
    assert resource.releases == resource.closes == resource.verifications == 1
    assert admission.settled and not case.dev._busy
    assert not case.controller._development_pending
    assert not case.controller._eligible
    assert case.dev.stage == Stage.IMPLEMENT
    assert case.dev._receipt is None and case.dev._development._artifact is None
    assert not hasattr(admission, "accept_receipt")
    assert not hasattr(admission, "refresh")
    assert "native_admission_failed" in case.controller.reasons


@pytest.mark.parametrize("change", [
    {}, {"grant_id": "foreign"}, {"actor_id": "foreign"},
    {"controller_instance": "foreign"}, {"cycle_id": "foreign"},
    {"generation": -1}, {"action": "review"}, {"stage": "plan"},
])
def test_forged_or_equal_but_foreign_grant_cannot_claim(case, change):
    grant = case.dev.authorize("execute")
    with pytest.raises(IntegrityError, match="grant"):
        admit(case, replace(grant, **change))
    assert not case.dev._busy and not case.controller._development_pending
    assert not case.controller._eligible


def test_stale_actual_binding_rejected_before_ownership(case):
    grant = case.dev.authorize("execute")
    case.dev._development._revision += 1
    with pytest.raises(IntegrityError, match="grant"):
        admit(case, grant)
    assert not case.dev._busy


async def test_competing_constructor_does_not_steal_or_revoke_owner(case):
    grant = case.dev.authorize("execute")
    admission = admit(case, grant)
    resource = started(admission)
    admission.release()
    with pytest.raises(IntegrityError, match="already owned"):
        await asyncio.to_thread(admit, case, grant)
    admission.guard()
    assert case.dev._busy and case.controller._eligible
    with pytest.raises(IntegrityError, match="cleanup"):
        case.controller.close()
    await admission.close()
    assert resource.closes == 1


@pytest.mark.parametrize("drift", [
    "binding", "generation", "actor", "plan", "boot", "broker", "history",
])
async def test_guard_rejects_stale_authority(case, drift):
    admission = admit(case)
    started(admission)
    admission.release()
    if drift == "binding":
        case.dev._development._revision += 1
    elif drift == "generation":
        case.dev._generation += 1
    elif drift == "actor":
        case.dev._actors["author"] = "replacement"
    elif drift == "plan":
        case.dev._development._plan = replace(
            case.dev._development._plan, verification=(Verification("value", "drift"),),
        )
    elif drift == "boot":
        case.clock.boot = "another-process"
    elif drift == "broker":
        object.__setattr__(admission.broker_settings, "base_url",
                           "http://127.0.0.1:8002/v1")
    else:
        case.controller._emit("development_input", {
            "cycle_id": case.dev._id, "kind": "review",
            "role_report": {"findings": [{"id": "new-finding", "status": "open"}]},
        })
    with pytest.raises(IntegrityError):
        admission.guard()
    await admission.close()
    assert not case.controller._eligible


@pytest.mark.parametrize("point", ["before_release", "release_intent", "delivery"])
async def test_revocation_fences_release_and_delivery(case, monkeypatch, point):
    admission = admit(case)
    resource = started(admission)
    if point == "release_intent":
        original = admission._record

        def record(state, *args):
            original(state, *args)
            if state == "release_intent":
                case.controller._abort("revoked_at_release")

        monkeypatch.setattr(admission, "_record", record)
    elif point == "delivery":
        admission.release()
        case.controller._abort("revoked_at_delivery")
    else:
        case.controller._abort("revoked_before_release")
    with pytest.raises(IntegrityError):
        admission.guard() if point == "delivery" else admission.release()
    assert resource.releases == (1 if point == "delivery" else 0)
    await admission.close()


@pytest.mark.parametrize("method", ["start", "release"])
async def test_lifecycle_operations_are_one_use(case, method):
    admission = admit(case)
    resource = started(admission)
    if method == "release":
        admission.release()
    with pytest.raises(IntegrityError):
        if method == "start":
            admission.start(lambda owner: pytest.fail("second factory dispatched"))
        else:
            admission.release()
    await admission.close()
    assert resource.releases == (1 if method == "release" else 0)


async def test_release_failure_is_ambiguous_until_owned_stop(case):
    admission = admit(case)
    resource = started(admission)
    resource.release_error = OSError("release acknowledgement lost")
    with pytest.raises(OSError):
        admission.release()
    assert case.controller._development_pending
    await admission.close()
    failures = values(case.controller, "development_failure")
    assert failures[-1]["first_failure"]["error"] == "release acknowledgement lost"
    assert resource.verifications == 1


@pytest.mark.parametrize("wrong", ["namespace", "upstream", "run", "context", "raw"])
async def test_local_close_or_foreign_stop_cannot_release_ownership(case, wrong):
    admission = admit(case)
    resource = started(admission)
    admission.release()
    if wrong == "namespace":
        resource.stop = replace(resource.stop, namespace_stopped=False)
    elif wrong == "upstream":
        resource.stop = replace(resource.stop, upstream_stopped=False)
    elif wrong == "run":
        resource.stop = replace(
            resource.stop, run=replace(admission.run, run_id="other"),
        )
    elif wrong == "context":
        resource.stop = replace(resource.stop, authority_sha256="0" * 64)
    else:
        resource.stop = asdict(resource.stop)
    with pytest.raises(IntegrityError, match="unconfirmed"):
        await admission.close()
    assert case.dev._busy and case.controller._development_pending
    assert not admission.settled and not case.controller._eligible
    with pytest.raises(IntegrityError, match="cleanup"):
        case.controller.close()


async def test_async_close_failure_is_not_retried_or_called_settled(case):
    admission = admit(case)
    resource = started(admission)
    admission.release()
    resource.close_error = OSError("owned close failed")
    for _ in range(2):
        with pytest.raises(IntegrityError, match="unconfirmed"):
            await admission.close()
    assert resource.closes == 1
    assert resource.verifications == 1
    assert case.dev._busy and not admission.settled
    assert values(case.controller, "development_failure")[-1]["error"] == (
        "owned close failed"
    )


async def test_repeated_cancel_waits_for_owned_cleanup_and_final_delivery(case):
    admission = admit(case)
    resource = started(admission)
    admission.release()
    resource.entered, resource.finish = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(admission.close())
    await resource.entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert case.dev._busy and not task.done()
    resource.finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.settled and not case.dev._busy
    assert resource.closes == 1
    await admission.close()
    assert resource.closes == 1


async def test_late_failure_cannot_release_lease_at_final_delivery(case, monkeypatch):
    admission = admit(case)
    started(admission)
    admission.release()
    original = admission._finish
    entered, finish = threading.Event(), threading.Event()

    def finishing(stop):
        original(stop)
        entered.set()
        assert finish.wait(5), "test failed to release final delivery"

    monkeypatch.setattr(admission, "_finish", finishing)
    task = asyncio.create_task(admission.close())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        admission.fail(OSError("late evidence failure"))
    finally:
        finish.set()
    with pytest.raises(IntegrityError, match="revoked before delivery"):
        await task
    assert case.dev._busy and case.controller._development_pending


async def test_native_records_and_authority_are_journaled(case):
    admission = admit(case)
    started(admission)
    admission.release()
    await admission.close()
    records = case.controller.journal.verify()
    files = [f.content for r in records for f in r.files.files]
    assert admission.settings.authority in files
    assert EVIDENCE.files[0].content in files
    assert any("first_failure" in r.value["data"] for r in records
               if r.value["kind"] == "development_failure")


async def test_unbounded_guards_do_not_consume_work_or_call_quotas(case):
    admission = admit(case)
    started(admission)
    admission.release()
    case.clock.ns += 100 * 24 * 3600 * 1_000_000_000
    for _ in range(25):
        admission.guard()
    assert case.controller._eligible
    await admission.close()
    with pytest.raises(IntegrityError):
        case.dev.authorize("execute")


async def test_failure_accounting_fault_still_closes_but_retains_lease(
    case, monkeypatch,
):
    admission = admit(case)
    resource = started(admission)
    admission.release()
    original = case.controller.journal.append

    def append(kind, *args, **kwargs):
        if kind == "development_failure":
            raise OSError("failure archive unavailable")
        return original(kind, *args, **kwargs)

    monkeypatch.setattr(case.controller.journal, "append", append)
    with pytest.raises(IntegrityError, match="unconfirmed"):
        await admission.close()
    assert resource.closes == 1
    assert case.dev._busy and not case.controller._eligible


@pytest.mark.parametrize("point", ["factory", "reservation"])
async def test_pre_release_failure_has_no_running_resources(case, monkeypatch, point):
    if point == "reservation":
        original = case.controller._emit

        def emit(kind, data, *args):
            result = original(kind, data, *args)
            if data.get("mode") == "native_admission":
                case.clock.boot = "lost-after-reservation"
            return result

        monkeypatch.setattr(case.controller, "_emit", emit)
        with pytest.raises(IntegrityError):
            admit(case)
        assert not case.dev._busy and not case.controller._development_pending
    else:
        admission = admit(case)

        def factory(owner):
            raise OSError("inert factory failed")

        with pytest.raises(OSError):
            admission.start(factory)
        await admission.close()
        assert admission.settled and not case.dev._busy
