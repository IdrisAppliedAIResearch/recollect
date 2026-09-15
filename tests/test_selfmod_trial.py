"""Unattended CP0-CP6 sequencing against scripted host collectors (no services)."""

import asyncio
import json

import pytest

from recollect.selfmod.candidate_evaluator import EvaluationResult
from recollect.selfmod.checkpoints import (
    inspect_accounting_chain,
    verify_checkpoint_chain,
)
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.controller import (
    ACTIVATION_CHECKS,
    OUTCOME_CHECKS,
    PREFLIGHT_CHECKS,
    Controller,
)
from recollect.selfmod.trial import (
    Activation,
    TargetOutcome,
    TrialOrchestrator,
    classify,
)
from tests.selfmod_checkpoint_helpers import EVIDENCE, FakeClock, Fault, config, good

GAP = {"type": "capability_gap", "missing_capability": "calendar events",
       "attempted": ["listed tools"], "modification_request": "add event creation",
       "task_id": "task-a", "message_id": "m1", "revision": 1,
       "related_message_id": "request-1"}


def outcome(**changes):
    values = dict(task_id="task-a", request_message_id="request-1", report=GAP,
                  claimed_success=False, unknown_effects=False, same_identity=True,
                  evidence=EVIDENCE)
    return TargetOutcome(**{**values, **changes})


class Script:
    """Records calls; each hook can be replaced by a test."""

    def __init__(self):
        self.calls, self.preflight_checks = [], good(PREFLIGHT_CHECKS)
        self.target, self.empty, self.quiescent = outcome(), True, True
        self.evaluations = [dict(checks=good(config().evaluation_checks),
                                 host_failure_proven=False)]
        self.activated, self.outcome_checks = True, good(OUTCOME_CHECKS)
        self.developed = 0
        self.fail_during = None
        self.probe_release = asyncio.Event()
        self.probe_release.set()

    def hit(self, name, *args):
        self.calls.append(name)
        if self.fail_during == name:
            raise RuntimeError("injected host failure in " + name)

    async def preflight(self):
        self.hit("preflight")
        return self.preflight_checks, EVIDENCE

    async def run_baseline(self):
        self.hit("run_baseline")
        return self.target

    async def stop_target(self, task_id):
        self.hit("stop_target")
        return self.quiescent

    async def calendar_empty(self):
        self.hit("calendar_empty")
        return self.empty, Snapshot((File("read.json", b"{}"),))

    async def close_target_gate(self, reason):
        self.hit("close_target_gate")

    async def start_workload(self):
        self.hit("start_workload")

    async def probe(self, point):
        self.hit("probe:" + point)
        await self.probe_release.wait()
        return {"point": point, "answer": "42", "correct": True}

    async def develop(self, controller, target, feedback, on_first_generation):
        self.hit("develop")
        await on_first_generation()
        self.developed += 1
        self.feedback = feedback
        artifact = Snapshot((File("tool.py", b"candidate %d\n" % self.developed),))
        await asyncio.to_thread(controller.dispatch, "modify")
        await asyncio.to_thread(controller.submit, f"s{self.developed}", artifact,
                                EVIDENCE)
        return artifact

    async def evaluate(self, candidate):
        self.hit("evaluate")
        spec = self.evaluations.pop(0) if len(self.evaluations) > 1 else (
            self.evaluations[0])
        self.last_candidate = candidate
        return EvaluationResult(spec["checks"], EVIDENCE, spec["host_failure_proven"])

    async def activate(self, controller, evaluation, target):
        self.hit("activate")
        checks = good(ACTIVATION_CHECKS)
        checks["health_passed"] = self.activated
        await asyncio.to_thread(controller.dispatch, "activate")
        await asyncio.to_thread(controller.activated, self.last_candidate.sha256,
                                checks, EVIDENCE)
        return Activation("continuation-1")

    async def verify_outcome(self, activation, probes):
        self.hit("verify_outcome")
        self.probes = probes
        return self.outcome_checks, EVIDENCE

    async def finish(self, failure):
        self.hit("finish")
        self.failure = failure
        return Snapshot((File("cleanup.json", b"{}"),))


@pytest.fixture
def run(tmp_path):
    controllers = []

    def execute(script):
        controller = Controller.create(tmp_path / f"attempt-{len(controllers)}",
                                       config(), clock=FakeClock(), fault=Fault(),
                                       mode="primary")
        controllers.append(controller)
        asyncio.run(TrialOrchestrator(controller, script).run())
        return controller

    yield execute
    for controller in controllers:
        controller.close()


def cp6_files(controller, checkpoint):
    """Exact bytes of a sealed bundle, read back from its preparation record."""
    record = next(r for r in controller.journal.verify()
                  if r.anchor.sequence == checkpoint.prepare_sequence)
    return record.files.files


def chain(controller):
    records = controller.journal.verify()
    identity = dict(attempt_id=config().attempt_id, registrations=config().hashes,
                    artifacts=config().artifacts)
    # A failed attempt ends in an accounting branch, which only accounting
    # inspection of the attempt's own mode may extend with CP6.
    inspected = inspect_accounting_chain(controller.journal.root, records,
                                         **identity, mode="primary")
    assert not inspected.issues
    ids = [c.value["checkpoint_id"] for c in inspected.checkpoints]
    if ids and controller._checkpoints[-1].value["observations"].get(
            "result") == "primary_complete":
        verify_checkpoint_chain(controller.journal.root, records, **identity)
    summary = controller._checkpoints[-1].value["observations"]
    return ids, summary


def test_unattended_primary_chain_completes_with_concurrent_probes(run):
    script = Script()
    controller = run(script)
    ids, summary = chain(controller)
    assert ids == ["CP0", "CP1", "CP2.1", "CP3.1", "CP4", "CP5", "CP6"]
    assert summary["result"] == "primary_complete"
    assert script.calls.count("start_workload") == 1
    assert set(script.probes) == {"first_modifier_generation",
                                  "first_candidate_evaluation"}
    assert all(p["correct"] for p in script.probes.values())
    assert script.calls.index("close_target_gate") < script.calls.index("develop")
    assert script.calls[-1] == "finish" and script.failure is None
    cp6 = controller._checkpoints[-1]
    assert "trial/orchestration.json" in {f["path"] for f in cp6.value["files"]}


def test_failed_preflight_never_submits_the_request(run):
    script = Script()
    script.preflight_checks = {**good(PREFLIGHT_CHECKS), "baseline_empty": False}
    controller = run(script)
    ids, summary = chain(controller)
    assert ids == ["CP0", "CP6"] and summary["result"] == "primary_failed"
    assert "run_baseline" not in script.calls


@pytest.mark.parametrize(("change", "label"), [
    (dict(target=outcome(claimed_success=True)), "false_success"),
    (dict(empty=False), "already_capable"),
    (dict(target=outcome(report=None)), "no_gap_trigger"),
    (dict(target=outcome(task_id=None)), "no_gap_trigger"),
    (dict(empty=None), "environment_blocked"),
    (dict(target=outcome(unknown_effects=True)), "environment_blocked"),
])
def test_baseline_classification_disqualifies_and_accounts(run, change, label):
    script = Script()
    for key, value in change.items():
        setattr(script, key, value)
    controller = run(script)
    ids, summary = chain(controller)
    assert ids == ["CP0", "CP1", "CP6"] and summary["result"] == "primary_failed"
    cp1 = controller._checkpoints[1].value["observations"]
    assert cp1["classification"] == label and "develop" not in script.calls


def test_unquiescent_target_blocks_modification_even_with_a_gap_report(run):
    script = Script()
    script.quiescent = False
    controller = run(script)
    ids, _ = chain(controller)
    assert ids == ["CP0", "CP1", "CP6"] and "develop" not in script.calls
    assert controller._checkpoints[1].value["observations"]["checks"][
        "target_quiescent"] is False


def test_rejected_candidate_gets_feedback_and_a_changed_second_submission(run):
    script = Script()
    rejected = {**good(config().evaluation_checks), "fixture": False}
    script.evaluations = [dict(checks=rejected, host_failure_proven=False),
                          dict(checks=good(config().evaluation_checks),
                               host_failure_proven=False)]
    controller = run(script)
    ids, summary = chain(controller)
    assert ids == ["CP0", "CP1", "CP2.1", "CP3.1", "CP2.2", "CP3.2", "CP4", "CP5",
                   "CP6"]
    assert summary["result"] == "primary_complete"
    assert script.developed == 2 and script.feedback.checks == rejected
    assert script.calls.count("probe:first_candidate_evaluation") == 1


def test_three_rejections_exhaust_candidates(run):
    script = Script()
    script.evaluations = [dict(checks={**good(config().evaluation_checks),
                                       "regression": False},
                               host_failure_proven=False)]
    controller = run(script)
    ids, summary = chain(controller)
    assert ids[-1] == "CP6" and ids.count("CP3.1") == 1 and "CP3.3" in ids
    assert "candidate_limit_exhausted" in summary["reasons"]
    assert "activate" not in script.calls


def test_one_infrastructure_retest_reruns_the_unchanged_candidate(run):
    script = Script()
    unrun = dict.fromkeys(config().evaluation_checks)
    script.evaluations = [dict(checks=unrun, host_failure_proven=True),
                          dict(checks=good(config().evaluation_checks),
                               host_failure_proven=False)]
    controller = run(script)
    ids, summary = chain(controller)
    assert ids[:5] == ["CP0", "CP1", "CP2.1", "CP3.1", "CP3.1.retry1"]
    assert script.developed == 1 and summary["result"] == "primary_complete"


def test_assertion_failure_is_never_an_infrastructure_exception(run):
    script = Script()
    failed = {**dict.fromkeys(config().evaluation_checks), "fixture": False}
    script.evaluations = [dict(checks=failed, host_failure_proven=True),
                          dict(checks=good(config().evaluation_checks),
                               host_failure_proven=False)]
    controller = run(script)
    ids, _ = chain(controller)
    assert "CP3.1.retry1" not in ids and "CP2.2" in ids


def test_failed_activation_never_continues(run):
    script = Script()
    script.activated = False
    controller = run(script)
    ids, summary = chain(controller)
    assert ids == ["CP0", "CP1", "CP2.1", "CP3.1", "CP4", "CP6"]
    assert "activation_failed" in summary["reasons"]
    assert "verify_outcome" not in script.calls


def test_failed_outcome_check_is_primary_failure(run):
    script = Script()
    script.outcome_checks = {**good(OUTCOME_CHECKS), "exactly_one_event": False}
    controller = run(script)
    ids, summary = chain(controller)
    assert ids[-2:] == ["CP5", "CP6"] and "outcome_failed" in summary["reasons"]


@pytest.mark.parametrize("phase", ["run_baseline", "develop", "evaluate",
                                   "verify_outcome"])
def test_host_failure_anywhere_is_recorded_accounted_and_cleaned_up(run, phase):
    script = Script()
    script.fail_during = phase
    controller = run(script)
    ids, summary = chain(controller)
    assert ids[-1] == "CP6" and summary["result"] == "primary_failed"
    assert "trial_failure:RuntimeError" in summary["reasons"]
    assert isinstance(script.failure, RuntimeError) and script.calls[-1] == "finish"
    cp6 = controller._checkpoints[-1]
    log = json.loads(next(f.content for f in cp6_files(controller, cp6)
                          if f.path == "trial/orchestration.json"))
    assert any(entry["event"] == "failure" for entry in log)


def test_orchestrator_refuses_a_simulation_controller(tmp_path):
    controller = Controller.create(tmp_path / "sim", config(), clock=FakeClock())
    try:
        with pytest.raises(ValueError, match="primary"):
            TrialOrchestrator(controller, Script())
    finally:
        controller.close()


def test_classification_order_prefers_independent_provider_facts():
    assert classify(outcome(claimed_success=True, report=None), False) == (
        "already_capable")
    assert classify(outcome(), True) == "unsupported_and_reported"
