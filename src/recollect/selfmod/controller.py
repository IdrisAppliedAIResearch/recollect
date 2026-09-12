"""Offline simulation controller. No agent dispatch or live-task PASS is exposed.

Fixture observations are supplied by the trusted host. Real collectors, runtime
attestation, and dispatch-time fencing must be wired before any live experiment.
"""

import contextlib
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .checkpoints import (
    bundle,
    bundle_name,
    inspect_accounting_chain,
    materialize,
    verify_checkpoint_chain,
    verify_materialized,
)
from .contracts import File, Snapshot, TaskContract, require_digest, require_tuple
from .journal import EMPTY_SNAPSHOT, IntegrityError, Journal, decode, encode

_BOOT_ID = f"{os.getpid()}-{uuid.uuid4().hex}"
_LIMIT_NS = 3600 * 1_000_000_000
PREFLIGHT_CHECKS = frozenset(
    {
        "runtime_frozen",
        "evaluator_frozen",
        "same_authorization",
        "baseline_empty",
        "three_way_generation",
    }
)
BASELINE_CHECKS = frozenset(
    {
        "gap_reported",
        "modification_requested",
        "target_quiescent",
        "baseline_empty",
        "same_identity",
        "no_false_success",
        "no_unknown_effects",
    }
)
ACTIVATION_CHECKS = frozenset(
    {
        "route_changed",
        "health_passed",
        "target_gate_closed",
        "target_quiescent",
        "baseline_empty",
        "continuation_linked",
        "a_available",
    }
)
OUTCOME_CHECKS = frozenset(
    {
        "original_task_completed",
        "event_fields_verified",
        "exactly_one_event",
        "same_identity_replay",
        "generated_code_attribution",
        "unchanged_authorization",
        "main_reply_captured",
        "concurrency_passed",
        "responsiveness_passed",
    }
)


@dataclass(frozen=True)
class Stamp:
    monotonic_ns: int
    utc: str
    boot_id: str

    def __post_init__(self):
        if (
            type(self.monotonic_ns) is not int
            or self.monotonic_ns < 0
            or not isinstance(self.boot_id, str)
            or not self.boot_id
            or datetime.fromisoformat(self.utc).utcoffset() != UTC.utcoffset(None)
        ):
            raise ValueError("Valid process-clock identity and UTC stamp required")


def current_stamp() -> Stamp:
    return Stamp(time.monotonic_ns(), datetime.now(UTC).isoformat(), _BOOT_ID)


@dataclass(frozen=True)
class ControllerConfig:
    attempt_id: str
    contract: TaskContract
    registrations: tuple[tuple[str, str], ...]
    baseline_sha256: str
    evaluator_sha256: str
    evaluation_checks: tuple[str, ...]

    def __post_init__(self):
        if not self.attempt_id.strip():
            raise ValueError("Attempt ID required")
        require_tuple(self.registrations)
        for pair in self.registrations:
            require_tuple(pair)
            if len(pair) != 2:
                raise ValueError("Registration must identify a named digest")
            require_digest(pair[1])
        if (
            set(dict(self.registrations))
            != {"protocol", "checkpoints", "amendment", "runtime"}
            or len(self.registrations) != 4
        ):
            raise ValueError("Freeze all three registrations and the runtime identity")
        require_digest(self.baseline_sha256)
        require_digest(self.evaluator_sha256)
        require_tuple(self.evaluation_checks)
        if (
            not self.evaluation_checks
            or len(set(self.evaluation_checks)) != len(self.evaluation_checks)
            or any(
                not isinstance(s, str) or not s.strip() for s in self.evaluation_checks
            )
        ):
            raise ValueError("Freeze the independent evaluator check inventory")

    @property
    def hashes(self) -> dict:
        return {**dict(self.registrations), "task_contract": self.contract.sha256}

    @property
    def artifacts(self) -> dict:
        return {"baseline": self.baseline_sha256, "evaluator": self.evaluator_sha256}


def _passed(checks: dict, required: frozenset | set) -> bool:
    return set(checks) == required and all(
        type(v) is bool and v for v in checks.values()
    )


class Controller:
    @classmethod
    def create(
        cls,
        root: Path,
        config: ControllerConfig,
        *,
        clock=current_stamp,
        fault=lambda _: None,
    ):
        journal = Journal.create(root, fault=fault)
        try:
            result = cls(journal, config, clock=clock)
            (root / "checkpoints").mkdir()
            (root / "receipts").mkdir()
            result._emit(
                "attempt_opened",
                {
                    "mode": "simulation",
                    "config": asdict(config),
                    "controller_instance": result._instance,
                },
            )
            return result
        except BaseException:
            journal.close()
            raise

    @classmethod
    def recover(cls, root: Path, config: ControllerConfig, *, clock=current_stamp):
        """Declare continuity lost; inspect completed archives read-only instead."""
        journal = Journal.recover(root)
        try:
            result = cls(journal, config, clock=clock)
            records = journal.verify()
            result._verify_config(records)
            inspection = result._inspect_accounting(records)
            result._checkpoints = inspection.checkpoints
            result._damage = inspection.issues
            for record in records:
                value = record.value
                if value["kind"] == "candidate_accepted":
                    data = value["data"]
                    result._submissions[data["submission_id"]] = (
                        data["candidate_number"],
                        data["candidate_sha256"],
                    )
                    result._number = data["candidate_number"]
                    result._candidate = data["candidate_sha256"]
                elif value["kind"] == "terminal":
                    reason = value["data"]["reason"]
                    if reason not in result._reasons:
                        result._reasons.append(reason)
                elif value["kind"] == "infrastructure_retest_consumed":
                    result._retest_used = True
                elif value["kind"] in {"request_start_observed", "endpoint_observed"}:
                    result._historical_timing.append(value)
            result._eligible = False
            result._phase = "failed"
            result._reasons.append("controller_reopened_accounting_only")
            result._emit("recovery", {"reason": result._reasons[-1]})
            return result
        except BaseException:
            journal.close()
            raise

    def __init__(self, journal: Journal, config: ControllerConfig, *, clock):
        self.journal, self.config, self._clock = journal, config, clock
        self._lock = threading.RLock()
        self._instance = uuid.uuid4().hex
        self._last = clock()
        self._started: Stamp | None = None
        self._endpoint: Stamp | None = None
        self._eligible = True
        self._phase = "preflight"
        self._checkpoints = ()
        self._submissions: dict[str, tuple[int, str]] = {}
        self._number = 0
        self._candidate = None
        self._rejected = set()
        self._retest_used = False
        self._retry = False
        self._dispatched = None
        self._reasons: list[str] = []
        self._accounting_mode = False
        self._branch_sequence = 0
        self._damage = ()
        self._historical_timing = []

    def _now(self) -> Stamp:
        now = self._clock()
        if (
            now.boot_id != self._last.boot_id
            or now.monotonic_ns < self._last.monotonic_ns
        ):
            raise IntegrityError("Process clock identity/order lost")
        self._last = now
        return now

    def _emit(self, kind, data, files=EMPTY_SNAPSHOT):
        return self.journal.append(kind, {**data, "clock": asdict(self._now())}, files)

    def _verify_config(self, records):
        if (
            not records
            or records[0].value["kind"] != "attempt_opened"
            or records[0].value["data"]["mode"] != "simulation"
            or encode(records[0].value["data"]["config"]) != encode(asdict(self.config))
        ):
            raise IntegrityError("Attempt configuration differs from frozen root")

    def _chain(self, records):
        if self._accounting_mode:
            return self._inspect_accounting(records).checkpoints
        return verify_checkpoint_chain(
            self.journal.root,
            records,
            attempt_id=self.config.attempt_id,
            registrations=self.config.hashes,
            artifacts=self.config.artifacts,
        )

    def _inspect_accounting(self, records):
        return inspect_accounting_chain(
            self.journal.root,
            records,
            attempt_id=self.config.attempt_id,
            registrations=self.config.hashes,
            artifacts=self.config.artifacts,
        )

    def _verify(self):
        records = self.journal.verify()
        self._verify_config(records)
        chain = self._chain(records)
        if tuple(c.manifest_sha256 for c in chain) != tuple(
            c.manifest_sha256 for c in self._checkpoints
        ):
            raise IntegrityError("Checkpoint head changed outside the controller")
        for record in records:
            if (
                record.value["kind"] == "verification_receipt"
                and record.anchor.sequence > self._branch_sequence
            ):
                verify_materialized(
                    self.journal.root
                    / "receipts"
                    / record.value["data"]["manifest_sha256"],
                    record.files,
                )
        return records

    def _prepare_accounting(self):
        records = self.journal.verify()
        self._verify_config(records)
        if self._eligible and self._phase == "complete":
            try:
                self._verify()
            except (ValueError, OSError, KeyError) as exc:
                self._abort("evidence_integrity_failure: " + str(exc))
                records = self.journal.verify()
            else:
                return
        if self._phase == "accounted":
            raise IntegrityError("Attempt is already accounted")
        if self._eligible:
            self._abort("stopped_before_completion")
            records = self.journal.verify()
        inspection = self._inspect_accounting(records)
        issues = list(inspection.issues)
        for record in records:
            if record.value["kind"] == "verification_receipt":
                try:
                    verify_materialized(
                        self.journal.root
                        / "receipts"
                        / record.value["data"]["manifest_sha256"],
                        record.files,
                    )
                except (ValueError, OSError) as exc:
                    issues.append(
                        {"record_sequence": record.anchor.sequence, "reason": str(exc)}
                    )
        if issues and "evidence_integrity_failure" not in self._reasons:
            self._reasons.append("evidence_integrity_failure")
        self._eligible = False
        self._phase = "failed"
        self._checkpoints = inspection.checkpoints
        self._damage = tuple(issues)
        branch = self._emit(
            "accounting_branch",
            {
                "result": "simulation_failed",
                "reasons": self._reasons,
                "verified_prefix": [c.seal_sequence for c in self._checkpoints],
                "issues": issues,
                "prior_journal_anchor": asdict(self.journal.head),
            },
        )
        self._accounting_mode = True
        self._branch_sequence = branch.anchor.sequence

    def _abort(self, reason):
        self._eligible = False
        self._phase = "failed"
        if reason not in self._reasons:
            self._reasons.append(reason)
        if not self.journal.poisoned:
            # Eligibility stays false even if best-effort failure recording fails.
            with contextlib.suppress(Exception):
                # Even a broken clock must not stop best-effort failure accounting.
                self.journal.append(
                    "terminal", {"reason": reason, "last_clock": asdict(self._last)}
                )

    @contextlib.contextmanager
    def _operation(self, *, accounting=False):
        with self._lock:
            if self._phase == "accounted":
                raise IntegrityError("Attempt is already accounted")
            if not accounting and (not self._eligible or self.journal.recovery_only):
                raise IntegrityError("Primary path is permanently ineligible")
            try:
                self._verify()
                now = self._now()
                if (
                    not accounting
                    and self._started is not None
                    and now.monotonic_ns - self._started.monotonic_ns > _LIMIT_NS
                ):
                    raise IntegrityError("budget_exhausted")
                yield
            except BaseException as exc:
                self._abort(str(exc))
                raise

    def _expect(self, phase):
        if self._phase != phase:
            raise IntegrityError(f"Expected {phase}, found {self._phase}")

    def _after_gate(self):
        now = self._now()
        if (
            self._started is not None
            and now.monotonic_ns - self._started.monotonic_ns > _LIMIT_NS
        ):
            raise IntegrityError("budget_exhausted")

    def _seal(self, checkpoint_id, evidence, observations, gate, reasons=()):
        started = self._now()
        captured = Snapshot(
            (*evidence.files, File("observations.json", encode(observations)))
        )
        completed = self._now()
        metadata = dict(
            schema_version=1,
            archive_sequence=self.journal.head.sequence + 1,
            attempt_id=self.config.attempt_id,
            checkpoint_id=checkpoint_id,
            checkpoint_sequence=len(self._checkpoints) + 1,
            previous_checkpoint_sha256=(
                self._checkpoints[-1].manifest_sha256 if self._checkpoints else None
            ),
            registrations=self.config.hashes,
            candidate_number=self._number or None,
            candidate_sha256=self._candidate,
            collection_started=asdict(started),
            collection_completed=asdict(completed),
            seal_prepared=asdict(self._now()),
            actor_id=self._instance,
            artifacts=self.config.artifacts,
            observations=observations,
            gate=gate,
            reasons=list(reasons or (() if gate else ("checkpoint_gate_failed",))),
            missing_evidence=[],
            deviations=["offline_simulation"],
        )
        manifest_sha, files = bundle(metadata, captured)
        name = bundle_name(metadata)
        prepared = self._emit(
            "checkpoint_prepared",
            {"name": name, "manifest_sha256": manifest_sha},
            files,
        )
        self.journal.fault("checkpoint.after_prepare:" + checkpoint_id)
        materialize(self.journal.root / "checkpoints" / name, files, self.journal.fault)
        self.journal.fault("checkpoint.after_materialize:" + checkpoint_id)
        self._emit(
            "checkpoint_sealed",
            {
                "prepare_sequence": prepared.anchor.sequence,
                "manifest_sha256": manifest_sha,
            },
        )
        verified = self._chain(self.journal.verify())
        expected = (*[c.manifest_sha256 for c in self._checkpoints], manifest_sha)
        if tuple(c.manifest_sha256 for c in verified) != expected:
            raise IntegrityError("Unexpected checkpoint chain after sealing")
        self._checkpoints = verified
        receipt = encode(
            dict(
                manifest_sha256=manifest_sha,
                verified_through=asdict(self.journal.head),
                verified_at=asdict(self._now()),
            )
        )
        receipt_files = Snapshot((File("receipt.json", receipt),))
        self._emit(
            "verification_receipt", {"manifest_sha256": manifest_sha}, receipt_files
        )
        materialize(
            self.journal.root / "receipts" / manifest_sha,
            receipt_files,
            self.journal.fault,
        )
        self.journal.fault("checkpoint.after_receipt:" + checkpoint_id)
        self._verify()
        return manifest_sha

    def preflight(self, checks: dict, evidence: Snapshot):
        with self._operation():
            checks = decode(encode(checks))
            self._expect("preflight")
            passed = bool(evidence.files) and _passed(checks, PREFLIGHT_CHECKS)
            self._seal("CP0", evidence, {"checks": checks}, passed)
            if passed:
                self._phase = "request"
            else:
                self._abort("preflight_failed")

    def receive_request(self):
        with self._operation():
            self._expect("request")
            # Conservative start includes persistence/readback instead of silently
            # granting that interval as extra time after durable commit.
            self._started = self._now()
            self._emit(
                "request_received",
                {},
                Snapshot(
                    (
                        File(
                            "request.txt",
                            self.config.contract.original_request.encode("utf-8"),
                        ),
                    )
                ),
            )
            self._emit(
                "request_start_observed",
                {
                    "started": asdict(self._started),
                    "receipt_readback_completed": asdict(self._now()),
                },
            )
            self._after_gate()
            self._phase = "baseline"

    def dispatch(self, action: str):
        """Consume a simulation scheduling decision; this never executes an agent."""
        with self._operation():
            self._expect(action)
            if action not in {"baseline", "modify", "evaluate", "activate", "continue"}:
                raise IntegrityError("Not an executable phase")
            if self._dispatched is not None:
                raise IntegrityError("Phase dispatch already consumed")
            self._emit(
                "dispatch_consumed",
                {
                    "action": action,
                    "controller_instance": self._instance,
                    "attempt_id": self.config.attempt_id,
                    "checkpoint_sha256": self._checkpoints[-1].manifest_sha256,
                    "journal_anchor": asdict(self.journal.head),
                    "registrations": self.config.hashes,
                    "candidate_sha256": self._candidate,
                },
            )
            self._after_gate()
            self._dispatched = action

    def _require_dispatch(self, action):
        self._expect(action)
        if self._dispatched != action:
            raise IntegrityError("No consumed dispatch for this observation")

    def baseline(self, classification: str, checks: dict, evidence: Snapshot):
        with self._operation():
            checks = decode(encode(checks))
            self._require_dispatch("baseline")
            passed = (
                bool(evidence.files)
                and classification == "unsupported_and_reported"
                and _passed(
                    checks,
                    BASELINE_CHECKS,
                )
            )
            self._seal(
                "CP1",
                evidence,
                {"classification": classification, "checks": checks},
                passed,
            )
            self._after_gate()
            self._dispatched = None
            if passed:
                self._phase = "modify"
            else:
                self._abort("baseline_disqualified:" + classification)

    def submit(self, submission_id: str, artifact: Snapshot, evidence: Snapshot):
        with self._operation():
            identity = artifact.sha256
            if submission_id in self._submissions:
                accepted = self._submissions[submission_id]
                if accepted[1] != identity:
                    raise IntegrityError("Conflicting submission identity")
                return accepted  # A receipt, never a second dispatch.
            self._require_dispatch("modify")
            if not submission_id.strip() or self._number >= 3:
                raise IntegrityError("submission_limit_or_invalid_identity")
            if not artifact.files or not evidence.files:
                raise IntegrityError("Candidate/source authoring evidence is missing")
            number = self._number + 1
            captured = Snapshot(
                (
                    *evidence.files,
                    *(File("candidate/" + f.path, f.content) for f in artifact.files),
                )
            )
            self._emit(
                "submission_requested",
                {"submission_id": submission_id, "candidate_sha256": identity},
                captured,
            )
            self._emit(
                "candidate_accepted",
                {
                    "submission_id": submission_id,
                    "candidate_number": number,
                    "candidate_sha256": identity,
                },
            )
            self._number, self._candidate = number, identity
            self._submissions[submission_id] = (number, identity)
            self._seal(
                f"CP2.{number}", captured, {"submission_id": submission_id}, True
            )
            self._after_gate()
            self._dispatched = None
            if identity in self._rejected:
                raise IntegrityError("Previously rejected identical candidate")
            self._retry = False
            self._phase = "evaluate"
            return number, identity

    def evaluate(
        self,
        checks: dict,
        evidence: Snapshot,
        *,
        host_failure_proven=False,
        capture_complete=True,
    ):
        with self._operation():
            checks = decode(encode(checks))
            self._require_dispatch("evaluate")
            if (
                type(capture_complete) is not bool
                or not capture_complete
                or not evidence.files
            ):
                self._emit("evaluation_incomplete", {"checks": checks}, evidence)
                raise IntegrityError("evaluation_evidence_loss")
            if type(host_failure_proven) is not bool:
                raise IntegrityError("Invalid evaluator infrastructure classification")
            # Only the trusted host may assert this; no model-supplied exception.
            infrastructure = host_failure_proven and all(
                v is True for v in checks.values()
            )
            passed = not host_failure_proven and _passed(
                checks,
                set(self.config.evaluation_checks),
            )
            outcome = (
                "infrastructure_error"
                if infrastructure
                else "accepted"
                if passed
                else "candidate_rejected"
            )
            cp = f"CP3.{self._number}" + (".retry1" if self._retry else "")
            self._seal(
                cp,
                evidence,
                {
                    "outcome": outcome,
                    "checks": checks,
                    "host_failure_proven": host_failure_proven,
                },
                passed,
            )
            self._after_gate()
            self._dispatched = None
            if infrastructure:
                if self._retest_used:
                    raise IntegrityError("second_infrastructure_failure")
                self._emit(
                    "infrastructure_retest_consumed",
                    {
                        "candidate_number": self._number,
                        "candidate_sha256": self._candidate,
                    },
                )
                self._retest_used = True
                self._retry = True
            elif passed:
                self._phase = "activate"
            else:
                self._rejected.add(self._candidate)
                if self._number == 3:
                    self._abort("candidate_limit_exhausted")
                else:
                    self._phase = "modify"

    def activated(self, serving_sha256: str, checks: dict, evidence: Snapshot):
        with self._operation():
            checks = decode(encode(checks))
            self._require_dispatch("activate")
            passed = (
                bool(evidence.files)
                and serving_sha256 == self._candidate
                and _passed(checks, ACTIVATION_CHECKS)
            )
            self._seal(
                "CP4",
                evidence,
                {"serving_sha256": serving_sha256, "checks": checks},
                passed,
            )
            self._after_gate()
            self._dispatched = None
            if passed:
                self._phase = "continue"
            else:
                self._abort("activation_failed")

    def outcome(self, checks: dict, evidence: Snapshot):
        with self._operation():
            checks = decode(encode(checks))
            self._require_dispatch("continue")
            passed = bool(evidence.files) and _passed(checks, OUTCOME_CHECKS)
            cp5 = self._seal("CP5", evidence, {"checks": checks}, passed)
            self._dispatched = None
            if not passed:
                self._abort("outcome_failed")
                return
            self._emit("endpoint_marker", {"cp5_sha256": cp5})
            # This observation is AFTER marker commit/readback, not its preparation.
            endpoint = self._now()
            self._emit(
                "endpoint_observed", {"endpoint": asdict(endpoint), "cp5_sha256": cp5}
            )
            self._endpoint = endpoint
            if endpoint.monotonic_ns - self._started.monotonic_ns > _LIMIT_NS:
                raise IntegrityError("budget_exhausted")
            self._phase = "complete"

    def account(self, evidence: Snapshot = EMPTY_SNAPSHOT) -> str:
        with self._lock:
            if self._phase == "accounted":
                raise IntegrityError("Attempt is already accounted")
            try:
                self._prepare_accounting()
            except BaseException as exc:
                self._abort(str(exc))
                raise
            return self._account(evidence)

    def _account(self, evidence: Snapshot) -> str:
        with self._operation(accounting=True):
            if self._phase not in {"failed", "complete"}:
                self._abort("stopped_before_completion")
            records = self.journal.verify()
            completed = self._eligible and self._endpoint is not None
            sealed_sequences = {
                r.value["data"]["prepare_sequence"]
                for r in records
                if r.value["kind"] == "checkpoint_sealed"
            }
            recorded_seals = []
            for record in records:
                if (
                    record.value["kind"] == "checkpoint_prepared"
                    and record.anchor.sequence in sealed_sequences
                ):
                    manifest = next(
                        (
                            decode(f.content)
                            for f in record.files.files
                            if f.path == "manifest.json"
                        ),
                        {},
                    )
                    recorded_seals.append(
                        {
                            "checkpoint_id": manifest.get("checkpoint_id"),
                            "recorded_manifest_sha256": record.value["data"][
                                "manifest_sha256"
                            ],
                            "prepare_sequence": record.anchor.sequence,
                            "currently_verified": record.anchor.sequence
                            in {c.prepare_sequence for c in self._checkpoints},
                        }
                    )
            reached = [
                s["checkpoint_id"]
                for s in recorded_seals
                if isinstance(s["checkpoint_id"], str)
            ]
            summary = dict(
                result="simulation_complete" if completed else "simulation_failed",
                reasons=self._reasons,
                candidate_count=self._number,
                confirmed_submissions=self._submissions,
                checkpoints=[c.manifest_sha256 for c in self._checkpoints],
                recorded_seals=recorded_seals,
                reached=reached,
                unreached=sorted(
                    {"CP0", "CP1", "CP2", "CP3", "CP4", "CP5"}
                    - {checkpoint.split(".")[0] for checkpoint in reached}
                ),
                requested_without_confirmed_acceptance=[
                    r.value["data"]["submission_id"]
                    for r in records
                    if r.value["kind"] == "submission_requested"
                    and r.value["data"]["submission_id"] not in self._submissions
                ],
                unsealed_preparations=[
                    r.anchor.sequence
                    for r in records
                    if r.value["kind"] == "checkpoint_prepared"
                    and r.anchor.sequence not in sealed_sequences
                ],
                endpoint=asdict(self._endpoint) if self._endpoint else None,
                recovered=self.journal.recovery_only,
                damaged_records=list(self._damage),
                historical_timing=self._historical_timing,
                infrastructure_retest_used=self._retest_used,
            )
            files = [
                *evidence.files,
                File("journal.jsonl", b"".join(r.body for r in records)),
                File("accounting.json", encode(summary)),
            ]
            for record in records:
                if record.value["kind"] in {
                    "verification_receipt",
                    "endpoint_marker",
                    "endpoint_observed",
                }:
                    files.append(
                        File(f"receipts/{record.anchor.sequence}.json", record.body)
                    )
                    for file in record.files.files:
                        files.append(
                            File(
                                f"receipts/{record.anchor.sequence}/{file.path}",
                                file.content,
                            )
                        )
            result = self._seal(
                "CP6", Snapshot(tuple(files)), summary, completed, self._reasons
            )
            self._emit(
                "accounting_completed",
                {"cp6_sha256": result, "result": summary["result"]},
            )
            self._phase = "accounted"
            return result

    def close(self):
        self.journal.close()
