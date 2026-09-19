"""The generic "do this at time T" primitive.

A tool answers a call; nothing built as a tool can act on its own when a
moment arrives. For that to be possible at all, the harness provides one
generic timer: register a job for an aware point in time, and this module
hands the job to an injected ``deliver`` when it comes due. It knows nothing
about reminders, calendars or channels — what a fired job *means* lives
entirely in the deliverer the host wired in.

The schedule is durable outside the process that wrote it: a job due while
the server was down is delivered late, exactly once, when the next scheduler
starts — a local timer cannot act while nothing runs. Each job is claimed
(marked fired and saved) *before* delivery, so a crash mid-delivery loses
that one job rather than repeating it forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = 1
#: Pending jobs are a queue, not a residence: room for a device's reminders,
#: small enough for one scan per wake.
MAX_PENDING = 256
#: A finished job only has to live long enough to answer "did it run?".
MAX_FINISHED = 50
MAX_PAYLOAD_BYTES = 4096
#: The wake loop caps its sleep so a lost wakeup self-corrects.
MAX_SLEEP_SECONDS = 60.0
STATUSES = {"pending", "fired", "failed", "canceled"}

_LOG = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _finished_at(job: dict) -> str:
    return job.get("fired_at") or job.get("canceled_at") or job["created_at"]


class ScheduleStore:
    """The schedule as one small file, atomically written so that a
    half-written save is never loadable."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> list[dict]:
        """The saved jobs, or ``[]`` when nothing has been written yet.

        Anything unreadable or malformed is a ``ValueError``, never a
        structural exception escaping to the caller.
        """
        if not self._path.exists():
            return []
        try:
            record = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("The schedule file could not be read.") from error
        if not isinstance(record, dict) or record.get("schema") != SCHEMA:
            raise ValueError("Unrecognized schedule record")
        jobs = record.get("jobs")
        if not isinstance(jobs, list):
            raise ValueError("Malformed schedule record")
        for job in jobs:
            if (not isinstance(job, dict) or not isinstance(job.get("job_id"), str)
                    or not isinstance(job.get("payload"), dict)
                    or job.get("status") not in STATUSES
                    or not isinstance(job.get("due_at"), str)
                    or not isinstance(job.get("created_at"), str)
                    or any(job.get(stamp) is not None
                           and not isinstance(job.get(stamp), str)
                           for stamp in ("fired_at", "canceled_at"))):
                raise ValueError("Malformed schedule record")
            try:
                due = datetime.fromisoformat(job["due_at"])
            except ValueError as error:
                raise ValueError("Malformed schedule record") from error
            # A naive time parses fine but cannot be ordered against the
            # loop's aware clock; reject it here, not as a runtime error.
            if due.tzinfo is None:
                raise ValueError("Malformed schedule record")
        return jobs

    def save(self, jobs: list[dict]) -> None:
        """Write the whole schedule, atomically.

        This small, bounded file (MAX_PENDING + MAX_FINISHED jobs, payloads
        capped at MAX_PAYLOAD_BYTES) is written once per booking or delivery,
        never per tick, so it is a deliberate exception to "blocking work
        goes through asyncio.to_thread": offloading it would put the job
        list and this staging file into the thread pool, needing a lock the
        single-threaded design otherwise does not have.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"schema": SCHEMA, "jobs": jobs}, sort_keys=True,
                          ensure_ascii=False).encode("utf-8")
        staging = self._path.with_suffix(".json.tmp")
        staging.write_bytes(data)
        staging.replace(self._path)


class Scheduler:
    """Delivers persisted jobs when they come due, oldest first.

    ``deliver`` is awaited with the job dict. An exception marks that job
    failed with the reason and the loop carries on — one broken job neither
    kills the timer nor retries forever.
    """

    def __init__(self, store: ScheduleStore, deliver, *,
                 max_pending: int = MAX_PENDING) -> None:
        self._store = store
        self._deliver = deliver
        self._max_pending = max_pending
        try:
            self._jobs = store.load()
        except ValueError:
            # A corrupt schedule must not take the server down: jobs are
            # ephemeral, so the file starts fresh and the loss is logged.
            _LOG.warning("The schedule file was unreadable; starting empty.")
            self._jobs = []
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def schedule(self, due_at: datetime, payload: dict) -> dict:
        """Remember ``payload`` for the aware moment ``due_at``. A moment
        already past is due on the next tick."""
        if not isinstance(due_at, datetime) or due_at.tzinfo is None:
            raise ValueError("A scheduled job needs a timezone-aware time.")
        if not isinstance(payload, dict):
            raise ValueError("A scheduled job needs an object payload.")
        try:
            encoded = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValueError("A job payload must be JSON.") from error
        if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"A job payload is limited to "
                             f"{MAX_PAYLOAD_BYTES} bytes.")
        if len(self.pending()) >= self._max_pending:
            raise ValueError(f"At most {self._max_pending} jobs wait at once.")
        job = {"job_id": uuid.uuid4().hex, "due_at": due_at.isoformat(),
               "payload": payload, "status": "pending",
               "created_at": _now().isoformat()}
        self._jobs.append(job)
        self._save()
        self._wake.set()
        return dict(job)

    def cancel(self, job_id: str) -> dict:
        for job in self._jobs:
            if job["job_id"] == job_id:
                if job["status"] != "pending":
                    raise ValueError(f"Job {job_id} is already {job['status']}.")
                job["status"] = "canceled"
                job["canceled_at"] = _now().isoformat()
                self._save()
                return dict(job)
        raise KeyError(f"No such scheduled job: {job_id}")

    def pending(self) -> list[dict]:
        return [dict(job) for job in self._jobs if job["status"] == "pending"]

    def _due(self, now: datetime) -> list[dict]:
        return sorted(
            (job for job in self._jobs
             if job["status"] == "pending"
             and datetime.fromisoformat(job["due_at"]) <= now),
            key=lambda job: datetime.fromisoformat(job["due_at"]))

    async def deliver_due(self, now: datetime | None = None) -> int:
        """Deliver every job due by ``now``, oldest first; return how many."""
        delivered = 0
        for job in self._due(now or _now()):
            job["status"] = "fired"
            job["fired_at"] = _now().isoformat()
            self._save()
            try:
                await self._deliver(job)
            except Exception as error:  # noqa: BLE001 - one job's failure, logged
                job["status"] = "failed"
                job["error"] = f"{type(error).__name__}: {error}"[:500]
            self._save()
            delivered += 1
        return delivered

    async def start(self, *, idle_seconds: float = 1.0) -> None:
        """Wake on the next due job (or when a job is added), never before."""
        self._task = asyncio.create_task(self._run(idle_seconds))

    async def _run(self, idle_seconds: float) -> None:
        while True:
            await self.deliver_due()
            upcoming = self.pending()
            delay = idle_seconds
            if upcoming:
                next_due = min(datetime.fromisoformat(job["due_at"])
                               for job in upcoming)
                delay = max((next_due - _now()).total_seconds(), 0.0)
            delay = min(delay, MAX_SLEEP_SECONDS)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)
            self._wake.clear()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _save(self) -> None:
        pending = [job for job in self._jobs if job["status"] == "pending"]
        finished = [job for job in self._jobs if job["status"] != "pending"]
        if len(finished) > MAX_FINISHED:
            finished = sorted(finished, key=_finished_at)[-MAX_FINISHED:]
        self._jobs = pending + finished
        self._store.save(self._jobs)
