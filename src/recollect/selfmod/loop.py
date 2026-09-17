"""Self-modification loop: from A's capability gap to the finished request.

Tests are authored and frozen first. Each attempt develops from A's unchanged
tree, then switches to a B image that resumes the same request. Any failure
discards B and becomes feedback for the next attempt. When B finishes the
request, B becomes A. Attempts continue until one finishes or the user stops
the run; there is no elapsed-time or attempt limit.
"""

import asyncio
from dataclasses import dataclass

from .deployment import SubagentBundle

#: Harness defects, as opposed to a model or environment failure worth retrying.
BUGS = (TypeError, AttributeError, NameError, ImportError, IndentationError)
FEEDBACK = 8
#: Longest pause between attempts that keep failing the same way.
MAX_PAUSE = 300.0
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
    #: The user stopped the run, for example by canceling the resumed request.
    stopped: bool = False
    #: What the worker on B actually saw, for the next attempt to act on.
    evidence: str = ""


def _worker_rows(store, session_id, task_id):
    try:
        return store.messages(session_id, task_id)
    except Exception:  # a store failure must not mask the attempt's own result
        return []


def _tool_errors(rows):
    """{(tool, first line of the error): how many times it repeated}."""
    errors = {}
    for row in rows:
        payload = row.get("payload") or {}
        if row.get("kind") != "tool":
            continue
        observation = " ".join(str(payload.get("observation") or "").split())
        if observation.lower().startswith("error"):
            key = (payload.get("tool") or "tool", observation[:240])
            errors[key] = errors.get(key, 0) + 1
    return errors


def worker_evidence(store, session_id, task_id, *, tools=4, bound=1200):
    """What the failed worker's own tool calls showed, deduplicated and short.

    An attempt that only hears "the request did not finish" rebuilds the same
    interface. The repeated tool errors name the real defect.
    """
    rows = _worker_rows(store, session_id, task_id)
    errors, worked, last = _tool_errors(rows), 0, ""
    for row in rows:
        payload = row.get("payload") or {}
        observation = " ".join(str(payload.get("observation") or "").split())
        if row.get("kind") == "tool":
            if observation and not observation.lower().startswith("error"):
                worked += 1
        elif row.get("kind") in {"blocked", "result"}:
            last = " ".join(str(payload.get("text") or "").split())[:240]
    lines = []
    for (tool, observation), count in sorted(errors.items(), key=lambda i: -i[1])[
            :tools]:
        lines.append(f"- {tool} failed {count}x: {observation}")
    if worked:
        lines.append(f"- {worked} tool call(s) returned without an error")
    if last:
        lines.append(f"- it stopped saying: {last}")
    return "\n".join(lines)[:bound]


def _reason(error):
    return f"{type(error).__name__}: {error}"[:2048]


class SelfModificationLoop:
    """Ports: ``author_tests(gap, stopped, record)``, ``develop(attempt, tests,
    feedback) -> candidate`` and a switch with ``activate(attempt, candidate,
    tests, gap)``
    and ``reset(reason)``."""

    def __init__(self, *, author_tests, develop, switch, retry_pause=1.0,
                 on_event=None):
        # Async observer of each event, for user-visible milestones.
        self._on_event = on_event
        self._author_tests, self._develop, self._switch = author_tests, develop, switch
        self._retry_pause = retry_pause
        self._stop = asyncio.Event()

    def stop(self):
        """User stop: no new authoring pass or attempt starts."""
        self._stop.set()

    async def _record(self, kind, data):
        if self._on_event is not None:
            await self._on_event(kind, data)

    async def _stopped(self):
        await self._record("loop_stopped", {})
        return Outcome(False, "stopped by the user", stopped=True)

    async def run(self, gap):
        await self._record("loop_started", {"gap": gap})
        tests = None
        while tests is None:
            if self._stop.is_set():
                return await self._stopped()
            try:
                tests = await self._author_tests(gap, self._stop.is_set, self._record)
            except BUGS as error:
                # A defect in this harness never becomes a transient failure to
                # retry: it would call the model forever and never succeed.
                await self._record("loop_failed", {"reason": _reason(error)})
                return Outcome(False, _reason(error))
            except Exception as error:
                await self._record("tests_failed", {"reason": _reason(error)})
                # A pause between failed passes, not a limit on agent work.
                await asyncio.sleep(self._retry_pause)
        feedback, attempt = [], 0
        last_reason, repeats = None, 0
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
            if outcome.stopped:
                await self._switch.reset("stopped by the user")
                return await self._stopped()
            if outcome.finished:
                await self._record("attempt_finished", {"attempt": attempt,
                                                        "detail": outcome.detail})
                return outcome
            await self._switch.reset(outcome.detail)
            feedback.append(f"attempt {attempt}: {outcome.detail}"
                            + (f"\nwhat its worker saw:\n{outcome.evidence}"
                               if outcome.evidence else ""))
            repeats = repeats + 1 if outcome.detail == last_reason else 1
            last_reason = outcome.detail
            # Pacing, not a limit: an identical failure waits longer each time,
            # and a new failure reason starts again from the shortest pause.
            pause = min(self._retry_pause * 2 ** (repeats - 1), MAX_PAUSE)
            await self._record("attempt_failed", {"attempt": attempt,
                                                  "reason": outcome.detail,
                                                  "repeats": repeats,
                                                  "pause_s": pause})
            await asyncio.sleep(pause)
        return await self._stopped()


class DeploymentSwitch:
    """Build B, resume the original request on it, then promote or discard B.

    ``stage(verified)`` returns B's Deployment with its sandbox manager;
    ``promote(candidate, tests)`` saves the build and makes B serve as A;
    ``discard(deployment)`` stops B and removes its image and directories.
    """

    def __init__(self, *, deployments, images, coordinator, session_id,
                 parent_task_id, request, base_image_id, launch, stage, promote,
                 discard, poll_seconds=1.0, on_event=None):
        self._deployments, self._images = deployments, images
        self._coordinator, self._session_id = coordinator, session_id
        self._parent, self._request = parent_task_id, request
        self._base_image_id, self._launch = base_image_id, launch
        self._stage, self._promote, self._discard = stage, promote, discard
        self._poll, self._on_event = poll_seconds, on_event
        #: A B image built this attempt that no deployment owns yet.
        self._unstaged = None

    async def activate(self, attempt, candidate, tests, gap):
        bundle = SubagentBundle(candidate, self._base_image_id, self._launch)
        self._unstaged = await self._images.build(bundle)
        verified = await self._images.verify(bundle, self._unstaged)
        self._deployments.stage_b(await asyncio.to_thread(self._stage, verified))
        self._unstaged = None
        task = await self._coordinator.submit(
            self._session_id, f"selfmod-{self._parent}-{attempt}",
            continuation_brief(tests, gap), self._request,
            parent_task_id=self._parent, continuation=True)
        await self._coordinator.release_held(self._session_id, task["task_id"])
        if self._on_event is not None:
            await self._on_event("resuming", {"attempt": attempt,
                                              "task_id": task["task_id"]})
        while True:
            final = await asyncio.to_thread(self._coordinator.store.get,
                                            self._session_id, task["task_id"])
            if final["state"] in TERMINAL:
                break
            # Observation cadence only; the resumed request has no deadline.
            await asyncio.sleep(self._poll)
        store, task_id = self._coordinator.store, task["task_id"]
        if final["state"] == "completed":
            await self._promote(candidate, tests)
            return Outcome(True, "the resumed request completed on B")
        if final["state"] == "canceled":
            return Outcome(False, "the resumed request was canceled", stopped=True)
        evidence = await asyncio.to_thread(worker_evidence, store,
                                           self._session_id, task_id)
        return Outcome(False, f"the resumed request ended {final['state']}: "
                              f"{final.get('progress') or ''}"[:2048],
                       evidence=evidence)

    async def reset(self, reason):
        """Scrap this attempt's B: nothing of it outlives the attempt."""
        image, self._unstaged = self._unstaged, None
        if image is not None and image != self._deployments.a.image_id:
            await self._images.remove(image)
        b = self._deployments.discard_b()
        if b is not None:
            await self._discard(b)
