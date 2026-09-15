from dataclasses import replace

import pytest

from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.development import CheckResults, Development, Review, Stage
from tests.test_selfmod_contracts import make_scope


@pytest.fixture
def setup():
    baseline, policy, contract, plan, proposed = make_scope()
    clock = [120.0]
    options = dict(
        attempt_id="attempt", instance_id="modifier-1", author_id="author",
        forward_reviewer_id="forward", code_reviewer_id="code", contract=contract,
        policy=policy, baseline=baseline, verified_cp1_sha256="1" * 64,
        original_started_at=100.0, deadline=200.0, clock=lambda: clock[0],
    )
    return Development(**options), plan, proposed, clock, options


def review(dev, reviewer, *, approved=True, blockers=()):
    return Review(dev.binding, reviewer, approved, blockers, "2" * 64)


def checks(dev, *, passed=True):
    return CheckResults(dev.binding, (("unit", passed), ("regression", True)), "3" * 64)


def implement(dev, plan, proposed):
    dev.propose("modifier-1", plan)
    dev.review("modifier-1", review(dev, "forward"))
    dev.implementation("modifier-1", proposed)


def ready(dev, plan, proposed):
    implement(dev, plan, proposed)
    dev.checks("modifier-1", checks(dev))
    dev.review("modifier-1", review(dev, "code"))


def test_full_development_path_stops_before_submission_or_deployment(setup):
    dev, plan, proposed, _, _ = setup
    ready(dev, plan, proposed)
    assert dev.stage == Stage.READY
    assert dev.candidate("modifier-1").sha256 == proposed.sha256
    assert [e.kind for e in dev.events] == [
        "cp1_released", "plan_proposed", "forward_review", "implementation_captured",
        "development_checks", "code_review",
    ]
    assert [e.sequence for e in dev.events] == list(range(1, 7))
    assert dev.events[-1].binding.artifact_sha256 == proposed.sha256


def test_forward_review_cannot_be_skipped_or_self_approved(setup):
    dev, plan, proposed, _, _ = setup
    with pytest.raises(ValueError, match="approved forward review"):
        dev.implementation("modifier-1", proposed)
    dev.propose("modifier-1", plan)
    with pytest.raises(ValueError, match="unassigned reviewer"):
        dev.review("modifier-1", review(dev, "author"))
    with pytest.raises(ValueError, match="development gates"):
        dev.candidate("modifier-1")


def test_blocker_overrides_approval_and_reviewer_must_recheck(setup):
    dev, plan, proposed, _, _ = setup
    dev.propose("modifier-1", plan)
    dev.review("modifier-1", review(dev, "forward", blockers=("B1",)))
    assert dev.stage == Stage.FORWARD_REVIEW
    with pytest.raises(ValueError):
        dev.implementation("modifier-1", proposed)
    dev.review("modifier-1", review(dev, "forward"))
    assert dev.stage == Stage.IMPLEMENT
    assert [e.detail for e in dev.events[-2:]] == ["rejected", "accepted"]


def test_ordinary_development_failure_can_revise_without_resetting_time(setup):
    dev, plan, proposed, clock, _ = setup
    implement(dev, plan, proposed)
    dev.checks("modifier-1", checks(dev, passed=False))
    assert dev.stage == Stage.IMPLEMENT
    changed = Snapshot((*proposed.files[:-1], File("extensions/new.py", b"changed")))
    clock[0] = 190
    dev.implementation("modifier-1", changed)
    dev.checks("modifier-1", checks(dev))
    dev.review("modifier-1", review(dev, "code"))
    assert dev.candidate("modifier-1").sha256 == changed.sha256
    clock[0] = 201
    with pytest.raises(ValueError, match="deadline"):
        dev.candidate("modifier-1")
    assert dev.stage == Stage.TERMINAL


def test_code_edit_invalidates_tests_and_code_review(setup):
    dev, plan, proposed, _, _ = setup
    ready(dev, plan, proposed)
    old_checks = checks(dev)
    old_review = review(dev, "code")
    changed = Snapshot((*proposed.files[:-1], File("extensions/new.py", b"revision")))
    dev.implementation("modifier-1", changed)
    assert dev.stage == Stage.CHECKS
    with pytest.raises(ValueError):
        dev.candidate("modifier-1")
    with pytest.raises(ValueError):
        dev.checks("modifier-1", old_checks)
    dev.checks("modifier-1", checks(dev))
    with pytest.raises(ValueError, match="Stale review"):
        dev.review("modifier-1", old_review)


def test_plan_edit_invalidates_all_downstream_approvals(setup):
    dev, plan, proposed, _, _ = setup
    ready(dev, plan, proposed)
    changed = replace(plan, changes=(replace(plan.changes[0], reason="revised plan"),))
    dev.propose("modifier-1", changed)
    assert dev.stage == Stage.FORWARD_REVIEW
    assert dev.binding.artifact_sha256 is None
    with pytest.raises(ValueError):
        dev.implementation("modifier-1", proposed)
    with pytest.raises(ValueError):
        dev.candidate("modifier-1")


def test_identical_reverted_bytes_cannot_reuse_old_approval(setup):
    dev, plan, proposed, _, _ = setup
    ready(dev, plan, proposed)
    old_checks = checks(dev)
    dev.implementation("modifier-1", proposed)
    with pytest.raises(ValueError, match="current implementation"):
        dev.checks("modifier-1", old_checks)
    dev.propose("modifier-1", plan)
    old_forward = review(dev, "forward")
    dev.propose("modifier-1", plan)
    with pytest.raises(ValueError, match="Stale review"):
        dev.review("modifier-1", old_forward)


@pytest.mark.parametrize("results", [
    (("unit", True),),
    (("unit", True), ("regression", True), ("extra", True)),
    (("unit", True), ("regression", True), ("unit", True)),
])
def test_exact_check_inventory_required(setup, results):
    dev, plan, proposed, _, _ = setup
    implement(dev, plan, proposed)
    with pytest.raises(ValueError, match="inventory"):
        dev.checks("modifier-1", CheckResults(dev.binding, results, "4" * 64))
    assert dev.stage == Stage.CHECKS


def test_skipped_or_string_truth_is_not_a_pass(setup):
    dev, plan, _, _, _ = setup
    dev.propose("modifier-1", plan)
    with pytest.raises(ValueError, match="boolean"):
        CheckResults(dev.binding, (("unit", "skipped"),), "3" * 64)
    with pytest.raises(ValueError, match="boolean"):
        replace(review(dev, "forward"), approved="true")


@pytest.mark.parametrize("field", ["attempt_id", "instance_id", "contract_sha256",
                                  "baseline_sha256", "plan_sha256"])
def test_receipts_cannot_cross_identity_boundaries(setup, field):
    dev, plan, _, _, _ = setup
    dev.propose("modifier-1", plan)
    report = review(dev, "forward")
    bad_binding = replace(report.binding, **{field: "different"})
    with pytest.raises(ValueError, match="Stale review"):
        dev.review("modifier-1", replace(report, binding=bad_binding))


def test_scope_breach_is_terminal_and_does_not_return_a_cleaned_candidate(setup):
    dev, plan, proposed, _, _ = setup
    dev.propose("modifier-1", plan)
    dev.review("modifier-1", review(dev, "forward"))
    bad = Snapshot((*proposed.files, File("unrelated.py", b"drift")))
    with pytest.raises(ValueError, match="Forbidden"):
        dev.implementation("modifier-1", bad)
    assert dev.stage == Stage.TERMINAL
    assert dev.events[-2].kind == "snapshot_rejected"
    with pytest.raises(ValueError, match="terminal"):
        dev.propose("modifier-1", plan)


def test_worker_death_cannot_be_refreshed_into_same_primary_path(setup):
    dev, plan, _, _, _ = setup
    dev.fail_worker("modifier-1", "watchdog terminated worker")
    with pytest.raises(ValueError, match="terminal"):
        dev.propose("modifier-1", plan)
    with pytest.raises(ValueError, match="Stale"):
        dev.propose("modifier-2", plan)


def test_boundary_deadline_and_backward_clock(setup):
    dev, plan, proposed, clock, _ = setup
    ready(dev, plan, proposed)
    clock[0] = 200.0
    assert dev.candidate("modifier-1")
    clock[0] = 199.0
    with pytest.raises(ValueError, match="Monotonic"):
        dev.candidate("modifier-1")
    assert dev.stage == Stage.TERMINAL


def test_updates_and_reviews_have_no_count_quota_but_keep_deadline(setup):
    dev, plan, proposed, clock, _ = setup
    for _ in range(100):
        dev.propose("modifier-1", plan)
        dev.review("modifier-1", review(dev, "forward", approved=False))
        assert dev.stage == Stage.FORWARD_REVIEW
        dev.review("modifier-1", review(dev, "forward"))
        dev.implementation("modifier-1", proposed)
    assert dev._updates == dev._reviews == 200
    assert dev.stage == Stage.CHECKS
    clock[0] = 201.0
    with pytest.raises(ValueError, match="deadline"):
        dev.propose("modifier-1", plan)
    assert dev.stage == Stage.TERMINAL


@pytest.mark.parametrize("change", [
    {"verified_cp1_sha256": ""}, {"code_reviewer_id": "author"},
    {"deadline": float("inf")},
    {"deadline": 3701}, {"deadline": 99},
])
def test_invalid_frozen_configuration_rejected(setup, change):
    *_, options = setup
    with pytest.raises(ValueError):
        Development(**{**options, **change})
