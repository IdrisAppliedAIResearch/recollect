from dataclasses import replace

import pytest

from recollect.selfmod.checkpoints import verify_checkpoint_chain
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.controller import (
    ACTIVATION_CHECKS,
    BASELINE_CHECKS,
    OUTCOME_CHECKS,
    PREFLIGHT_CHECKS,
    Controller,
)
from recollect.selfmod.journal import IntegrityError, read_archive
from tests.selfmod_checkpoint_helpers import (
    ARTIFACT,
    EVIDENCE,
    FakeClock,
    Fault,
    config,
    create,
    evaluate,
    finish,
    good,
    submit,
    through_activation,
    through_baseline,
    values,
)


@pytest.fixture
def controller(tmp_path):
    instance = create(tmp_path / "attempt")
    yield instance
    instance.close()


def summary(controller):
    return controller._checkpoints[-1].value["observations"]


def test_full_simulated_chain_and_exact_original_request(controller):
    through_activation(controller)
    cp6 = finish(controller)
    records = read_archive(controller.journal.root, controller.journal.head)
    chain = verify_checkpoint_chain(
        controller.journal.root,
        records,
        attempt_id=config().attempt_id,
        registrations=config().hashes,
        artifacts=config().artifacts,
    )
    assert [c.value["checkpoint_id"] for c in chain] == [
        "CP0",
        "CP1",
        "CP2.1",
        "CP3.1",
        "CP4",
        "CP5",
        "CP6",
    ]
    assert cp6 == chain[-1].manifest_sha256
    assert summary(controller)["result"] == "simulation_complete"
    assert summary(controller)["unreached"] == []
    request = next(r for r in records if r.value["kind"] == "request_received")
    assert request.files.files[0].content == config().contract.original_request.encode()
    for checkpoint in chain:
        assert checkpoint.value["registrations"] == config().hashes
        assert not {"manifest.json", "manifest.sha256"} & {
            f["path"] for f in checkpoint.value["files"]
        }
    cp5 = chain[-2].manifest_sha256
    kinds = [r.value["kind"] for r in records]
    receipt_seq = next(
        r.anchor.sequence
        for r in records
        if r.value["kind"] == "verification_receipt"
        and r.value["data"]["manifest_sha256"] == cp5
    )
    assert receipt_seq < kinds.index("endpoint_marker") + 1
    assert kinds.index("endpoint_marker") < kinds.index("endpoint_observed")
    assert cp6 not in summary(controller)["checkpoints"]


@pytest.mark.parametrize(
    "failure", ["false_success", "not_quiescent", "unknown_effect"]
)
def test_baseline_mixed_or_unknown_outcome_never_releases_modifier(controller, failure):
    controller.preflight(good(PREFLIGHT_CHECKS), EVIDENCE)
    controller.receive_request()
    controller.dispatch("baseline")
    checks = good(BASELINE_CHECKS)
    checks[
        {
            "false_success": "no_false_success",
            "not_quiescent": "target_quiescent",
            "unknown_effect": "no_unknown_effects",
        }[failure]
    ] = False
    controller.baseline("unsupported_and_reported", checks, EVIDENCE)
    with pytest.raises(IntegrityError, match="ineligible"):
        controller.dispatch("modify")
    controller.account()
    assert summary(controller)["result"] == "simulation_failed"
    assert values(controller, "candidate_accepted") == []


def test_failed_preflight_does_not_start_request_clock(controller):
    controller.preflight({}, EVIDENCE)
    with pytest.raises(IntegrityError):
        controller.receive_request()
    controller.account()
    assert not values(controller, "request_received")
    assert summary(controller)["candidate_count"] == 0


def test_empty_evidence_is_not_a_pass(controller):
    controller.preflight(good(PREFLIGHT_CHECKS), Snapshot(()))
    controller.account()
    assert summary(controller)["result"] == "simulation_failed"


def test_required_dispatch_is_consumed_only_once(controller):
    through_baseline(controller)
    controller.dispatch("modify")
    with pytest.raises(IntegrityError, match="already consumed"):
        controller.dispatch("modify")
    assert (
        len(
            [
                v
                for v in values(controller, "dispatch_consumed")
                if v["action"] == "modify"
            ]
        )
        == 1
    )


def test_out_of_order_call_fails_closed(controller):
    with pytest.raises(IntegrityError, match="Expected activate"):
        controller.dispatch("activate")
    with pytest.raises(IntegrityError, match="ineligible"):
        controller.preflight(good(PREFLIGHT_CHECKS), EVIDENCE)


def test_idempotent_submission_returns_receipt_without_dispatch(controller):
    through_baseline(controller)
    accepted = submit(controller)
    head = controller.journal.head
    assert controller.submit("submission-1", ARTIFACT, EVIDENCE) == accepted
    assert controller.journal.head == head
    assert len(values(controller, "candidate_accepted")) == 1
    assert not [
        v for v in values(controller, "dispatch_consumed") if v["action"] == "evaluate"
    ]


def test_conflicting_submission_id_is_terminal(controller):
    through_baseline(controller)
    submit(controller)
    with pytest.raises(IntegrityError, match="Conflicting"):
        controller.submit(
            "submission-1", Snapshot((File("x", b"different"),)), EVIDENCE
        )
    with pytest.raises(IntegrityError):
        controller.dispatch("evaluate")


def test_candidate_rejection_preserved_and_third_candidate_can_finish(controller):
    through_baseline(controller)
    for number in (1, 2, 3):
        artifact = Snapshot((File("extension.py", str(number).encode()),))
        submit(controller, number, artifact)
        evaluate(controller, None if number == 3 else {"fixture": False})
    controller.dispatch("activate")
    controller.activated(artifact.sha256, good(ACTIVATION_CHECKS), EVIDENCE)
    finish(controller)
    assert summary(controller)["candidate_count"] == 3
    assert summary(controller)["result"] == "simulation_complete"
    cp3 = [
        c.value
        for c in controller._checkpoints
        if c.value["checkpoint_id"].startswith("CP3")
    ]
    assert [c["observations"]["outcome"] for c in cp3] == [
        "candidate_rejected",
        "candidate_rejected",
        "accepted",
    ]


def test_three_rejections_never_accept_a_fourth(controller):
    through_baseline(controller)
    for number in (1, 2, 3):
        submit(controller, number, Snapshot((File("x", str(number).encode()),)))
        evaluate(controller, {})
    with pytest.raises(IntegrityError):
        submit(controller, 4)
    controller.account()
    assert summary(controller)["candidate_count"] == 3


def test_rejected_bytes_cannot_return_after_intervening_candidate(controller):
    through_baseline(controller)
    submit(controller)
    evaluate(controller, {})
    submit(controller, 2, Snapshot((File("x", b"different"),)))
    evaluate(controller, {})
    with pytest.raises(IntegrityError, match="identical candidate"):
        submit(controller, 3, ARTIFACT)
    controller.account()
    assert summary(controller)["candidate_count"] == 3
    assert (
        len(
            [
                v
                for v in values(controller, "dispatch_consumed")
                if v["action"] == "evaluate"
            ]
        )
        == 2
    )


@pytest.mark.parametrize("partial_checks", [{}, {"fixture": True}])
def test_single_infrastructure_retest_links_same_candidate(controller, partial_checks):
    through_baseline(controller)
    submit(controller)
    evaluate(controller, partial_checks, host_failure_proven=True)
    evaluate(controller)
    controller.dispatch("activate")
    controller.activated(ARTIFACT.sha256, good(ACTIVATION_CHECKS), EVIDENCE)
    finish(controller)
    assert summary(controller)["result"] == "simulation_complete"
    assert len(values(controller, "infrastructure_retest_consumed")) == 1
    names = [c.value["checkpoint_id"] for c in controller._checkpoints]
    assert names[3:6] == ["CP3.1", "CP3.1.retry1", "CP4"]


@pytest.mark.parametrize("partial_checks", [{}, {"fixture": True}])
def test_second_infrastructure_error_across_candidates_is_terminal(
    controller, partial_checks
):
    through_baseline(controller)
    submit(controller)
    evaluate(controller, {}, host_failure_proven=True)
    evaluate(controller, {})
    submit(controller, 2, Snapshot((File("x", b"different"),)))
    with pytest.raises(IntegrityError, match="second_infrastructure"):
        evaluate(controller, partial_checks, host_failure_proven=True)
    controller.account()
    assert summary(controller)["result"] == "simulation_failed"
    assert len(values(controller, "infrastructure_retest_consumed")) == 1


def test_infrastructure_claim_cannot_hide_assertions_or_missing_capture(controller):
    through_baseline(controller)
    submit(controller)
    evaluate(controller, {"fixture": False}, host_failure_proven=True)
    assert not values(controller, "infrastructure_retest_consumed")
    submit(controller, 2, Snapshot((File("x", b"different"),)))
    with pytest.raises(IntegrityError, match="evidence_loss"):
        evaluate(controller, {}, capture_complete=False)
    assert values(controller, "evaluation_incomplete")


@pytest.mark.parametrize("failure", ["wrong_digest", "event_before_activation"])
def test_activation_failure_never_releases_original_task(controller, failure):
    through_baseline(controller)
    submit(controller)
    evaluate(controller)
    controller.dispatch("activate")
    checks = good(ACTIVATION_CHECKS)
    if failure == "event_before_activation":
        checks["baseline_empty"] = False
    controller.activated(
        "0" * 64 if failure == "wrong_digest" else ARTIFACT.sha256, checks, EVIDENCE
    )
    with pytest.raises(IntegrityError):
        controller.dispatch("continue")
    controller.account()
    assert summary(controller)["result"] == "simulation_failed"


@pytest.mark.parametrize(
    "phase,late",
    [
        ("checkpoint.after_receipt:CP5", True),
        ("journal.after_commit:endpoint_marker", True),
        ("journal.after_readback:endpoint_marker", True),
        ("journal.after_readback:endpoint_marker", False),
    ],
)
def test_endpoint_includes_receipt_and_marker_completion(tmp_path, phase, late):
    clock, fault = FakeClock(), Fault()
    controller = create(tmp_path / "attempt", clock, fault)
    try:
        through_activation(controller)
        controller.dispatch("continue")
        start = values(controller, "request_start_observed")[0]["started"][
            "monotonic_ns"
        ]
        clock.ns = start + 3599 * 1_000_000_000

        def delay(point):
            if point == phase:
                clock.ns = start + (3601 if late else 3600) * 1_000_000_000

        fault.callback = delay
        controller.outcome(good(OUTCOME_CHECKS), EVIDENCE)
        observed = values(controller, "endpoint_observed")[0]
        assert observed["endpoint"]["monotonic_ns"] == clock.ns
        assert observed["elapsed_ns"] == (3601 if late else 3600) * 1_000_000_000
        assert observed["timing_policy"] == "observational"
        fault.callback = None
        clock.ns += 100 * 1_000_000_000
        controller.account()
        assert summary(controller)["result"] == "simulation_complete"
    finally:
        controller.close()


@pytest.mark.parametrize("change", ["backward", "boot"])
def test_clock_identity_or_order_loss_is_terminal(tmp_path, change):
    clock = FakeClock()
    controller = create(tmp_path / "attempt", clock)
    try:
        through_baseline(controller)
        if change == "backward":
            clock.ns -= 1
        else:
            clock.boot = "different-process"
        with pytest.raises(IntegrityError, match="clock"):
            controller.dispatch("modify")
        assert controller._eligible is False
    finally:
        controller.close()


def test_recovery_only_and_wrong_config_rejected(tmp_path):
    root = tmp_path / "attempt"
    controller = create(root)
    through_baseline(controller)
    submit(controller)
    controller.close()
    with pytest.raises(IntegrityError, match="configuration"):
        Controller.recover(
            root, replace(config(), attempt_id="other"), clock=FakeClock()
        )
    recovered = Controller.recover(root, config(), clock=FakeClock())
    try:
        with pytest.raises(IntegrityError, match="ineligible"):
            recovered.dispatch("evaluate")
        recovered.account()
        assert summary(recovered)["candidate_count"] == 1
        assert summary(recovered)["result"] == "simulation_failed"
    finally:
        recovered.close()


def test_completed_archive_inspection_does_not_reopen_or_invalidate(tmp_path):
    root = tmp_path / "attempt"
    controller = create(root)
    through_activation(controller)
    finish(controller)
    anchor = controller.journal.head
    controller.close()
    before = read_archive(root, anchor)
    assert read_archive(root, anchor) == before
    chain = verify_checkpoint_chain(
        root,
        before,
        attempt_id=config().attempt_id,
        registrations=config().hashes,
        artifacts=config().artifacts,
    )
    assert chain[-1].value["observations"]["result"] == "simulation_complete"
    assert before[-1].value["kind"] == "accounting_completed"


def test_request_readback_delay_is_recorded_without_expiry(tmp_path):
    clock, fault = FakeClock(), Fault()
    controller = create(tmp_path / "attempt", clock, fault)
    try:
        controller.preflight(good(PREFLIGHT_CHECKS), EVIDENCE)
        original = clock.ns

        def delay(point):
            if point == "journal.after_commit:request_received":
                clock.ns += 3601 * 1_000_000_000

        fault.callback = delay
        controller.receive_request()
        timing = values(controller, "request_start_observed")[0]
        assert timing["started"]["monotonic_ns"] == original
        assert timing["receipt_readback_completed"]["monotonic_ns"] == clock.ns
        assert not values(controller, "dispatch_consumed")
    finally:
        controller.close()


def test_recovered_accounting_retains_terminal_reason_retest_and_original_clock(
    tmp_path,
):
    root = tmp_path / "attempt"
    controller = create(root)
    through_baseline(controller)
    submit(controller)
    evaluate(controller, {"fixture": True}, host_failure_proven=True)
    with pytest.raises(IntegrityError, match="second_infrastructure"):
        evaluate(controller, {"fixture": True}, host_failure_proven=True)
    started = values(controller, "request_start_observed")[0]["started"]
    controller.close()
    recovered = Controller.recover(root, config(), clock=FakeClock())
    try:
        recovered.account()
        observed = summary(recovered)
        assert "second_infrastructure_failure" in observed["reasons"]
        assert observed["infrastructure_retest_used"]
        assert observed["historical_timing"][0]["data"]["started"] == started
        assert observed["endpoint"] is None
    finally:
        recovered.close()


def test_caller_mutation_during_persistence_cannot_change_frozen_observation(tmp_path):
    fault = Fault()
    controller = create(tmp_path / "attempt", fault=fault)
    checks = good(PREFLIGHT_CHECKS)
    try:
        fault.callback = lambda point: checks.clear()
        controller.preflight(checks, EVIDENCE)
        assert controller._checkpoints[0].value["observations"]["checks"] == good(
            PREFLIGHT_CHECKS
        )
        assert not checks
    finally:
        controller.close()
