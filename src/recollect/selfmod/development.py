"""Controller-owned, pre-CP2 development gates; no execution or persistence.

Callers must authenticate actors, verify CP1 release, and capture complete file
snapshots and receipts outside the worker. These objects are not an agent RPC API.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .contracts import (
    ChangePolicy,
    Plan,
    Snapshot,
    TaskContract,
    reconstruct_candidate,
    require_digest,
    require_tuple,
)


class Stage(StrEnum):
    PLAN = "plan"
    FORWARD_REVIEW = "forward_review"
    IMPLEMENT = "implement"
    CHECKS = "checks"
    CODE_REVIEW = "code_review"
    READY = "ready_for_submission"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class Binding:
    attempt_id: str
    instance_id: str
    revision: int
    contract_sha256: str
    baseline_sha256: str
    plan_sha256: str
    artifact_sha256: str | None


@dataclass(frozen=True)
class Review:
    binding: Binding
    reviewer_id: str
    approved: bool
    unresolved_blockers: tuple[str, ...]
    evidence_sha256: str

    def __post_init__(self):
        require_digest(self.evidence_sha256)
        require_tuple(self.unresolved_blockers)
        if type(self.approved) is not bool:
            raise ValueError("Review decision must be boolean")


@dataclass(frozen=True)
class CheckResults:
    binding: Binding
    results: tuple[tuple[str, bool], ...]
    evidence_sha256: str

    def __post_init__(self):
        require_digest(self.evidence_sha256)
        require_tuple(self.results)
        for result in self.results:
            require_tuple(result)
            if len(result) != 2 or type(result[1]) is not bool:
                raise ValueError("Each required check needs an explicit boolean result")


@dataclass(frozen=True)
class Event:
    sequence: int
    monotonic: float
    kind: str
    binding: Binding | None
    evidence_sha256: str | None
    detail: str


class Development:
    def __init__(
        self, *, attempt_id: str, instance_id: str, author_id: str,
        forward_reviewer_id: str, code_reviewer_id: str,
        contract: TaskContract, policy: ChangePolicy, baseline: Snapshot,
        verified_cp1_sha256: str, original_started_at: float, deadline: float,
        max_updates: int, max_review_reports: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        actors = (author_id, forward_reviewer_id, code_reviewer_id)
        if (not all(isinstance(s, str) and s.strip()
                    for s in (attempt_id, instance_id, *actors))
                or len(set(actors)) != 3):
            raise ValueError("Controller must assign distinct author/review instances")
        require_digest(verified_cp1_sha256)
        if (contract.policy_sha256 != policy.sha256
                or baseline.sha256 != policy.baseline_sha256):
            raise ValueError("Frozen contract/policy/baseline mismatch")
        if (not all(math.isfinite(v) for v in (original_started_at, deadline))
                or not original_started_at < deadline <= original_started_at + 3600):
            raise ValueError("Inherit the original attempt's finite deadline")
        if any(type(v) is not int or v < 1
               for v in (max_updates, max_review_reports)):
            raise ValueError("Freeze positive finite development budgets")
        self._attempt = attempt_id
        self._instance = instance_id
        self._reviewers = (forward_reviewer_id, code_reviewer_id)
        self._contract, self._policy, self._baseline = contract, policy, baseline
        self._deadline, self._clock = deadline, clock
        self._last_time = original_started_at
        self._max_updates, self._max_reviews = max_updates, max_review_reports
        self._updates = self._reviews = 0
        self._revision = 0
        self._stage = Stage.PLAN
        self._plan: Plan | None = None
        self._artifact: Snapshot | None = None
        self._events: list[Event] = []
        self._guard(instance_id)
        self._record("cp1_released", evidence=verified_cp1_sha256)

    @property
    def stage(self) -> Stage:
        return self._stage

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def binding(self) -> Binding:
        if self._plan is None:
            raise ValueError("No plan to bind")
        return Binding(
            self._attempt, self._instance, self._revision, self._contract.sha256,
            self._baseline.sha256, self._plan.sha256,
            self._artifact.sha256 if self._artifact else None,
        )

    def _record(self, kind: str, *, evidence: str | None = None, detail: str = ""):
        self._events.append(Event(
            len(self._events) + 1, self._last_time, kind,
            self.binding if self._plan else None, evidence, detail,
        ))

    def _terminate(self, reason: str):
        self._stage = Stage.TERMINAL
        self._record("terminal", detail=reason)

    def _guard(self, instance_id: str):
        if instance_id != self._instance:
            raise ValueError("Stale or unauthorized modifier instance")
        if self._stage == Stage.TERMINAL:
            raise ValueError("Primary path is terminal; refresh cannot revive it")
        now = self._clock()
        if not math.isfinite(now) or now < self._last_time:
            self._terminate("clock_integrity_failure")
            raise ValueError("Monotonic clock identity/order lost")
        self._last_time = now
        if now > self._deadline:
            self._terminate("budget_exhausted")
            raise ValueError("Original attempt deadline exhausted")

    def _consume_update(self):
        if self._updates >= self._max_updates:
            self._terminate("development_update_budget_exhausted")
            raise ValueError("Development update budget exhausted")
        self._updates += 1

    def propose(self, instance_id: str, plan: Plan):
        self._guard(instance_id)
        self._consume_update()
        try:
            plan.validate(self._contract, self._policy)
        except ValueError as exc:
            self._record("plan_rejected", evidence=plan.sha256, detail=str(exc))
            raise
        self._plan, self._artifact = plan, None
        self._revision += 1
        self._stage = Stage.FORWARD_REVIEW
        self._record("plan_proposed")

    def review(self, instance_id: str, report: Review):
        self._guard(instance_id)
        if self._stage not in {Stage.FORWARD_REVIEW, Stage.CODE_REVIEW}:
            raise ValueError("No review is pending")
        forward = self._stage == Stage.FORWARD_REVIEW
        expected_reviewer = self._reviewers[0 if forward else 1]
        if report.binding != self.binding or report.reviewer_id != expected_reviewer:
            raise ValueError("Stale review or unassigned reviewer")
        if self._reviews >= self._max_reviews:
            self._terminate("review_budget_exhausted")
            raise ValueError("Review budget exhausted")
        self._reviews += 1
        accepted = report.approved and not report.unresolved_blockers
        self._record(
            "forward_review" if forward else "code_review",
            evidence=report.evidence_sha256,
            detail="accepted" if accepted else "rejected",
        )
        if accepted:
            self._stage = Stage.IMPLEMENT if forward else Stage.READY
        # Rejections stay pending so a reviewer can adjudicate a recorded dispute.
        # Edited code/plans must re-enter through their respective methods.

    def implementation(self, instance_id: str, proposed: Snapshot):
        self._guard(instance_id)
        if self._stage not in {
            Stage.IMPLEMENT, Stage.CHECKS, Stage.CODE_REVIEW, Stage.READY,
        }:
            raise ValueError("Implementation requires an approved forward review")
        self._consume_update()
        try:
            artifact = reconstruct_candidate(
                self._baseline, proposed, self._policy, self._plan, self._contract,
            )
        except ValueError as exc:
            self._record("snapshot_rejected", evidence=proposed.sha256, detail=str(exc))
            self._terminate("scope_or_snapshot_integrity_failure")
            raise
        # Never let an edited artifact inherit test or code-review approval.
        self._artifact = artifact
        self._revision += 1
        self._stage = Stage.CHECKS
        self._record("implementation_captured")

    def checks(self, instance_id: str, report: CheckResults):
        self._guard(instance_id)
        if self._stage != Stage.CHECKS or report.binding != self.binding:
            raise ValueError("Checks must bind the current implementation")
        results = dict(report.results)
        if (len(results) != len(report.results)
                or set(results) != set(self._contract.development_checks)):
            raise ValueError("Required development check inventory mismatch")
        passed = all(results.values())
        self._record("development_checks", evidence=report.evidence_sha256,
                     detail="passed" if passed else "failed")
        self._stage = Stage.CODE_REVIEW if passed else Stage.IMPLEMENT

    def fail_worker(self, instance_id: str, reason: str):
        self._guard(instance_id)
        self._terminate("worker_failure: " + reason)

    def candidate(self, instance_id: str) -> Snapshot:
        self._guard(instance_id)
        if self._stage != Stage.READY:
            raise ValueError("Candidate has not cleared development gates")
        # Readiness is not durable submission, CP2 sealing, or deployment approval.
        return self._artifact
