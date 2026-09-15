"""Host-local handoff tests: fake runtimes, no models, Docker or Calendar."""

import asyncio
import base64
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from recollect.selfmod.contracts import (
    File,
    Plan,
    PlannedChange,
    Requirement,
    Snapshot,
    TaskContract,
    Verification,
)
from recollect.selfmod.development import CheckResults, Review, Stage
from recollect.selfmod.executor import FixtureExecutor
from recollect.selfmod.integration import DevelopmentSettings
from recollect.selfmod.journal import IntegrityError, decode
from recollect.selfmod.round import ModificationRound
from tests.selfmod_containment_helpers import spec
from tests.selfmod_round_helpers import (
    EVIDENCE,
    FakeClock,
    Fault,
    round_config,
    submitted,
    values,
)
from tests.test_selfmod_executor import Runtime


@pytest.fixture
def case(tmp_path):
    fixture = spec()
    contract = TaskContract(
        "Change the fixture's allowed value", (
            Requirement("value", "value equals 2", "fixture evaluator"),
        ), ("unit", "regression"), fixture.policy.sha256,
    )
    plan = Plan(contract.sha256, (
        PlannedChange("editable.py", "modify", ("value",), "original task"),
    ), (Verification("value", "unit and independent evaluation"),))
    settings = DevelopmentSettings(
        fixture.image_id, fixture.image_environment, fixture.entrypoint,
    )
    clock, fault = FakeClock(), Fault()
    controller = ModificationRound.create(
        tmp_path / "controller", round_config(contract, fixture.baseline.sha256),
        clock=clock, fault=fault,
    )
    item = SimpleNamespace(
        controller=controller, clock=clock, fault=fault, fixture=fixture,
        plan=plan, settings=settings, root=tmp_path,
    )
    yield item
    controller.close()


def opened(case):
    return reopen(case)


def reopen(case):
    return case.controller.open_development(
        baseline=case.fixture.baseline, policy=case.fixture.policy,
        settings=case.settings,
    )


def review(dev, *, approved=True, blockers=()):
    grant = dev.authorize("review")
    report = Review(dev.binding, grant.actor_id, approved, blockers, EVIDENCE.sha256)
    dev.review(grant, report, EVIDENCE)


def planned(case):
    dev = opened(case)
    dev.propose(dev.authorize("plan"), case.plan)
    review(dev)
    return dev


def runtime(case, name="inputs", value=b"value = 2\n"):
    result = Runtime(case.root / name, case.clock)
    result.cancel = lambda: None

    def changed(report):
        snapshot = Snapshot(tuple(
            File(f.path, value if f.path == "editable.py" else f.content)
            for f in result.config.baseline.files
        ))
        report["snapshot_sha256"] = snapshot.sha256
        for file in report["files"]:
            if file["path"] == "editable.py":
                file["base64"] = base64.b64encode(value).decode()
        for entry in report["entries"]:
            if entry["path"] == "editable.py":
                entry["bytes"] = len(value)
        return report

    result.result_change = changed
    return result


def checks(dev, passed=True):
    grant = dev.authorize("checks")
    dev.checks(grant, CheckResults(
        dev.binding, (("unit", passed), ("regression", True)), EVIDENCE.sha256,
    ), EVIDENCE)


async def ready(case):
    dev = planned(case)
    await dev.execute(dev.authorize("execute"), runtime(case))
    checks(dev)
    review(dev)
    return dev


async def test_end_to_end_authenticated_fixture_spine_journals_the_candidate(case):
    dev = await ready(case)
    number, identity = dev.submit(dev.authorize("submit"))
    assert number == 1 and identity == case.controller.candidate.sha256
    candidate = submitted(case.controller)
    assert candidate["candidate/editable.py"] == b"value = 2\n"
    assert any(b'"kind":"snapshot_verified"' in b for b in candidate.values())
    assert any(b'"kind":"termination_verified"' in b for b in candidate.values())
    assert any(b'"kind":"plan"' in b for b in candidate.values())
    assert any(p.endswith("/report.json") for p in candidate)
    assert any(p.endswith("/plan.json") for p in candidate)
    for record in case.controller.journal.verify():
        if record.value["kind"] in {"development_input", "development_execution"}:
            for file in record.files.files:
                assert candidate[f"records/{record.anchor.sequence}/{file.path}"] == (
                    file.content
                )


async def test_integrated_release_and_admission_survive_days(case):
    dev = planned(case)
    worker = runtime(case)

    def advance(point):
        case.clock.ns += 86400 * 1_000_000_000

    worker.hook = advance
    await dev.execute(dev.authorize("execute"), worker)
    assert worker.deadline.monotonic_ns is None
    assert worker.config.timeout_ms is None
    assert decode(worker.release_bytes)["remaining_ms"] is None
    checks(dev)
    review(dev)
    assert dev.submit(dev.authorize("submit"))[0] == 1
    assert submitted(case.controller)["candidate/editable.py"] == b"value = 2\n"


def test_cannot_open_while_a_cycle_is_settling(case):
    case.controller._development_pending = True
    with pytest.raises(IntegrityError, match="settling"):
        reopen(case)
    case.controller._development_pending = False
    assert not values(case.controller, "development_opened")


@pytest.mark.parametrize("tamper", ["baseline", "policy", "profile"])
def test_open_rejects_unbound_frozen_inputs(case, tamper):
    baseline, policy = case.fixture.baseline, case.fixture.policy
    settings = case.settings
    if tamper == "baseline":
        baseline = EVIDENCE
    elif tamper == "policy":
        policy = replace(policy, modify=())
    else:
        settings = replace(settings, entrypoint="missing.py")
    with pytest.raises(ValueError):
        case.controller.open_development(baseline=baseline, policy=policy,
                                         settings=settings)


@pytest.mark.parametrize("replay", [False, True])
def test_equal_but_foreign_or_consumed_grants_rejected(case, replay):
    dev = opened(case)
    grant = dev.authorize("plan")
    if replay:
        dev.propose(grant, case.plan)
    else:
        grant = replace(grant)
    with pytest.raises(IntegrityError, match="grant"):
        dev.propose(grant, case.plan)
    with pytest.raises(IntegrityError, match="ineligible"):
        dev.authorize("plan")


def test_only_one_outstanding_handoff(case):
    dev = opened(case)
    dev.authorize("plan")
    with pytest.raises(IntegrityError, match="outstanding"):
        dev.authorize("plan")


@pytest.mark.parametrize("tamper", ["actor", "binding", "evidence"])
def test_review_authentication_and_exact_evidence(case, tamper):
    dev = opened(case)
    dev.propose(dev.authorize("plan"), case.plan)
    grant = dev.authorize("review")
    report = Review(dev.binding, grant.actor_id, True, (), EVIDENCE.sha256)
    if tamper == "actor":
        report = replace(report, reviewer_id="modifier-claims-reviewer")
    elif tamper == "binding":
        report = replace(report, binding=replace(dev.binding, revision=99))
    else:
        report = replace(report, evidence_sha256="f" * 64)
    with pytest.raises(ValueError):
        dev.review(grant, report, EVIDENCE)
    assert dev.stage == Stage.FORWARD_REVIEW


def test_rejected_review_requires_fresh_one_shot_handoff(case):
    dev = opened(case)
    dev.propose(dev.authorize("plan"), case.plan)
    review(dev, approved=False, blockers=("Unnecessary abstraction",))
    assert dev.stage == Stage.FORWARD_REVIEW
    review(dev)
    assert dev.stage == Stage.IMPLEMENT
    data = values(case.controller, "development_input")
    assert [x["report"]["approved"] for x in data if x["kind"] == "review"] == [
        False, True,
    ]


@pytest.mark.parametrize("existing", [False, True])
async def test_raw_submit_cannot_bypass_integration_or_idempotency(case, existing):
    dev = await ready(case)
    artifact = dev._receipt.snapshot
    if existing:
        dev.submit(dev.authorize("submit"))
    with pytest.raises(IntegrityError, match="Authenticated"):
        case.controller.submit(dev._id, artifact, EVIDENCE)


@pytest.mark.parametrize("stage", ["plan", "forward_review", "implement", "checks"])
async def test_no_submission_before_all_gates(case, stage):
    dev = opened(case)
    if stage != "plan":
        dev.propose(dev.authorize("plan"), case.plan)
    if stage in {"implement", "checks"}:
        review(dev)
    if stage == "checks":
        await dev.execute(dev.authorize("execute"), runtime(case))
    with pytest.raises(IntegrityError, match="stage"):
        dev.authorize("submit")
    assert not values(case.controller, "candidate_submitted")


async def test_failed_checks_allow_revision_but_do_not_inherit_approval(case):
    dev = planned(case)
    await dev.execute(dev.authorize("execute"), runtime(case))
    original = dev.binding
    checks(dev, False)
    assert dev.stage == Stage.IMPLEMENT
    await dev.execute(dev.authorize("execute"), runtime(case, "second"))
    assert dev.binding.artifact_sha256 == original.artifact_sha256
    assert dev.binding.revision > original.revision
    assert dev.stage == Stage.CHECKS
    checks(dev)
    review(dev)
    assert dev.submit(dev.authorize("submit"))[0] == 1


@pytest.mark.parametrize("point", ["prepare", "release", "collect", "terminate"])
async def test_failed_execution_journals_evidence_and_uncertainty(case, point):
    dev = planned(case)
    worker = runtime(case)

    def fail(name):
        if name == point:
            raise OSError("injected fixture failure")

    worker.hook = fail
    with pytest.raises(OSError):
        await dev.execute(dev.authorize("execute"), worker)
    assert "terminate" in worker.calls
    failure = values(case.controller, "development_failure")[-1]
    assert failure["capture_complete"]
    assert failure["termination_confirmed"] is (point != "terminate")
    archived = [f.content for r in case.controller.journal.verify()
                if r.value["kind"] == "development_failure" for f in r.files.files]
    assert any(b'"kind":"fixture_failure_accounted"' in b for b in archived)
    assert not values(case.controller, "candidate_submitted")
    assert not case.controller.eligible


@pytest.mark.parametrize("point", [
    "journal.after_readback:development_consumed",
    "journal.after_readback:development_execution",
    "journal.after_readback:development_transition",
])
async def test_round_persistence_fault_blocks_submission(case, point):
    dev = planned(case)
    grant = dev.authorize("execute")
    case.fault.at = point
    with pytest.raises(IntegrityError, match="accounting unconfirmed"):
        await dev.execute(grant, runtime(case))
    assert not case.controller._eligible
    assert not case.controller._development_pending


async def test_invalid_plan_delta_never_enters_development(case):
    dev = planned(case)
    worker = runtime(case, value=b"value = 1\n")
    with pytest.raises(ValueError, match="Actual changes"):
        await dev.execute(dev.authorize("execute"), worker)
    assert not values(case.controller, "candidate_submitted")
    assert not case.controller.eligible


@pytest.mark.parametrize("clock_fault", ["rollback", "boot"])
async def test_clock_fault_at_release_prevents_worker_release(case, clock_fault):
    dev = planned(case)
    worker = runtime(case)

    def hook(name):
        if name == "read_inputs":
            if clock_fault == "rollback":
                case.clock.ns -= 1
            else:
                case.clock.boot = "foreign-process"

    worker.hook = hook
    with pytest.raises(IntegrityError):
        await dev.execute(dev.authorize("execute"), worker)
    assert "release" not in worker.calls
    assert values(case.controller, "development_failure")


async def test_revoked_controller_blocks_physical_release(case):
    dev = planned(case)
    worker = runtime(case)

    def hook(name):
        if name == "read_inputs":
            with case.controller._lock:
                case.controller._abort("host revoked")

    worker.hook = hook
    with pytest.raises(IntegrityError):
        await dev.execute(dev.authorize("execute"), worker)
    assert "release" not in worker.calls


async def test_old_cycle_fenced_after_a_new_cycle_opens(case):
    dev = await ready(case)
    dev.submit(dev.authorize("submit"))
    current = reopen(case)
    assert current._id != dev._id
    with pytest.raises(IntegrityError, match="stale development"):
        dev.authorize("plan")


async def test_cycle_replacement_keeps_audit_counts_without_action_quotas(case):
    dev = await ready(case)
    dev.submit(dev.authorize("submit"))
    before = (case.controller._development_updates,
              case.controller._development_reviews)
    current = reopen(case)
    assert (case.controller._development_updates,
            case.controller._development_reviews) == before
    for _ in range(12):
        current.propose(current.authorize("plan"), case.plan)
        review(current, approved=False)
    assert case.controller._development_updates == before[0] + 12
    assert case.controller._development_reviews == before[1] + 12
    assert current.stage == Stage.FORWARD_REVIEW
    assert len(values(case.controller, "candidate_submitted")) == 1


async def test_replacement_settings_cannot_change(case):
    dev = await ready(case)
    dev.submit(dev.authorize("submit"))
    case.settings = replace(case.settings, image_id="sha256:" + "a" * 64)
    with pytest.raises(IntegrityError, match="settings changed"):
        reopen(case)


async def wait(event):
    assert await asyncio.to_thread(event.wait, 5), "integration barrier timed out"


async def test_cancellation_during_import_is_terminal_and_accounted(case):
    dev = planned(case)
    entered, released = threading.Event(), threading.Event()

    def barrier(point):
        if point == "journal.after_readback:development_execution" and dev._executor:
            entered.set()
            assert released.wait(5)

    case.fault.callback = barrier
    task = asyncio.create_task(dev.execute(dev.authorize("execute"), runtime(case)))
    await wait(entered)
    task.cancel()
    task.cancel()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not case.controller._eligible
    assert not case.controller._development_pending


async def test_busy_execution_prevents_close_and_concurrent_execution(case):
    dev = planned(case)
    entered, released = threading.Event(), threading.Event()
    worker = runtime(case)

    def barrier(name):
        if name == "collect":
            entered.set()
            assert released.wait(5)

    worker.hook = barrier
    grant = dev.authorize("execute")
    task = asyncio.create_task(dev.execute(grant, worker))
    await wait(entered)
    with pytest.raises(IntegrityError, match="cleanup"):
        case.controller.close()
    with pytest.raises(IntegrityError):
        await dev.execute(grant, runtime(case, "foreign"))
    assert case.controller._development_pending
    released.set()
    with pytest.raises(IntegrityError):
        await task
    assert not case.controller._development_pending
    assert "terminate" in worker.calls


@pytest.mark.parametrize("tamper", [
    "diagnostic", "run", "snapshot", "anchor", "primary_failed", "journal_head",
    "clock_rollback", "spec_binding",
])
async def test_executor_handoff_rejects_untrusted_results(case, monkeypatch, tamper):
    dev = planned(case)
    original = FixtureExecutor.run_async

    async def corrupted(executor, worker):
        receipt = await original(executor, worker)
        if tamper == "diagnostic":
            return replace(receipt, diagnostic_only=True)
        if tamper == "run":
            return replace(receipt, run_id="f" * 32)
        if tamper == "snapshot":
            return replace(receipt, snapshot=EVIDENCE)
        if tamper == "anchor":
            return replace(receipt, archive_anchor=replace(receipt.archive_anchor,
                                                          sha256="f" * 64))
        if tamper == "primary_failed":
            executor._failed = True
        elif tamper == "journal_head":
            executor._record("foreign_append", {})
        elif tamper == "clock_rollback":
            case.clock.ns -= 1
        elif tamper == "spec_binding":
            executor._spec = replace(executor.spec, binding=replace(
                executor.spec.binding, revision=99,
            ))
        return receipt

    monkeypatch.setattr(FixtureExecutor, "run_async", corrupted)
    with pytest.raises(IntegrityError):
        await dev.execute(dev.authorize("execute"), runtime(case))
    assert not values(case.controller, "candidate_submitted")
    assert not case.controller._eligible


async def test_full_diagnostic_refresh_cannot_enter_primary_handoff(case, monkeypatch):
    dev = planned(case)
    original = FixtureExecutor.run_async

    async def diagnostic(executor, worker):
        await original(executor, worker)
        executor._state = "failed"
        executor._max_refreshes = 1
        executor._account_cancel()
        grant = executor.authorize_refresh()
        executor.refresh(grant)
        return await original(executor, worker)

    monkeypatch.setattr(FixtureExecutor, "run_async", diagnostic)
    with pytest.raises(IntegrityError):
        await dev.execute(dev.authorize("execute"), runtime(case))
    assert not case.controller._eligible
    assert values(case.controller, "development_failure")


async def test_cancellation_during_runtime_waits_for_stop_and_failure_archive(case):
    dev = planned(case)
    worker = runtime(case)
    entered, cancelled, stopping, released = (threading.Event() for _ in range(4))
    worker.cancel = cancelled.set

    def hook(name):
        if name == "collect":
            entered.set()
            assert cancelled.wait(5)
            raise InterruptedError("cancelled fixture")
        if name == "terminate":
            stopping.set()
            assert released.wait(5)

    worker.hook = hook
    task = asyncio.create_task(dev.execute(dev.authorize("execute"), worker))
    await wait(entered)
    task.cancel()
    await wait(stopping)
    task.cancel()
    assert not task.done()
    assert case.controller._development_pending
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert values(case.controller, "development_failure")[-1]["termination_confirmed"]
    assert not case.controller._development_pending


async def test_close_failure_after_import_is_terminal(case, monkeypatch):
    dev = planned(case)
    original = FixtureExecutor.close

    def broken(executor):
        original(executor)
        raise OSError("close failed")

    monkeypatch.setattr(FixtureExecutor, "close", broken)
    with pytest.raises(OSError, match="close failed"):
        await dev.execute(dev.authorize("execute"), runtime(case))
    assert not case.controller._eligible
    assert not case.controller._development_pending


@pytest.mark.parametrize("point", [
    "journal.before_commit:development_input",
    "journal.after_readback:development_transition",
])
def test_review_archive_failure_cannot_grant_execution(case, point):
    dev = opened(case)
    dev.propose(dev.authorize("plan"), case.plan)
    grant = dev.authorize("review")
    case.fault.at = point
    with pytest.raises(OSError):
        dev.review(grant, Review(dev.binding, grant.actor_id, True, (),
                                EVIDENCE.sha256), EVIDENCE)
    with pytest.raises(IntegrityError, match="ineligible"):
        dev.authorize("execute")


async def test_check_inventory_must_be_exact(case):
    dev = planned(case)
    await dev.execute(dev.authorize("execute"), runtime(case))
    grant = dev.authorize("checks")
    with pytest.raises(ValueError, match="inventory"):
        dev.checks(grant, CheckResults(dev.binding, (("unit", True),),
                                       EVIDENCE.sha256), EVIDENCE)
    assert not case.controller._eligible


@pytest.mark.parametrize("stage", ["plan", "ready", "running"])
async def test_returned_facade_is_not_raw_submission_authority(case, stage):
    task = None
    released = threading.Event()
    if stage == "plan":
        dev = opened(case)
    elif stage == "ready":
        dev = await ready(case)
    else:
        dev = planned(case)
        entered = threading.Event()
        worker = runtime(case)

        def barrier(name):
            if name == "collect":
                entered.set()
                assert released.wait(5)

        worker.hook = barrier
        task = asyncio.create_task(dev.execute(dev.authorize("execute"), worker))
        await wait(entered)
    with pytest.raises(IntegrityError, match="Authenticated"):
        case.controller.submit("bypass", EVIDENCE, EVIDENCE, _development=dev)
    assert not values(case.controller, "candidate_submitted")
    if task:
        released.set()
        with pytest.raises(IntegrityError):
            await task


async def test_lease_stays_busy_until_final_async_delivery(case, monkeypatch):
    dev = planned(case)
    entered, released = threading.Event(), threading.Event()
    original = dev._close_execution

    def paused(lease):
        original(lease)
        entered.set()
        assert released.wait(5)

    monkeypatch.setattr(dev, "_close_execution", paused)
    task = asyncio.create_task(dev.execute(dev.authorize("execute"), runtime(case)))
    await wait(entered)
    task.cancel()
    assert case.controller._development_pending
    with pytest.raises(IntegrityError, match="cleanup"):
        case.controller.close()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not case.controller._development_pending
    assert values(case.controller, "development_failure")[-1]["capture_complete"]
    assert not case.controller.eligible


async def test_repeated_cancellation_cannot_hide_accounting_failure(case):
    dev = planned(case)
    entered, released = threading.Event(), threading.Event()
    worker = runtime(case)

    def fail(name):
        if name == "collect":
            raise OSError("fixture failure")

    def barrier(point):
        if point == "journal.before_commit:development_failure":
            entered.set()
            assert released.wait(5)

    worker.hook = fail
    case.fault.callback = barrier
    case.fault.at = "journal.before_commit:development_failure"
    task = asyncio.create_task(dev.execute(dev.authorize("execute"), worker))
    await wait(entered)
    task.cancel()
    task.cancel()
    released.set()
    with pytest.raises(IntegrityError, match="accounting unconfirmed"):
        await task
    assert not case.controller._eligible
    assert not case.controller._development_pending


async def test_late_execution_call_cannot_reopen_a_closed_round(case):
    dev = opened(case)
    grant = dev.authorize("plan")
    case.controller.close()
    anchor = case.controller.journal.head
    with pytest.raises(IntegrityError, match="closed"):
        await dev.execute(grant, runtime(case))
    assert case.controller.journal.head == anchor
    assert case.controller._phase == "closed"


async def test_cross_loop_release_cannot_clear_a_new_execution_lease(case, monkeypatch):
    dev = planned(case)
    split, release_old, consumed, collecting, release_new = (
        threading.Event() for _ in range(5)
    )
    assign = type(dev).__setattr__

    def split_assignment(instance, name, value):
        assign(instance, name, value)
        if (instance is dev and name == "_busy" and value is False
                and not split.is_set()):
            split.set()
            assert release_old.wait(5)

    monkeypatch.setattr(type(dev), "__setattr__", split_assignment)
    grant = dev.authorize("execute")
    worker = runtime(case)
    old = asyncio.create_task(asyncio.to_thread(
        lambda: asyncio.run(dev.execute(grant, worker)),
    ))
    await wait(split)
    old_lease = dev._lease
    consume = dev._consume

    def observed(grant, action):
        consume(grant, action)
        consumed.set()

    monkeypatch.setattr(dev, "_consume", observed)
    next_worker = runtime(case, "next", b"value = 3\n")

    def hold(name):
        if name == "collect":
            collecting.set()
            assert release_new.wait(5)

    next_worker.hook = hold
    new = asyncio.create_task(dev.execute(dev.authorize("execute"), next_worker))
    await wait(consumed)
    assert case.controller._development_pending
    assert "prepare" not in next_worker.calls
    release_old.set()
    await old
    await wait(collecting)
    assert dev._lease is not old_lease
    assert case.controller._development_pending
    with pytest.raises(IntegrityError, match="cleanup"):
        case.controller.close()
    release_new.set()
    await new
    assert not case.controller._development_pending
