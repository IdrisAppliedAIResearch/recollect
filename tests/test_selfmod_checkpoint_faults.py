import subprocess
import sys

import pytest

from recollect.selfmod.contracts import Snapshot
from recollect.selfmod.controller import ACTIVATION_CHECKS, OUTCOME_CHECKS, Controller
from recollect.selfmod.journal import IntegrityError
from tests.selfmod_checkpoint_helpers import (
    ARTIFACT,
    EVIDENCE,
    FakeClock,
    Fault,
    config,
    create,
    evaluate,
    good,
    submit,
    through_activation,
    through_baseline,
)


@pytest.mark.parametrize(
    "boundary",
    [
        "journal.before_commit:candidate_accepted",
        "journal.after_commit:candidate_accepted",
        "journal.before_readback:candidate_accepted",
        "journal.after_readback:candidate_accepted",
        "journal.before_commit:checkpoint_prepared",
        "journal.after_commit:checkpoint_prepared",
        "journal.before_readback:checkpoint_prepared",
        "journal.after_readback:checkpoint_prepared",
        "checkpoint.after_prepare:CP2.1",
        "bundle.before_write:candidate/extension.py",
        "bundle.after_write:candidate/extension.py",
        "bundle.before_write:manifest.json",
        "bundle.after_write:manifest.json",
        "checkpoint.after_materialize:CP2.1",
        "journal.before_commit:checkpoint_sealed",
        "journal.after_commit:checkpoint_sealed",
        "journal.before_readback:checkpoint_sealed",
        "journal.after_readback:checkpoint_sealed",
        "journal.before_commit:verification_receipt",
        "journal.after_commit:verification_receipt",
        "journal.before_readback:verification_receipt",
        "journal.after_readback:verification_receipt",
        "bundle.before_write:receipt.json",
        "bundle.after_write:receipt.json",
        "checkpoint.after_receipt:CP2.1",
    ],
)
def test_every_candidate_seal_boundary_fails_closed_and_accounts_actual_acceptance(
    tmp_path,
    boundary,
):
    root, fault = tmp_path / "attempt", Fault()
    controller = create(root, fault=fault)
    through_baseline(controller)
    fault.at = boundary
    with pytest.raises(OSError, match="injected"):
        submit(controller)
    assert not controller._eligible
    with pytest.raises(IntegrityError, match="ineligible"):
        controller.dispatch("evaluate")
    controller.close()
    recovered = Controller.recover(root, config(), clock=FakeClock())
    try:
        recovered.account()
        summary = recovered._checkpoints[-1].value["observations"]
        expected_count = (
            0 if boundary == "journal.before_commit:candidate_accepted" else 1
        )
        assert summary["candidate_count"] == expected_count
        assert summary["result"] == "simulation_failed"
        assert bool(summary["requested_without_confirmed_acceptance"]) == (
            expected_count == 0
        )
        assert not [
            r
            for r in recovered.journal.verify()
            if r.value["kind"] == "dispatch_consumed"
            and r.value["data"]["action"] == "evaluate"
        ]
    finally:
        recovered.close()


def test_disk_error_while_writing_failure_record_still_latches_stop(tmp_path):
    fault = Fault()
    controller = create(tmp_path / "attempt", fault=fault)
    try:
        through_baseline(controller)
        fault.at = "journal.before_commit:terminal"
        with pytest.raises(IntegrityError, match="Expected activate"):
            controller.dispatch("activate")
        assert controller._eligible is False
        assert controller.journal.poisoned
        with pytest.raises(IntegrityError, match="ineligible"):
            controller.dispatch("modify")
    finally:
        controller.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "checkpoint.after_prepare:CP4",
        "checkpoint.after_materialize:CP4",
        "journal.after_commit:checkpoint_sealed",
        "bundle.before_write:receipt.json",
        "checkpoint.after_receipt:CP4",
    ],
)
def test_partial_activation_seal_never_dispatches_continuation(tmp_path, boundary):
    fault = Fault()
    controller = create(tmp_path / "attempt", fault=fault)
    try:
        through_baseline(controller)
        submit(controller)
        evaluate(controller)
        controller.dispatch("activate")
        fault.at = boundary
        with pytest.raises(OSError):
            controller.activated(ARTIFACT.sha256, good(ACTIVATION_CHECKS), EVIDENCE)
        with pytest.raises(IntegrityError):
            controller.dispatch("continue")
    finally:
        controller.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "bundle.before_write:receipt.json",
        "checkpoint.after_receipt:CP5",
        "journal.before_commit:endpoint_marker",
        "journal.after_commit:endpoint_marker",
        "journal.before_readback:endpoint_marker",
        "journal.after_readback:endpoint_marker",
        "journal.before_commit:endpoint_observed",
        "journal.after_commit:endpoint_observed",
    ],
)
def test_endpoint_write_uncertainty_never_becomes_success_on_reopen(tmp_path, boundary):
    root, fault = tmp_path / "attempt", Fault()
    controller = create(root, fault=fault)
    through_activation(controller)
    controller.dispatch("continue")
    fault.at = boundary
    with pytest.raises(OSError):
        controller.outcome(good(OUTCOME_CHECKS), EVIDENCE)
    controller.close()
    recovered = Controller.recover(root, config(), clock=FakeClock())
    try:
        recovered.account()
        assert (
            recovered._checkpoints[-1].value["observations"]["result"]
            == "simulation_failed"
        )
    finally:
        recovered.close()


def test_materialization_keyboard_interrupt_cannot_resume_same_controller(tmp_path):
    fault = Fault()
    controller = create(tmp_path / "attempt", fault=fault)
    try:
        through_baseline(controller)

        def interrupt(point):
            if point == "checkpoint.after_materialize:CP2.1":
                raise KeyboardInterrupt("simulated cancellation")

        fault.callback = interrupt
        with pytest.raises(KeyboardInterrupt):
            submit(controller)
        assert not controller._eligible
        with pytest.raises(IntegrityError):
            controller.dispatch("evaluate")
    finally:
        controller.close()


@pytest.mark.parametrize(
    "checkpoint", ["CP0", "CP1", "CP2.1", "CP3.1", "CP4", "CP5", "CP6"]
)
def test_actual_controller_process_exit_after_checkpoint_never_resumes(
    tmp_path, checkpoint
):
    root = tmp_path / "attempt"
    program = """
import os, sys
from pathlib import Path
from tests.selfmod_checkpoint_helpers import create, through_activation, finish
controller = create(Path(sys.argv[1]))
controller.journal.fault = lambda point: (
    os._exit(74) if point == 'checkpoint.after_receipt:' + sys.argv[2] else None
)
through_activation(controller)
finish(controller)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(root), checkpoint],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 74, result.stderr.decode()
    recovered = Controller.recover(root, config(), clock=FakeClock())
    try:
        with pytest.raises(IntegrityError):
            recovered.dispatch("modify")
        recovered.account()
        assert (
            recovered._checkpoints[-1].value["observations"]["result"]
            == "simulation_failed"
        )
    finally:
        recovered.close()


def test_corrupted_sealed_files_are_not_rebuilt_from_archive_to_restore_eligibility(
    tmp_path,
):
    root = tmp_path / "attempt"
    controller = create(root)
    through_baseline(controller)
    target = root / "checkpoints" / controller._checkpoints[0].name / "fixture.txt"
    controller.close()
    target.write_bytes(b"corrupted")
    recovered = Controller.recover(root, config(), clock=FakeClock())
    try:
        with pytest.raises(IntegrityError):
            recovered.dispatch("modify")
        recovered.account()
        summary = recovered._checkpoints[-1].value["observations"]
        assert summary["result"] == "simulation_failed"
        assert summary["damaged_records"]
        assert summary["reached"] == ["CP0", "CP1"]
        assert not any(s["currently_verified"] for s in summary["recorded_seals"])
        assert summary["unsealed_preparations"] == []
    finally:
        recovered.close()
    assert target.read_bytes() == b"corrupted"


def test_missing_evaluation_capture_preserves_available_partial_bytes(tmp_path):
    controller = create(tmp_path / "attempt")
    try:
        through_baseline(controller)
        submit(controller)
        controller.dispatch("evaluate")
        with pytest.raises(IntegrityError, match="evidence_loss"):
            controller.evaluate({}, EVIDENCE, capture_complete=False)
        record = next(
            r
            for r in controller.journal.verify()
            if r.value["kind"] == "evaluation_incomplete"
        )
        assert record.files == EVIDENCE
        with pytest.raises(IntegrityError):
            controller.submit("new", Snapshot(()), EVIDENCE)
    finally:
        controller.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "journal.before_commit:checkpoint_prepared",
        "journal.after_commit:checkpoint_prepared",
        "checkpoint.after_prepare:CP6",
        "bundle.before_write:accounting.json",
        "bundle.after_write:accounting.json",
        "checkpoint.after_materialize:CP6",
        "journal.before_commit:checkpoint_sealed",
        "journal.after_commit:checkpoint_sealed",
        "journal.before_readback:checkpoint_sealed",
        "journal.after_readback:checkpoint_sealed",
        "journal.before_commit:verification_receipt",
        "journal.after_commit:verification_receipt",
        "bundle.before_write:receipt.json",
        "bundle.after_write:receipt.json",
        "checkpoint.after_receipt:CP6",
        "journal.before_commit:accounting_completed",
        "journal.after_commit:accounting_completed",
        "journal.before_readback:accounting_completed",
        "journal.after_readback:accounting_completed",
    ],
)
def test_interrupted_cp6_is_preserved_and_accountable_without_resuming(
    tmp_path, boundary
):
    root, fault = tmp_path / "attempt", Fault()
    controller = create(root, fault=fault)
    through_activation(controller)
    controller.dispatch("continue")
    controller.outcome(good(OUTCOME_CHECKS), EVIDENCE)
    fault.at = boundary
    with pytest.raises(OSError, match="injected"):
        controller.account()
    assert not controller._eligible
    controller.close()
    old_files = {
        p: p.read_bytes()
        for parent in (root / "checkpoints", root / "receipts")
        for p in parent.rglob("*")
        if p.is_file()
    }
    recovered = Controller.recover(root, config(), clock=FakeClock())
    try:
        with pytest.raises(IntegrityError):
            recovered.dispatch("continue")
        recovered.account()
        assert (
            recovered._checkpoints[-1].value["observations"]["result"]
            == "simulation_failed"
        )
        assert all(p.read_bytes() == data for p, data in old_files.items())
        assert any(
            r.value["kind"] == "accounting_completed"
            for r in recovered.journal.verify()
        )
    finally:
        recovered.close()


@pytest.mark.parametrize("damage", ["missing_receipt", "missing_file", "corrupt_file"])
def test_same_controller_accounts_evidence_damage_without_repair(tmp_path, damage):
    from recollect.selfmod.checkpoints import verify_checkpoint_chain

    controller = create(tmp_path / "attempt")
    try:
        through_activation(controller)
        controller.dispatch("continue")
        controller.outcome(good(OUTCOME_CHECKS), EVIDENCE)
        cp0 = controller._checkpoints[0]
        root = controller.journal.root
        target = (
            root / "receipts" / cp0.manifest_sha256 / "receipt.json"
            if damage == "missing_receipt"
            else root / "checkpoints" / cp0.name / "fixture.txt"
        )
        if damage == "corrupt_file":
            target.write_bytes(b"damaged evidence")
        else:
            target.unlink()
        controller.account()
        observed = controller._checkpoints[-1].value["observations"]
        assert observed["result"] == "simulation_failed"
        assert observed["damaged_records"]
        assert (
            target.read_bytes() == b"damaged evidence"
            if damage == "corrupt_file"
            else not target.exists()
        )
        with pytest.raises(IntegrityError):
            verify_checkpoint_chain(
                root,
                controller.journal.verify(),
                attempt_id=config().attempt_id,
                registrations=config().hashes,
                artifacts=config().artifacts,
            )
    finally:
        controller.close()


def test_old_completion_marker_cannot_short_circuit_interrupted_failure_branch(
    tmp_path,
):
    from tests.selfmod_checkpoint_helpers import finish

    root = tmp_path / "attempt"
    controller = create(root)
    through_activation(controller)
    finish(controller)
    missing = (
        root / "receipts" / controller._checkpoints[0].manifest_sha256 / "receipt.json"
    )
    controller.close()
    missing.unlink()
    recovered = Controller.recover(root, config(), clock=FakeClock())
    fault = Fault()
    fault.at = "journal.after_commit:accounting_branch"
    recovered.journal.fault = fault
    with pytest.raises(OSError):
        recovered.account()
    recovered.close()
    again = Controller.recover(root, config(), clock=FakeClock())
    try:
        again.account()
        assert (
            again._checkpoints[-1].value["observations"]["result"]
            == "simulation_failed"
        )
        assert not missing.exists()
    finally:
        again.close()


def test_cancellation_during_accounting_preparation_permanently_fails(
    tmp_path, monkeypatch
):
    controller = create(tmp_path / "attempt")
    try:
        through_activation(controller)
        controller.dispatch("continue")
        controller.outcome(good(OUTCOME_CHECKS), EVIDENCE)
        with monkeypatch.context() as patch:

            def cancel():
                raise KeyboardInterrupt("verification interrupted")

            patch.setattr(controller, "_verify", cancel)
            with pytest.raises(KeyboardInterrupt):
                controller.account()
        assert not controller._eligible
        controller.account()
        assert (
            controller._checkpoints[-1].value["observations"]["result"]
            == "simulation_failed"
        )
    finally:
        controller.close()
