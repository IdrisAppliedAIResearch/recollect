"""Primary mode keeps every simulation gate and labels a live attempt honestly."""

import pytest

from recollect.selfmod.checkpoints import (
    inspect_accounting_chain,
    verify_checkpoint_chain,
)
from recollect.selfmod.controller import Controller
from recollect.selfmod.journal import IntegrityError, read_archive
from tests.selfmod_checkpoint_helpers import (
    EVIDENCE,
    FakeClock,
    Fault,
    config,
    create,
    finish,
    submit,
    through_activation,
    through_baseline,
    values,
)


def primary(root):
    return Controller.create(root, config(), clock=FakeClock(), fault=Fault(),
                             mode="primary")


def identity():
    frozen = config()
    return {"attempt_id": frozen.attempt_id, "registrations": frozen.hashes,
            "artifacts": frozen.artifacts}


def test_primary_chain_records_mode_without_simulation_deviation(tmp_path):
    controller = primary(tmp_path / "attempt")
    try:
        through_activation(controller)
        finish(controller)
        records = read_archive(controller.journal.root, controller.journal.head)
        assert records[0].value["data"]["mode"] == "primary"
        chain = verify_checkpoint_chain(controller.journal.root, records, **identity())
        assert [c.value["checkpoint_id"] for c in chain] == [
            "CP0", "CP1", "CP2.1", "CP3.1", "CP4", "CP5", "CP6"]
        assert all(c.value["deviations"] == [] for c in chain)
        assert chain[-1].value["observations"]["result"] == "primary_complete"
    finally:
        controller.close()


def test_primary_preflight_failure_is_accounted_as_primary_failed(tmp_path):
    controller = primary(tmp_path / "attempt")
    try:
        controller.preflight({}, EVIDENCE)
        controller.account()
        observed = controller._checkpoints[-1].value["observations"]
        assert observed["result"] == "primary_failed"
        assert "preflight_failed" in observed["reasons"]
    finally:
        controller.close()


def test_recovery_keeps_the_recorded_primary_mode(tmp_path):
    root = tmp_path / "attempt"
    controller = primary(root)
    through_baseline(controller)
    submit(controller)
    controller.close()
    recovered = Controller.recover(root, config(), clock=FakeClock())
    try:
        assert recovered.mode == "primary"
        with pytest.raises(IntegrityError, match="ineligible"):
            recovered.dispatch("evaluate")
        recovered.account()
        assert values(recovered, "accounting_branch")[-1]["result"] == "primary_failed"
        observed = recovered._checkpoints[-1].value["observations"]
        assert observed["result"] == "primary_failed"
        assert observed["candidate_count"] == 1
    finally:
        recovered.close()


def test_unknown_mode_is_refused_before_any_archive_exists(tmp_path):
    with pytest.raises(IntegrityError, match="mode"):
        Controller.create(tmp_path / "attempt", config(), clock=FakeClock(),
                          mode="live")
    assert not (tmp_path / "attempt").exists()


def test_accounting_branch_of_another_mode_cannot_extend_the_prefix(tmp_path):
    controller = create(tmp_path / "attempt")
    try:
        through_baseline(controller)
        controller.account()
        records = controller.journal.verify()
    finally:
        controller.close()
    own = inspect_accounting_chain(controller.journal.root, records, **identity())
    assert not own.issues and own.checkpoints[-1].value["checkpoint_id"] == "CP6"
    other = inspect_accounting_chain(controller.journal.root, records, **identity(),
                                     mode="primary")
    assert other.issues
    assert "CP6" not in {c.value["checkpoint_id"] for c in other.checkpoints}
