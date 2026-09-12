"""Generic fixtures only: no calendar adapter or target rehearsal."""

from dataclasses import FrozenInstanceError, replace

import pytest

from recollect.selfmod.contracts import (
    ChangePolicy,
    File,
    Plan,
    PlannedChange,
    Requirement,
    Snapshot,
    TaskContract,
    Verification,
    reconstruct_candidate,
)


def make_scope():
    baseline = Snapshot((File("runner.py", b"protected"),
                         File("extensions/existing.py", b"also protected")))
    policy = ChangePolicy(baseline.sha256, create_under=("extensions",))
    contract = TaskContract(
        "Perform the original fixture task.",
        (Requirement("outcome", "Original task succeeds", "Later independent CP5"),
         Requirement("preserve", "Research still works", "Frozen regression suite")),
        ("unit", "regression"), policy.sha256,
    )
    plan = Plan(
        contract.sha256,
        (PlannedChange("extensions/new.py", "create", ("outcome",),
                       "Implement only the missing fixture operation"),),
        (Verification("outcome", "Independent CP5 after activation"),
         Verification("preserve", "Run regression checks before submission")),
    )
    proposed = Snapshot((*baseline.files, File("extensions/new.py", b"fixture code")))
    return baseline, policy, contract, plan, proposed


@pytest.fixture
def scope():
    return make_scope()


def test_frozen_contract_and_baseline_reconstruction(scope):
    baseline, policy, contract, plan, proposed = scope
    with pytest.raises(FrozenInstanceError):
        contract.original_request = "different task"
    result = reconstruct_candidate(baseline, proposed, policy, plan, contract)
    assert result.sha256 == proposed.sha256
    assert result.files[0].content == b"also protected"
    assert baseline.files[0].content == b"protected"
    assert result.sha256 == Snapshot(tuple(reversed(result.files))).sha256
    changed = replace(contract, original_request="different task")
    assert changed.sha256 != contract.sha256


@pytest.mark.parametrize("path", [
    "../runner.py", "/root.py", "a/../b", "a//b", "a/./b", "a/", "a\\b",
    "C:/a", "file:stream", "a. /b", "a./b", "NUL.txt", "x/COM1.py", "CON/b",
    "LPT9", "a\x00b", "café.py", "a b.py",
])
def test_reject_nonportable_or_aliased_paths(path):
    with pytest.raises(ValueError):
        File(path, b"")


@pytest.mark.parametrize("paths", [
    ("a.py", "a.py"), ("a.py", "A.py"), ("a", "a/b"), ("a", "A/b"),
    ("Tools/a.py", "tools/b.py"),
])
def test_reject_snapshot_path_collisions(paths):
    with pytest.raises(ValueError):
        Snapshot(tuple(File(path, b"") for path in paths))


def test_reject_mutable_frozen_inputs(scope):
    _, policy, contract, plan, _ = scope
    for build in (
        lambda: File("a", bytearray(b"x")),
        lambda: Snapshot([File("a", b"x")]),
        lambda: replace(policy, modify=["runner.py"]),
        lambda: replace(contract, requirements=list(contract.requirements)),
        lambda: replace(plan, changes=list(plan.changes)),
    ):
        with pytest.raises(ValueError):
            build()


@pytest.mark.parametrize("kind", ["modify", "delete", "extra", "overwrite_in_create"])
def test_reject_whole_snapshot_on_out_of_scope_changes(scope, kind):
    baseline, policy, contract, plan, proposed = scope
    files = {f.path: f.content for f in proposed.files}
    if kind == "modify":
        files["runner.py"] = b"restored but drifted"
    elif kind == "delete":
        del files["runner.py"]
    elif kind == "extra":
        files["unrelated.py"] = b"unrelated"
    else:
        files["extensions/existing.py"] = b"creation is not overwrite permission"
    bad = Snapshot(tuple(File(p, b) for p, b in files.items()))
    with pytest.raises(ValueError, match="Forbidden"):
        reconstruct_candidate(baseline, bad, policy, plan, contract)


def test_exact_modification_and_deletion_grants(scope):
    baseline, _, contract, _, _ = scope
    policy = ChangePolicy(baseline.sha256, modify=("runner.py",),
                          delete=("extensions/existing.py",))
    contract = replace(contract, policy_sha256=policy.sha256)
    plan = Plan(contract.sha256, (
        PlannedChange("runner.py", "modify", ("outcome",), "Required fixture change"),
        PlannedChange("extensions/existing.py", "delete", ("outcome",), "Required"),
    ), (Verification("outcome", "CP5"), Verification("preserve", "regressions")))
    proposed = Snapshot((File("runner.py", b"modified"),))
    assert reconstruct_candidate(baseline, proposed, policy, plan, contract) == proposed


def test_creation_scope_is_recursive_but_not_sibling_prefix(scope):
    _, policy, _, _, _ = scope
    assert policy.permits("extensions/nested/new.py", "create")
    assert not policy.permits("extensions-other/new.py", "create")
    assert not policy.permits("extensions", "create")
    assert not policy.permits("extensions/existing.py", "modify")
    assert not policy.permits("extensions/new.py", "rename")


@pytest.mark.parametrize("change", ["contract", "missing_coverage", "extra_coverage",
                                   "unknown_requirement", "unnecessary_change"])
def test_plan_cannot_redefine_or_leave_task_uncovered(scope, change):
    _, policy, contract, plan, _ = scope
    if change == "contract":
        plan = replace(plan, contract_sha256="0" * 64)
    elif change == "missing_coverage":
        plan = replace(plan, verification=plan.verification[:1])
    elif change == "extra_coverage":
        plan = replace(plan, verification=(*plan.verification, plan.verification[0]))
    elif change == "unknown_requirement":
        change = replace(plan.changes[0], requirement_ids=("x",))
        plan = replace(plan, changes=(change,))
    else:
        plan = replace(plan, changes=(replace(plan.changes[0], path="unrelated.py"),))
    with pytest.raises(ValueError):
        plan.validate(contract, policy)


def test_drift_and_unplanned_allowed_edits_are_rejected(scope):
    baseline, policy, contract, plan, proposed = scope
    with pytest.raises(ValueError, match="Baseline drift"):
        reconstruct_candidate(proposed, proposed, policy, plan, contract)
    extra = Snapshot((*proposed.files, File("extensions/extra.py", b"bloat")))
    with pytest.raises(ValueError, match="reviewed plan"):
        reconstruct_candidate(baseline, extra, policy, plan, contract)
    with pytest.raises(ValueError, match="reviewed plan"):
        reconstruct_candidate(baseline, baseline, policy, plan, contract)


def test_case_only_rename_is_not_a_valid_change(scope):
    baseline, _, contract, plan, _ = scope
    policy = ChangePolicy(baseline.sha256, create_under=("extensions",),
                          delete=("extensions/existing.py",))
    contract = replace(contract, policy_sha256=policy.sha256)
    plan = replace(plan, contract_sha256=contract.sha256, changes=(
        PlannedChange("extensions/existing.py", "delete", ("outcome",), "reason"),
    ))
    proposed = Snapshot((File("runner.py", b"protected"),
                         File("extensions/EXISTING.py", b"also protected")))
    with pytest.raises(ValueError, match="case-aliased"):
        reconstruct_candidate(baseline, proposed, policy, plan, contract)
