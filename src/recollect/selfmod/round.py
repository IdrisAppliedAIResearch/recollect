"""One self-modification round: authority, audit journal and candidate intake.

A round starts from A's last known good tree after a capability gap. It owns the
development cycle's single lock, clock continuity and eligibility fence, and it
journals every step as a plain audit log. A failure closes the round for good;
recovery is a new round from A, never a repaired one. Submissions have no count
limit: evaluation, switch-over and retry belong to the self-modification loop.
The round never executes an agent itself.
"""

import contextlib
import threading
import uuid
from dataclasses import asdict, dataclass

from .clock import current_stamp
from .contracts import File, Snapshot, TaskContract, require_digest
from .journal import EMPTY_SNAPSHOT, Anchor, IntegrityError, Journal


@dataclass(frozen=True)
class RoundConfig:
    attempt_id: str
    contract: TaskContract
    baseline_sha256: str
    #: Why earlier attempts failed; every role sees it as ``prior_attempts``.
    feedback: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.attempt_id, str) or not self.attempt_id.strip():
            raise ValueError("Round ID required")
        if type(self.feedback) is not tuple or any(
                type(item) is not str or not item.strip() for item in self.feedback):
            raise ValueError("Feedback is a tuple of recorded failure reasons")
        if type(self.contract) is not TaskContract:
            raise ValueError("A round needs the original request's task contract")
        require_digest(self.baseline_sha256)


class ModificationRound:
    @classmethod
    def create(cls, root, config: RoundConfig, *, clock=current_stamp,
               fault=lambda _: None, trigger=None):
        """Open a round; ``trigger`` is the capability-gap report that started it."""
        journal = Journal.create(root, fault=fault)
        try:
            result = cls(journal, config, clock=clock)
            opened = result._emit("round_opened", {
                "config": asdict(config), "instance": result._instance,
                "trigger": trigger,
            })
            result._opening_sha256 = opened.anchor.sha256
            return result
        except BaseException:
            journal.close()
            raise

    def __init__(self, journal: Journal, config: RoundConfig, *, clock):
        self.journal, self.config, self._clock = journal, config, clock
        self._lock = threading.RLock()
        self._instance = uuid.uuid4().hex
        self._last = clock()
        self._started = self._last
        self._opening_sha256 = None
        self._eligible = True
        self._phase = "open"
        self._reasons: list[str] = []
        self._submissions: dict[str, tuple[int, str]] = {}
        self._number = 0
        self.candidate = None
        self._development = None
        self._development_pending = False
        self._development_lease_lock = threading.Lock()
        self._development_frozen = None
        self._development_updates = self._development_reviews = 0
        self._development_submission = None
        self._role_settings_sha256 = None
        self._role_model_calls = 0

    @property
    def eligible(self):
        return self._eligible

    @property
    def reasons(self):
        return tuple(self._reasons)

    def _now(self):
        now = self._clock()
        if (now.boot_id != self._last.boot_id
                or now.monotonic_ns < self._last.monotonic_ns):
            raise IntegrityError("Process clock identity/order lost")
        self._last = now
        return now

    def _emit(self, kind, data, files=EMPTY_SNAPSHOT):
        return self.journal.append(kind, {**data, "clock": asdict(self._now())}, files)

    def fail(self, reason: str) -> None:
        """Trusted host records a failure observed outside a development operation."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A failure needs a reason")
        with self._lock:
            if self._phase != "closed":
                self._abort(reason)

    def _abort(self, reason):
        # Coordinate revocation with async ownership release, without I/O here.
        with self._development_lease_lock:
            self._eligible = False
        if self._phase != "closed":
            self._phase = "failed"
        if reason not in self._reasons:
            self._reasons.append(reason)
        if not self.journal.poisoned and self._phase != "closed":
            # Eligibility stays false even if best-effort failure recording fails.
            with contextlib.suppress(Exception):
                self.journal.append("round_failed", {
                    "reason": reason, "last_clock": asdict(self._last)})

    @contextlib.contextmanager
    def _operation(self):
        with self._lock:
            if self._phase == "closed":
                raise IntegrityError("Round is closed")
            if not self._eligible or self.journal.recovery_only:
                raise IntegrityError("Round is permanently ineligible")
            try:
                self.journal.verify()
                self._now()
                yield
            except BaseException as exc:
                self._abort(str(exc))
                raise

    @contextlib.contextmanager
    def _development_operation(self, owner):
        with self._operation():
            if owner is not self._development:
                raise IntegrityError("Foreign or stale development authority")
            yield
            # Clock continuity must still hold when the gated step completes.
            self._now()

    def open_development(self, *, baseline, policy, settings):
        """Start a development cycle on A's tree; a new cycle fences the old one."""
        from .integration import IntegratedDevelopment

        with self._operation():
            if self._development_pending:
                raise IntegrityError("A development cycle is still settling")
            if baseline.sha256 != self.config.baseline_sha256:
                raise IntegrityError("Development baseline differs from A's tree")
            frozen = (policy.sha256, settings)
            if self._development_frozen not in (None, frozen):
                raise IntegrityError("Development scope/settings changed across cycles")
            self._development_frozen = frozen
            development = IntegratedDevelopment(
                self, baseline, policy, settings, self._opening_sha256,
                self._started, None,
            )
            self._development = development
            return development

    def _verify_segments(self, records):
        """Exhaust every bound native sidecar; a flattened copy is never trusted."""
        from .evidence_segments import iter_segments

        for record in records:
            value = record.value
            if value["kind"] != "development_execution":
                continue
            for binding in value["data"].get("segments") or ():
                parts = binding["path"].split("/")
                if (len(parts) != 3 or parts[0] != "segments"
                        or any(p in {"", ".", ".."} or "\\" in p for p in parts)):
                    raise IntegrityError("Invalid evidence segment binding")
                # An empty journal is bound with a null anchor, not omitted.
                anchor = (Anchor(**binding["anchor"])
                          if binding["anchor"] is not None else None)
                with contextlib.closing(iter_segments(
                    self.journal.root.joinpath(*parts), anchor,
                )) as stream:
                    for _ in stream:
                        pass

    def submit(self, submission_id, artifact: Snapshot, evidence: Snapshot, *,
               _development=None, _authorization=None):
        """Accept a candidate only through the development cycle's one-shot grant."""
        with self._operation():
            grant = self._development_submission
            if (
                grant is None or _development is not self._development
                or _authorization is not grant[0]
                or grant[1:] != (self._development, submission_id,
                                 artifact.sha256, evidence.sha256)
                or self._development_pending
            ):
                raise IntegrityError("Authenticated development submission required")
            self._development_submission = None
            if submission_id in self._submissions:
                accepted = self._submissions[submission_id]
                if accepted[1] != artifact.sha256:
                    raise IntegrityError("Conflicting submission identity")
                return accepted
            if not artifact.files or not evidence.files:
                raise IntegrityError("Candidate/source authoring evidence is missing")
            # Native sidecars are bound by anchor; exhaust them before accepting.
            self._verify_segments(self.journal.verify())
            number = self._number + 1
            self._emit("candidate_submitted", {
                "submission_id": submission_id, "candidate_number": number,
                "candidate_sha256": artifact.sha256,
            }, Snapshot((*evidence.files, *(File("candidate/" + f.path, f.content)
                                            for f in artifact.files))))
            self._number, self.candidate = number, artifact
            self._submissions[submission_id] = (number, artifact.sha256)
            return number, artifact.sha256

    def close(self):
        with self._lock:
            if self._development_pending:
                raise IntegrityError("Wait for development cleanup before closing")
            if self._phase == "closed":
                return
            if not self.journal.poisoned:
                with contextlib.suppress(Exception):
                    self.journal.append("round_closed", {
                        "eligible": self._eligible, "reasons": self._reasons,
                        "candidates": self._number})
            self._phase = "closed"
            self.journal.close()
