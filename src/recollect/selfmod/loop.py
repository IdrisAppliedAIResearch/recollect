"""Self-modification loop: from A's capability gap to the finished request.

Tests are authored and frozen first. Each attempt is a fresh round from A's
unchanged tree under due process, then a switch to an immutable B that resumes
the same request. Any failure, including an integrity failure, resets routing to
A, is journaled, and becomes feedback for the next attempt. Attempts continue
until one finishes the request or the user stops the run; there is no elapsed-time
or attempt limit. A failed reset is not survivable and ends the loop.
"""

import asyncio
import contextlib
from dataclasses import dataclass

from .contracts import TaskContract
from .deployment import SubagentBundle
from .journal import IntegrityError
from .round import ModificationRound, RoundConfig

FEEDBACK = 8
TERMINAL = {"completed", "blocked", "canceled", "interrupted"}


def continuation_brief(tests, gap):
    """B's brief for the resumed request; the request itself is sent separately."""
    tool = tests.interface["tool_name"]
    return (
        f"A new tool was added so this request can be done: {tool}, which "
        f"provides {gap.get('missing_capability')}.\n"
        f"1. Use {tool} to complete the request.\n"
        "2. The earlier work shows the previous worker could not do this. That is "
        "fixed: don't report the same gap again. If the new tool fails, report "
        "blocked with its error.\n"
        "3. If the tool needs the user's authorization for an external service, do "
        "everything else first, then finish with result: the feature is ready, "
        "plus the exact steps to authenticate."
    )


@dataclass(frozen=True)
class Outcome:
    finished: bool
    detail: str


def _reason(error):
    return f"{type(error).__name__}: {error}"[:2048]


class SelfModificationLoop:
    """Ports: ``author_tests(gap, stopped, record)``, ``develop(attempt, tests,
    feedback) -> candidate`` and a switch with ``activate(attempt, candidate,
    tests, gap)``
    and ``reset(reason)``."""

    def __init__(self, journal, *, author_tests, develop, switch, retry_pause=1.0):
        self._journal = journal
        self._author_tests, self._develop, self._switch = author_tests, develop, switch
        self._retry_pause = retry_pause
        self._stop = asyncio.Event()

    def stop(self):
        """User stop: no new authoring pass or attempt starts."""
        self._stop.set()

    async def _record(self, kind, data, files=None):
        extra = () if files is None else (files,)
        await asyncio.to_thread(self._journal.append, kind, data, *extra)

    async def _stopped(self):
        await self._record("loop_stopped", {})
        return Outcome(False, "stopped by the user")

    async def run(self, gap):
        await self._record("loop_started", {"gap": gap})
        tests = None
        while tests is None:
            if self._stop.is_set():
                return await self._stopped()
            try:
                tests = await self._author_tests(gap, self._stop.is_set, self._record)
            except Exception as error:
                await self._record("tests_failed", {"reason": _reason(error)})
                # A pause between failed passes, not a limit on agent work.
                await asyncio.sleep(self._retry_pause)
        feedback, attempt = [], 0
        while not self._stop.is_set():
            attempt += 1
            await self._record("attempt_started", {
                "attempt": attempt, "tests_sha256": tests.sha256,
                "feedback": feedback[-FEEDBACK:]})
            try:
                candidate = await self._develop(attempt, tests,
                                                tuple(feedback[-FEEDBACK:]))
                outcome = await self._switch.activate(
                    attempt, candidate, tests, gap)
            except asyncio.CancelledError:
                await self._switch.reset("cancelled")
                raise
            except Exception as error:
                outcome = Outcome(False, _reason(error))
            if outcome.finished:
                await self._record("attempt_finished", {"attempt": attempt,
                                                        "detail": outcome.detail})
                return outcome
            await self._switch.reset(outcome.detail)
            feedback.append(f"attempt {attempt}: {outcome.detail}")
            await self._record("attempt_failed", {"attempt": attempt,
                                                  "reason": outcome.detail})
        return await self._stopped()


class RoundDeveloper:
    """Each attempt is a fresh round from A's tree driven to one candidate."""

    def __init__(self, root, *, request, baseline, policy, settings, role_settings,
                 runtime_factory, model_factory=None):
        self._root, self._request = root, request
        self._baseline, self._policy, self._settings = baseline, policy, settings
        self._role_settings = role_settings
        self._runtime_factory, self._model_factory = runtime_factory, model_factory

    async def __call__(self, attempt, tests, feedback):
        contract = TaskContract(self._request, tests.contract_requirements, tests.names,
                                self._policy.sha256)
        config = RoundConfig(f"attempt-{attempt}", contract, self._baseline.sha256,
                             feedback)
        round_ = await asyncio.to_thread(ModificationRound.create,
                                         self._root / f"attempt-{attempt}", config)
        try:
            development = await asyncio.to_thread(lambda: round_.open_development(
                baseline=self._baseline, policy=self._policy,
                settings=self._settings))
            await development.run_until_ready(
                self._role_settings(tests.checks), self._runtime_factory,
                model_factory=self._model_factory)
            await asyncio.to_thread(
                lambda: development.submit(development.authorize("submit")))
            return round_.candidate
        finally:
            with contextlib.suppress(IntegrityError):
                await asyncio.to_thread(round_.close)


class DeploymentSwitch:
    """Build B, commit it, resume the original request on B and await its end."""

    def __init__(self, *, router, sandboxes, images, coordinator, session_id,
                 parent_task_id, request, base_image_id, launch, manager_factory,
                 poll_seconds=1.0):
        self._router, self._sandboxes, self._images = router, sandboxes, images
        self._coordinator, self._session_id = coordinator, session_id
        self._parent, self._request = parent_task_id, request
        self._base_image_id, self._launch = base_image_id, launch
        self._manager_factory, self._poll = manager_factory, poll_seconds

    async def activate(self, attempt, candidate, tests, gap):
        bundle = SubagentBundle(candidate, self._base_image_id, self._launch)
        image_id = await self._images.build(bundle)
        verified = await self._images.verify(bundle, image_id)
        await asyncio.to_thread(self._router.stage_b, verified)
        await asyncio.to_thread(self._router.begin_activation)
        manager = await asyncio.to_thread(self._manager_factory, verified)
        await asyncio.to_thread(self._sandboxes.register, "B", manager, verified)
        # The continuation is linked, and held, while the activation is open.
        task = await self._coordinator.submit(
            self._session_id, f"selfmod-{self._parent}-{attempt}",
            continuation_brief(tests, gap), self._request,
            parent_task_id=self._parent, continuation=True)
        await asyncio.to_thread(self._router.commit)
        await asyncio.to_thread(self._router.release_continuation, task["task_id"])
        await self._coordinator.release_held(self._session_id, task["task_id"])
        while True:
            final = await asyncio.to_thread(self._coordinator.store.get,
                                            self._session_id, task["task_id"])
            if final["state"] in TERMINAL:
                break
            # Observation cadence only; the resumed request has no deadline.
            await asyncio.sleep(self._poll)
        if final["state"] == "completed":
            return Outcome(True, "the resumed request completed on B")
        return Outcome(False, f"the resumed request ended {final['state']}: "
                              f"{final.get('progress') or ''}"[:2048])

    async def reset(self, reason):
        if self._router.live_b:
            await asyncio.to_thread(self._router.rollback, reason[:512])
