"""Offline host orchestration over a trusted fixture-runtime interface.

Receipts are not development approval, CP6, or deployment authority. Blocking
calls belong off the event loop; run_async coordinates cancellation and cleanup.
"""

import asyncio
import contextlib
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol

from .clock import Stamp, current_stamp
from .containment import (
    MAX_WIRE_BYTES,
    FixtureSpec,
    attest,
    frozen_input,
    release_record,
    verified_snapshot,
    verify_ready,
)
from .contracts import File, Snapshot
from .journal import EMPTY_SNAPSHOT, Anchor, IntegrityError, Journal, encode, sha256


@dataclass(frozen=True)
class Prepared:
    container_id: str
    input_dir: Path


@dataclass(frozen=True)
class Collected:
    report: bytes
    attachment_exitcode: int


@dataclass(frozen=True)
class Termination:
    run_id: str
    spec_sha256: str
    container_id: str | None
    confirmed: bool
    evidence: Snapshot


@dataclass(frozen=True)
class Deadline:
    monotonic_ns: int | None
    boot_id: str


class FixtureRuntime(Protocol):
    """Trusted adapter contract, not assertions accepted from the worker.

    Every operation enforces cancellation and clock continuity independently.
    A None deadline means unbounded work; finite fault fixtures enforce expiry.
    prepare creates a stopped container with only the supplied input snapshot;
    even if it raises, terminate must reconcile by unique spec labels/name.
    start returns exactly one bounded READY record, collect reads through EOF
    with a streaming byte cap. read_inputs captures host-owned input bytes.
    terminate must verify ownership and absence of *all* runnable descendants,
    including ambiguous creations, using its separate finite cleanup timeout.
    No method may invoke a host-process fallback or accept worker Docker flags.
    """

    def prepare(
        self, spec: FixtureSpec, inputs: Snapshot, deadline: Deadline
    ) -> Prepared: ...

    def inspect(self, worker: Prepared, deadline: Deadline) -> dict: ...

    def start(self, worker: Prepared, deadline: Deadline) -> bytes: ...

    def read_inputs(self, worker: Prepared, deadline: Deadline) -> Snapshot: ...

    def release(self, worker: Prepared, record: bytes, deadline: Deadline) -> None: ...

    def collect(
        self, worker: Prepared, limit: int, deadline: Deadline
    ) -> Collected: ...

    def terminate(
        self,
        spec: FixtureSpec,
        worker: Prepared | None,
        timeout_ms: int,
    ) -> Termination: ...

    def cancel(self) -> None:
        """Nonblocking sticky signal; cleanup must ignore this signal."""
        ...


@dataclass(frozen=True)
class FixtureReceipt:
    run_id: str
    snapshot: Snapshot
    archive_anchor: Anchor
    diagnostic_only: bool


@dataclass(frozen=True)
class RefreshGrant:
    grant_id: str
    failed_run_id: str
    failure_anchor: Anchor
    replacement: FixtureSpec


class FixtureExecutor:
    @classmethod
    def create(
        cls,
        root: Path,
        spec: FixtureSpec,
        *,
        original_started: Stamp,
        deadline_ns: int | None,
        max_refreshes: int,
        clock=current_stamp,
        fault=lambda _: None,
    ):
        if (
            (deadline_ns is not None and (
                type(deadline_ns) is not int
                or not original_started.monotonic_ns < deadline_ns
                <= original_started.monotonic_ns + 3600 * 1_000_000_000
            ))
            or type(max_refreshes) is not int
            or not 0 <= max_refreshes <= 3
        ):
            raise ValueError(
                "Freeze the inherited deadline and 0-3 diagnostic refreshes"
            )
        journal = Journal.create(root, fault=fault)
        try:
            result = cls(
                journal, spec, original_started, deadline_ns, max_refreshes, clock
            )
            result._record(
                "executor_opened",
                {
                    "mode": "offline_fixture",
                    "spec_sha256": spec.sha256,
                    "spec": spec.payload,
                    "original_started": asdict(original_started),
                    "deadline_ns": deadline_ns,
                    "max_refreshes": max_refreshes,
                },
                result._inputs,
            )
            result._now()
            return result
        except BaseException:
            journal.close()
            raise

    def __init__(self, journal, spec, started, deadline_ns, max_refreshes, clock):
        if journal.recovery_only or journal.verify():
            raise IntegrityError("Executor requires a new unclaimed journal")
        self.journal = journal
        self._spec, self._clock, self._last = spec, clock, started
        self._deadline_ns, self._max_refreshes = deadline_ns, max_refreshes
        self._inputs = frozen_input(spec)
        self._supervisor_sha = sha256(
            next(f.content for f in self._inputs.files if f.path == "supervisor.py")
        )
        self._state = "ready"
        self._failed = False
        self._clock_failed = False
        self._refreshes = 0
        self._failure_anchor = None
        self._grant = None
        self._lock = threading.Lock()
        self._api_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._stopped = False
        self._record("executor_claimed", {"owner_id": uuid.uuid4().hex})

    @property
    def spec(self) -> FixtureSpec:
        return self._spec

    @property
    def primary_failed(self) -> bool:
        return self._failed

    def verified_receipt(self, receipt: FixtureReceipt):
        """Host-only readback at handoff; a serialized receipt alone is not proof."""
        with self._idle(), self._exclusive():
            if (
                self._state != "complete" or self._failed
                or receipt.diagnostic_only is not False
                or receipt.run_id != self._spec.run_id
                or receipt.archive_anchor != self.journal.head
            ):
                raise IntegrityError("Receipt is not the current primary result")
            self._now(self._run_deadline)
            records = self.journal.verify()
            last = records[-1]
            if (
                last.anchor != receipt.archive_anchor
                or last.value["kind"] != "snapshot_verified"
                or last.files.sha256 != receipt.snapshot.sha256
                or last.value["data"]["snapshot_sha256"] != receipt.snapshot.sha256
                or any(r.value["data"]["diagnostic_only"] for r in records)
            ):
                raise IntegrityError("Receipt differs from sealed executor evidence")
            self._now(self._run_deadline)
            return records

    @contextlib.contextmanager
    def _exclusive(self):
        if not self._lock.acquire(blocking=False):
            raise IntegrityError("Executor operation already in progress")
        try:
            yield
        finally:
            self._lock.release()

    @contextlib.contextmanager
    def _idle(self):
        if not self._api_lock.acquire(blocking=False):
            raise IntegrityError("Executor operation already in progress")
        try:
            yield
        finally:
            self._api_lock.release()

    def _now(self, limit=None):
        if self._cancelled.is_set():
            raise InterruptedError("Executor caller cancelled")
        if self._clock_failed:
            raise IntegrityError("Executor clock continuity permanently lost")
        try:
            now = self._clock()
            if (
                not isinstance(now, Stamp)
                or now.boot_id != self._last.boot_id
                or now.monotonic_ns < self._last.monotonic_ns
            ):
                raise IntegrityError("Executor clock continuity lost")
        except BaseException:
            self._clock_failed = True
            raise
        self._last = now
        limits = [v for v in (self._deadline_ns, limit) if v is not None]
        if limits and now.monotonic_ns >= min(limits):
            raise IntegrityError("Executor deadline exhausted")
        return now

    def _record(self, kind, data, files=EMPTY_SNAPSHOT):
        return self.journal.append(
            kind,
            {
                **data,
                "run_id": self._spec.run_id,
                "diagnostic_only": self._failed,
                "last_clock": asdict(self._last),
            },
            files,
        )

    def _verify_stop(self, result, worker):
        if isinstance(result, Termination):
            self._record(
                "termination_observed",
                {
                    "reported_run_id": result.run_id,
                    "reported_spec_sha256": result.spec_sha256,
                    "container_id": result.container_id,
                    "confirmed": result.confirmed,
                },
                result.evidence,
            )
        if (
            not isinstance(result, Termination)
            or result.run_id != self._spec.run_id
            or result.spec_sha256 != self._spec.sha256
            or result.confirmed is not True
            or not result.evidence.files
            or worker is not None
            and result.container_id != worker.container_id
        ):
            raise IntegrityError("Namespace termination remains uncertain")
        self._record(
            "termination_verified",
            {
                "container_id": result.container_id,
                "spec_sha256": result.spec_sha256,
            },
            result.evidence,
        )

    def run(self, runtime: FixtureRuntime) -> FixtureReceipt:
        if not self._api_lock.acquire(blocking=False):
            raise IntegrityError("Executor operation already in progress")
        try:
            return self._run(runtime)
        finally:
            self._api_lock.release()

    async def run_async(self, runtime: FixtureRuntime) -> FixtureReceipt:
        """Do not deliver cancellation until cleanup and accounting resolve."""
        if not callable(getattr(runtime, "cancel", None)):
            raise ValueError("Async execution requires a cancellable runtime")
        if not self._api_lock.acquire(blocking=False):
            raise IntegrityError("Executor operation already in progress")
        try:
            task = asyncio.create_task(asyncio.to_thread(self._run, runtime))
            try:
                receipt = await asyncio.shield(task)
                try:
                    self._now(self._run_deadline)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    accounting = asyncio.create_task(
                        asyncio.to_thread(
                            self._account_cancel,
                            "ReceiptDeliveryFailed",
                            str(exc)[:2048],
                        )
                    )
                    await self._settle(accounting)
                    raise
                return receipt
            except asyncio.CancelledError:
                self._cancelled.set()
                hook_error = ""
                try:
                    runtime.cancel()
                except BaseException as exc:
                    hook_error = type(exc).__name__ + ": " + str(exc)[:2048]
                await self._settle(task)
                accounting = asyncio.create_task(
                    asyncio.to_thread(
                        self._account_cancel, "CallerCancelled", hook_error
                    )
                )
                await self._settle(accounting)
                if not accounting.result() or hook_error:
                    raise IntegrityError(
                        "Cancellation hook failed or cleanup/accounting unconfirmed"
                    ) from None
                raise
        finally:
            self._api_lock.release()

    @staticmethod
    async def _settle(task):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve even a CancelledError raised inside the synchronous worker.
        with contextlib.suppress(BaseException):
            task.result()

    def _account_cancel(self, error_type="CallerCancelled", error=""):
        with self._exclusive():
            self._failed = True
            self._state = "failed"
            self._failure_anchor = None
            try:
                record = self._record(
                    "fixture_failure_accounted",
                    {
                        "error_type": error_type,
                        "error": error,
                        "termination_confirmed": self._stopped,
                        "not_experiment_cp6": True,
                    },
                )
                if self._stopped:
                    self._failure_anchor = record.anchor
                return self._stopped
            except Exception:
                return False

    def _run(self, runtime: FixtureRuntime) -> FixtureReceipt:
        with self._exclusive():
            if self._state != "ready" or self.journal.recovery_only:
                raise IntegrityError("Run is consumed, closed or accounting-only")
            self._state = "running"
            self._stopped = False
            worker = None
            stopped = False
            try:
                self.journal.verify()
                start = self._now()
                limits = [v for v in (
                    self._deadline_ns,
                    start.monotonic_ns + self._spec.timeout_ms * 1_000_000
                    if self._spec.timeout_ms is not None else None,
                ) if v is not None]
                limit = min(limits) if limits else None
                self._run_deadline = limit
                deadline = Deadline(limit, start.boot_id)
                self._record("run_consumed", {"deadline": asdict(deadline)})
                self._now(limit)
                worker = runtime.prepare(self._spec, self._inputs, deadline)
                self._now(limit)
                source = worker.input_dir.absolute()
                archive = self.journal.root
                if (
                    source == archive
                    or source in archive.parents
                    or archive in source.parents
                ):
                    raise IntegrityError("Worker inputs overlap the host archive")
                inspection = runtime.inspect(worker, deadline)
                attest(inspection, self._spec, worker.input_dir, worker.container_id)
                state = inspection.get("State", {})
                if (
                    state.get("Status") != "created"
                    or state.get("Running") is not False
                    or type(state.get("Pid")) is not int
                    or state["Pid"] != 0
                ):
                    raise IntegrityError("Worker was not created stopped")
                self._now(limit)
                ready = runtime.start(worker, deadline)
                self._now(limit)
                verify_ready(ready, self._spec, self._supervisor_sha)
                inspection = runtime.inspect(worker, deadline)
                attest(inspection, self._spec, worker.input_dir, worker.container_id)
                state = inspection.get("State", {})
                if state.get("Running") is not True or state.get("Status") != "running":
                    raise IntegrityError("Supervisor is not waiting for release")
                if runtime.read_inputs(worker, deadline) != self._inputs:
                    raise IntegrityError("Frozen host inputs changed")
                self._now(limit)
                self._record(
                    "release_intent",
                    {},
                    Snapshot(
                        (
                            File("ready.json", ready),
                            File("inspection.json", encode(inspection)),
                        )
                    ),
                )
                now = self._now(limit)
                remaining = ((limit - now.monotonic_ns) // 1_000_000
                             if limit is not None else None)
                release = release_record(self._spec, self._supervisor_sha, remaining)
                runtime.release(worker, release, deadline)
                self._now(limit)
                self._record(
                    "release_sent", {}, Snapshot((File("release.json", release),))
                )
                self._now(limit)
                collected = runtime.collect(worker, MAX_WIRE_BYTES, deadline)
                # Retain available late evidence, but never return it as usable.
                if (
                    type(collected.report) is not bytes
                    or len(collected.report) > MAX_WIRE_BYTES
                ):
                    raise IntegrityError("Runtime violated bounded report contract")
                self._record(
                    "report_collected",
                    {
                        "attachment_exitcode": collected.attachment_exitcode,
                    },
                    Snapshot((File("report.json", collected.report),)),
                )
                self._now(limit)
                final = runtime.inspect(worker, deadline)
                observed = encode(final)
                truncated = len(observed) > 64 * 1024
                self._record(
                    "exit_observed",
                    {"truncated": truncated},
                    Snapshot((File("exit.json", observed[: 64 * 1024]),)),
                )
                if truncated:
                    raise IntegrityError("Runtime exit observation exceeds bound")
                attest(final, self._spec, worker.input_dir, worker.container_id)
                state = final.get("State", {})
                if (
                    type(collected.attachment_exitcode) is not int
                    or collected.attachment_exitcode != 0
                    or state.get("Status") != "exited"
                    or state.get("Running") is not False
                    or type(state.get("Pid")) is not int
                    or state["Pid"] != 0
                    or type(state.get("ExitCode")) is not int
                    or state["ExitCode"] != 0
                    or state.get("OOMKilled") is not False
                ):
                    raise IntegrityError("Supervisor exit was not clean")
                self._now(limit)
                termination = runtime.terminate(self._spec, worker, 3000)
                self._verify_stop(termination, worker)
                stopped = True
                self._stopped = True
                self._now(limit)
                snapshot = verified_snapshot(
                    collected.report, self._spec, self._supervisor_sha
                )
                receipt = self._record(
                    "snapshot_verified",
                    {
                        "snapshot_sha256": snapshot.sha256,
                    },
                    snapshot,
                )
                self.journal.verify()
                self._now(limit)
                self._state = "complete"
                return FixtureReceipt(
                    self._spec.run_id, snapshot, receipt.anchor, self._failed
                )
            except BaseException as exc:
                self._failed = True
                self._state = "failed"
                # Cancellation, broken clocks and archive failures must not bypass
                # cleanup. Its separate budget never extends result eligibility.
                if not stopped:
                    try:
                        termination = runtime.terminate(self._spec, worker, 3000)
                        self._verify_stop(termination, worker)
                        stopped = True
                    except BaseException:
                        stopped = False
                with contextlib.suppress(Exception):
                    record = self._record(
                        "fixture_failure_accounted",
                        {
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:2048],
                            "termination_confirmed": stopped,
                            "not_experiment_cp6": True,
                        },
                    )
                    if stopped:
                        self._failure_anchor = record.anchor
                self._stopped = stopped
                raise

    def authorize_refresh(self) -> RefreshGrant:
        """Explicit trusted-host action, never an automatic retry on failure."""
        with self._idle(), self._exclusive():
            if (
                self._state != "failed"
                or self._failure_anchor is None
                or self._grant is not None
                or self._refreshes >= self._max_refreshes
            ):
                raise IntegrityError(
                    "Refresh requires accounted failure and confirmed stop"
                )
            try:
                self.journal.verify()
                # Refresh after cancellation remains diagnostic. The primary
                # failure latch stays set while clock/deadline checks still apply.
                self._cancelled.clear()
                self._now()
                binding = replace(
                    self._spec.binding,
                    attempt_id=uuid.uuid4().hex,
                    instance_id=uuid.uuid4().hex,
                    revision=1,
                    artifact_sha256=None,
                )
                replacement = replace(
                    self._spec, run_id=uuid.uuid4().hex, binding=binding
                )
                grant = RefreshGrant(
                    uuid.uuid4().hex,
                    self._spec.run_id,
                    self._failure_anchor,
                    replacement,
                )
                self._record(
                    "diagnostic_refresh_authorized",
                    {
                        "grant_id": grant.grant_id,
                        "failed_run_id": grant.failed_run_id,
                        "failure_anchor": asdict(grant.failure_anchor),
                        "replacement_sha256": replacement.sha256,
                        "replacement": replacement.payload,
                    },
                )
                self._now()
                self._grant = grant
                return grant
            except BaseException:
                self._state = "closed"
                raise

    def refresh(self, grant: RefreshGrant):
        with self._idle(), self._exclusive():
            if self._state != "failed" or self._grant is None or grant != self._grant:
                raise IntegrityError("Unissued, stale or consumed refresh grant")
            self._state = "closed"
            self._grant = None
            self.journal.verify()
            self._now()
            self._record("diagnostic_refresh_consumed", {"grant_id": grant.grant_id})
            self._refreshes += 1
            self._spec = grant.replacement
            self._inputs = frozen_input(self._spec)
            if (
                sha256(
                    next(
                        f.content
                        for f in self._inputs.files
                        if f.path == "supervisor.py"
                    )
                )
                != self._supervisor_sha
            ):
                raise IntegrityError("Supervisor identity changed during refresh")
            self._record(
                "diagnostic_worker_prepared",
                {
                    "spec_sha256": self._spec.sha256,
                    "refresh_number": self._refreshes,
                },
                self._inputs,
            )
            self._now()
            self._failure_anchor = None
            self._state = "ready"

    def close(self):
        with self._idle(), self._exclusive():
            self._state = "closed"
            self.journal.close()
