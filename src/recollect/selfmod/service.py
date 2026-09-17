"""Application wiring: A's deployment, the capability-gap hook and one loop.

The app owns these objects; a worker never reaches them. Preparing builds A's
bundle image from the repository tree, removes every other bundle image and the
directories an earlier start left, and serves delegated work from A. A
structured gap report from A then stops A's execution, keeps its task
cancelable, and asks the user whether to build the capability. Only an explicit
yes, from chat or the task card, runs exactly one loop, announced at each
milestone. Canceling the task stops the loop immediately. A second report while
one waits or runs is declined, not queued.

When B finishes the request, its files are committed on a new git branch and B
serves as A; the replaced A is torn down once its work settles. A B that fails,
is canceled or is interrupted by a restart leaves nothing behind.
"""

import asyncio
import contextlib
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..connections import GUIDE as connection_guide_text
from ..connections import ConnectionService, GoogleAccount, google_store
from ..engine.sandbox.manager import SandboxDeployment, SandboxManager
from .agents import AgentDeveloper, DockerChecks
from .deployment import (
    BundleImages,
    Deployment,
    Deployments,
    SubagentBundle,
    materialize_skills,
)
from .docker import pinned_docker, resolve_image
from .loop import DeploymentSwitch, Outcome, SelfModificationLoop
from .promotion import PromotionError, promote
from .subagent_tree import (
    BUNDLE_PYTHONPATH,
    LAUNCH,
    PROTECTED,
    baseline,
    change_policy,
)
from .tests_first import authoring, model_completer

_LOG = logging.getLogger(__name__)
GAP_NOTICE = ("Building the capability now. I'll pick your request back up when "
              "it's ready.")


def _capability(gap):
    text = str(gap.get("missing_capability") or "a capability this request needs")
    text = text.strip().rstrip(".")
    # "Issue an HTTP request" reads as "issue an HTTP request" mid-sentence.
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        text = text[0].lower() + text[1:]
    return text


def proposal_notice(gap):
    return (f"I can't do that yet. The missing capability: {_capability(gap)}. "
            "Want me to build it? If you say yes, I'll pick your request back up "
            "once it works.")


def declined_notice(gap):
    return (f"Okay, I won't build it. This request stays blocked without "
            f"this capability: {_capability(gap)}.")


def feature_name(tool_name):
    """``http_request`` reads as "HTTP request" when spoken."""
    words = str(tool_name or "").replace("-", "_").split("_")
    spoken = [w.upper() if w.lower() in {"http", "https", "url", "api", "json", "sms"}
              else w for w in words if w]
    return " ".join(spoken) or None


def describe(kind, data):
    """One line for the implementation workspace, or None for internal events."""
    attempt = data.get("attempt")
    if kind == "tests_frozen":
        return (f"Tests frozen: {data.get('checks')} checks for "
                f"{data.get('tool_name')}.")
    if kind == "loop_failed":
        return f"The build stopped on a fault in Recollect: {_line(data.get('reason'))}"
    if kind == "tests_failed":
        return f"Writing tests failed, retrying: {_line(data.get('reason'))}"
    if kind == "attempt_started":
        return f"Attempt {attempt} started."
    if kind == "plan_unreadable":
        return "The planner's reply had no readable plan; asked again."
    if kind == "plan_outside_policy":
        return ("The plan changes files it may not touch; asked to revise: "
                + ", ".join(data.get("changes", [])))
    if kind == "plan_review":
        return "Plan review: " + _verdict(data)
    if kind == "plan_approved":
        plan = data.get("plan") or {}
        files = ", ".join(f"{c.get('operation')} {c.get('path')}"
                          for c in plan.get("changes", []))
        return f"Plan approved: {_line(plan.get('summary'))} ({files})"
    if kind == "implementation_turn":
        return "Implementer: " + _line(data.get("reply"), 400)
    if kind == "no_changes":
        return "The implementer changed nothing; asked again."
    if kind == "policy_violations":
        return ("Changes outside the allowed files, sent back: "
                + ", ".join(data.get("changes", [])))
    if kind == "checks":
        results = data.get("results", [])
        passed = sum(1 for r in results if r.get("passed"))
        failed = [r.get("name") for r in results if not r.get("passed")]
        return (f"Checks (no network): {passed}/{len(results)} passed"
                + (f"; failed: {', '.join(failed)}" if failed else "."))
    if kind == "code_review":
        return "Code review: " + _verdict(data)
    if kind == "candidate":
        return "Candidate ready: " + ", ".join(data.get("changes", []))
    if kind == "attempt_failed":
        return f"Attempt {attempt} failed: {_line(data.get('reason'))}"
    if kind == "resuming":
        return "The new capability passed. Resuming the request."
    if kind == "attempt_finished":
        return "The resumed request finished."
    if kind == "promoted":
        return (f"Saved as branch {data.get('branch')}; it now serves new work. "
                f"Roll back with: git switch {data.get('previous')}")
    if kind == "promotion_failed":
        return ("It serves new work until Recollect restarts, but saving it "
                f"failed: {_line(data.get('reason'))}")
    if kind == "loop_stopped":
        return "Stopped."
    return None


def _line(value, limit=240):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _verdict(data):
    if data.get("approved"):
        return "approved."
    issues = [_line(f.get("issue"), 160) for f in data.get("findings", [])
              if isinstance(f, dict)]
    return "changes requested: " + ("; ".join(issues) or "no details")


STOPPED_NOTICE = "Stopped building the capability."


def connection_guide():
    return connection_guide_text


class SelfModificationService:
    def __init__(self, config, coordinator, *, repository, root, images,
                 base_image_id, role_endpoint, role_model, manager_factory=None,
                 role_slot=None, sandbox_root=None, model_slot=None,
                 model_base_url=None, model_api_key=None, loop_factory=None,
                 completer=model_completer, docker=None, run_checks=None,
                 development_manager_factory=None, connections=None,
                 promote=promote, retire_poll=1.0):
        self._config, self._coordinator = config, coordinator
        self._repository, self._root = Path(repository), Path(root)
        self._images = images
        self._base_image_id = base_image_id
        self._role_endpoint, self._role_model = role_endpoint, role_model
        self._role_slot, self._model_slot = role_slot, model_slot
        self._manager_factory = manager_factory or self._sandbox_manager
        self._model_base_url, self._model_api_key = model_base_url, model_api_key
        self._loop_factory = loop_factory or self._build_loop
        self._completer, self._promote_files = completer, promote
        self._docker, self._run_checks = docker, run_checks
        #: The connected-account service; worker sandboxes (A and B) get its key.
        self._connections = connections
        self._development_manager_factory = (development_manager_factory
                                             or self._development_manager)
        # Sandbox directories must sit outside any git repository: opencode
        # scopes the project to the enclosing repo root, so a root under the
        # app's data directory would hand the worker this whole codebase. Every
        # self-modification directory lives under this one, emptied at start.
        self._workspace = Path(
            sandbox_root or getattr(config, "sandbox_root", None) or self._root
        ) / "selfmod"
        self.deployments = None
        self.baseline = self.policy = None
        self._lock = asyncio.Lock()
        self._loop = self._job = self._runner = None
        self._retiring = set()
        self._retire_poll = retire_poll
        #: The gap waiting for the user's go/no-go; at most one at a time.
        self._proposal = None
        self._stop_requested = False
        self._notices = 0
        #: What the running or last loop is doing, for the status API.
        self.status = {"state": "idle"}
        #: What the implementation workspace shows for the latest build.
        self.activity = []
        # On one model slot the loop queues behind conversation turns; with
        # three it takes the modifier lane.
        self._admission = model_slot if hasattr(model_slot, "slot_for") else None
        self._lane = "modifier" if role_slot is not None else "worker"

    def _record(self, kind, data):
        _LOG.info("selfmod %s %s", kind, data)

    def _directory(self, prefix):
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace / f"{prefix}-{uuid.uuid4().hex[:12]}"

    def _sandbox_manager(self, verified, name):
        skills = materialize_skills(verified.bundle, self._directory("skills-" + name))
        manager = SandboxManager(self._config, model_slot=self._model_slot,
                                 connections=(
                                     None if self._connections is None else
                                     (self._connections.base_url,
                                      self._connections.key)),
                                 deployment=SandboxDeployment(
                                     verified.image_id, skills,
                                     self._directory("root-" + name),
                                     python_path=BUNDLE_PYTHONPATH))
        if self._model_base_url is not None:
            manager.configure_model(self._model_base_url, self._model_api_key)
        return manager

    def _deployment(self, verified, role):
        manager = self._manager_factory(verified, role.lower())
        pinned = getattr(manager, "deployment", None)
        paths = () if pinned is None else (pinned.skills_source, pinned.root)
        return Deployment(role, verified, manager, paths)

    def _development_manager(self, name):
        """Stock OpenCode on the base image, in a sandbox root of its own."""
        empty = self._directory("dev-skills-" + name)
        empty.mkdir()
        manager = SandboxManager(
            self._config, model_slot=self._model_slot, development=True,
            deployment=SandboxDeployment(self._base_image_id, empty,
                                         self._directory("dev-" + name)))
        if self._model_base_url is not None:
            manager.configure_model(self._model_base_url, self._model_api_key)
        return manager

    async def prepare(self):
        """Serve A from its own bundle image; nothing from an earlier start stays."""
        await asyncio.to_thread(shutil.rmtree, self._workspace, ignore_errors=True)
        self.baseline = await asyncio.to_thread(baseline, self._repository)
        self.policy = change_policy(self.baseline)
        bundle = SubagentBundle(self.baseline, self._base_image_id, LAUNCH)
        image_id = await self._images.build(bundle)
        verified = await self._images.verify(bundle, image_id)
        removed = await self._images.sweep(keep=verified.image_id)
        a = await asyncio.to_thread(self._deployment, verified, "A")
        self.deployments = Deployments(a)
        self._coordinator.deployments = self.deployments
        self._coordinator.on_gap = self.handle_gap
        self._coordinator.on_cancel = self.cancel_requested
        self._coordinator.build_proposal = self.proposal
        self._record("a_serving", {"image_id": verified.image_id,
                                   "removed_images": removed})
        return verified

    def _guide(self):
        return None if self._connections is None else connection_guide()

    async def _promote(self, candidate, tests):
        """Save B's files on a new branch, then let B serve as A."""
        feature = self.status.get("feature") or tests.interface.get("tool_name")
        try:
            saved = await asyncio.to_thread(self._promote_files, self._repository,
                                            self.baseline, candidate, feature)
        except (PromotionError, OSError, ValueError) as error:
            await self._on_event("promotion_failed", {"reason": str(error)})
        else:
            self.status["branch"] = saved.branch
            await self._on_event("promoted", {"branch": saved.branch,
                                              "previous": saved.previous,
                                              "paths": list(saved.paths)})
        self.baseline = candidate
        self.policy = change_policy(candidate)
        self._retire(self.deployments.promote())

    def _retire(self, deployment):
        """Tear a replaced A down in the background once its work settles."""
        task = asyncio.create_task(self._discard(deployment))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def _discard(self, deployment):
        manager = deployment.manager
        # The running task finishes on the deployment it started on.
        while getattr(self._coordinator, "_active_manager", None) is manager:
            await asyncio.sleep(self._retire_poll)
        try:
            teardown = getattr(manager, "teardown", None)
            if teardown is not None:
                await teardown()
            await self._images.remove(deployment.image_id)
        except Exception as error:
            _LOG.warning("Could not remove deployment %s: %s",
                         deployment.image_id, error)
        for path in deployment.paths:
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)

    def _build_loop(self, session_id, task_id, request, gap):
        admitted = {"admission": self._admission, "lane": self._lane}
        author = self._completer(self._role_endpoint, self._role_model,
                                 slot=self._role_slot, **admitted)
        reviewer = self._completer(self._role_endpoint, self._role_model,
                                   slot=self._role_slot, **admitted)
        develop = AgentDeveloper(
            request=request, gap=gap,
            baseline=self.baseline, policy=self.policy, protected=PROTECTED,
            manager_factory=self._development_manager_factory,
            run_checks=self._run_checks or DockerChecks(
                self._docker, self._base_image_id),
            on_event=self._on_event, connections=self._guide(),
        )
        switch = DeploymentSwitch(
            deployments=self.deployments, images=self._images,
            coordinator=self._coordinator, session_id=session_id,
            parent_task_id=task_id, request=request,
            base_image_id=self._base_image_id, launch=LAUNCH,
            stage=lambda verified: self._deployment(verified, "B"),
            promote=self._promote, discard=self._discard,
            on_event=self._on_event,
        )
        return SelfModificationLoop(
            author_tests=authoring(
                request, baseline=self.baseline, policy=self.policy,
                author=author, reviewer=reviewer, connections=self._guide()),
            develop=develop, switch=switch, on_event=self._on_event)

    def proposal(self, session_id, task_id):
        """The capability build waiting for the user's go/no-go on this task."""
        pending = self._proposal
        if pending and pending["session_id"] == session_id and pending[
                "task_id"] == task_id:
            return {"missing_capability": pending["gap"].get("missing_capability"),
                    "modification_request": pending["gap"].get(
                        "modification_request")}
        return None

    async def handle_gap(self, session_id, gap):
        """A's gap pauses its task and asks the user; nothing is built without a yes."""
        task_id = gap.get("task_id")
        async with self._lock:
            building = self._runner is not None and not self._runner.done()
            if building or self._loop is not None or self._proposal is not None:
                self._record("gap_declined", {
                    "task_id": task_id,
                    "reason": "another capability build is waiting or running"})
                return None
            self._proposal = {"session_id": session_id, "task_id": task_id,
                              "gap": gap}
            self.activity = []
            now = datetime.now(UTC).isoformat()
            self.status = {"state": "awaiting_approval", "session_id": session_id,
                           "task_id": task_id, "continuation_task_id": None,
                           "attempt": 0,
                           "missing_capability": gap.get("missing_capability"),
                           "started_at": now, "updated_at": now}
        self._record("gap_proposed", {
            "task_id": task_id,
            "missing_capability": gap.get("missing_capability")})
        text = proposal_notice(gap)
        self._log("proposed", "Waiting for your go-ahead to build: "
                  + _capability(gap) + ".")
        await self._interrupt(session_id, task_id, text,
                              message_id="selfmod-proposal-" + task_id)
        return None

    async def decide(self, session_id, task_id, approve, *, announce=True):
        """The user's go/no-go. A yes starts the loop in the background.

        ``announce=False`` when the answer came through chat: the main chat's own
        reply already tells the user, so the card updates without a notice.
        """
        async with self._lock:
            pending = self._proposal
            if (pending is None or pending["session_id"] != session_id
                    or pending["task_id"] != task_id):
                raise ValueError("No capability build is waiting for approval "
                                 "on that task.")
            self._proposal = None
            gap = pending["gap"]
            if not approve:
                self.status.update(state="declined",
                                   updated_at=datetime.now(UTC).isoformat())
        self._record("gap_decision", {"task_id": task_id,
                                            "approved": bool(approve)})
        self._log("decision", "You approved the build." if approve
                  else "You declined the build.")
        if not approve:
            await self._notice(declined_notice(gap), notify=announce)
            return {"task_id": task_id, "build": "declined"}
        self.status.update(state="running")
        self._stop_requested = False
        await self._notice(GAP_NOTICE, message_id="selfmod-gap-" + task_id,
                           notify=announce)
        self._runner = asyncio.create_task(self._run(session_id, task_id, gap))
        return {"task_id": task_id, "build": "started"}

    async def _run(self, session_id, task_id, gap):
        try:
            async with self._lock:
                request = await asyncio.to_thread(
                    lambda: self._coordinator.store.get(
                        session_id, task_id)["original_message"])
                self._loop = self._loop_factory(session_id, task_id, request, gap)
            self._record("gap_accepted", {
                "task_id": task_id,
                "missing_capability": gap.get("missing_capability")})
            self._job = asyncio.create_task(self._loop.run(gap))
            outcome = await self._job
        except asyncio.CancelledError:
            if not self._stop_requested:
                raise
            outcome = Outcome(False, "stopped by the user", stopped=True)
            await self._notice(STOPPED_NOTICE)
        finally:
            self._loop = self._job = None
        if outcome.finished and self.status.get("continuation_task_id"):
            await self._complete_original(session_id, task_id,
                                          self.status["continuation_task_id"])
        self.status.update(
            state=("stopped" if outcome.stopped
                   else "finished" if outcome.finished else "failed"),
            updated_at=datetime.now(UTC).isoformat())
        self._record("loop_finished", {"task_id": task_id,
                                             "finished": outcome.finished,
                                             "stopped": outcome.stopped,
                                             "detail": outcome.detail})
        return outcome

    def _log(self, kind, text):
        self.activity.append({"at": datetime.now(UTC).isoformat(), "kind": kind,
                              "text": text})
        del self.activity[:-200]

    async def _complete_original(self, session_id, task_id, continuation_id):
        """The original task takes the resumed request's answer and finishes."""
        store = self._coordinator.store

        def complete():
            resumed = store.get(session_id, continuation_id)
            if resumed["state"] == "completed":
                store.update(session_id, task_id, state="completed",
                             progress=resumed["progress"], result=resumed["result"])

        try:
            await asyncio.to_thread(complete)
        except Exception as error:
            self._record("complete_original_failed", {
                "task_id": task_id, "reason": str(error)[:2048]})

    async def _interrupt(self, session_id, task_id, text, *, message_id):
        """A's execution stops; its task stays active so its card can cancel."""
        try:
            await self._coordinator.interrupt_for_selfmod(session_id, task_id, text)
        except Exception as error:
            self._record("interrupt_failed", {"task_id": task_id,
                                                    "reason": str(error)[:2048]})
        await self._notice(text, message_id=message_id)

    async def _notice(self, text, *, message_id=None, notify=True):
        """One milestone on the original task: a notification and its progress."""
        session_id, task_id = self.status.get("session_id"), self.status.get("task_id")
        if not task_id:
            return
        self._notices += 1
        self.status.update(milestone=text, updated_at=datetime.now(UTC).isoformat())
        try:
            if notify:
                await asyncio.to_thread(
                    self._coordinator.store.notify, session_id, task_id,
                    message_id or f"selfmod-{task_id}-{self._notices}", text)
            await asyncio.to_thread(self._coordinator.store.update, session_id,
                                    task_id, progress=text)
        except Exception as error:
            self._record("notice_failed", {"task_id": task_id,
                                                 "reason": str(error)[:2048]})

    async def _on_event(self, kind, data):
        attempt = data.get("attempt")
        line = describe(kind, data)
        if line is not None:
            self._log(kind, line)
        # Step-by-step detail lives in the implementation workspace; the chat
        # hears only the moments that change what the user should know.
        if kind == "tests_frozen":
            self.status["feature"] = feature_name(data.get("tool_name"))
        elif kind == "attempt_started":
            self.status["attempt"] = attempt
            feature = self.status.get("feature") or "requested"
            if attempt == 1:
                await self._notice(
                    f"I'm starting the build for the new {feature} feature.")
            else:
                # A failure notice already said it is trying again.
                await self._notice(f"Attempt {attempt} of the {feature} feature "
                                   "is building.", notify=False)
        elif kind == "attempt_failed":
            reason = (str(data.get("reason") or "no reason recorded")
                      .splitlines()[0][:200].rstrip("."))
            repeats = data.get("repeats") or 1
            if repeats > 1:
                # Same failure again: keep the card current without a new notice.
                await self._notice(f"Attempt {attempt} didn't pass: {reason} "
                                   f"({repeats} in a row). Trying again.",
                                   notify=False)
            else:
                await self._notice(f"Attempt {attempt} didn't pass: {reason}. "
                                   "Trying again.")
        elif kind == "plan_approved":
            await self._notice(f"Attempt {attempt}: plan approved, implementing.",
                               notify=False)
        elif kind == "checks":
            state = "passed" if data.get("passed") else "failed, fixing"
            await self._notice(f"Attempt {attempt}: checks {state}.", notify=False)
        elif kind == "code_review" and not data.get("approved"):
            await self._notice(f"Attempt {attempt}: review asked for changes.",
                               notify=False)
        elif kind == "resuming":
            self.status["continuation_task_id"] = data.get("task_id")
            await self._notice("The new capability passed. Resuming your request.")
        elif kind == "loop_stopped":
            await self._notice(STOPPED_NOTICE)

    def cancel_requested(self, session_id, task_id):
        """Coordinator hook: canceling the original or resumed task stops the loop."""
        status = self.status
        pending = self._proposal
        if (pending and pending["session_id"] == session_id
                and pending["task_id"] == task_id):
            # Canceling the task also answers its pending build question.
            self._proposal = None
            self.status.update(state="declined",
                               updated_at=datetime.now(UTC).isoformat())
            return
        if (status.get("state") == "running"
                and session_id == status.get("session_id")
                and task_id in {status.get("task_id"),
                                status.get("continuation_task_id")}):
            self.stop()

    def stop(self):
        """Deterministic stop: cancel the running loop now, including its step."""
        running = self._runner if self._job is None else self._job
        if running is None or running.done():
            return False
        self._stop_requested = True
        running.cancel()
        return True

    async def close(self):
        self.stop()
        if self._connections is not None:
            with contextlib.suppress(Exception):
                await self._connections.close()
        for task in list(self._retiring):
            task.cancel()


async def install(config, coordinator, *, repository=None, root=None,
                  model_slot=None, model_base_url=None, model_api_key=None):
    """Build and serve A, then hand the coordinator its gap hook."""
    repository = Path(repository or Path(__file__).resolve().parents[3])
    root = Path(root or (config.data_dir / "selfmod")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    docker = await pinned_docker(root / "docker-cli")
    base_image_id = await resolve_image(docker, config.sandbox_container_image)
    # A connected Google account, when the user has authorized one.
    connections = None
    if GoogleAccount.available(google_store()):
        connections = ConnectionService(GoogleAccount(google_store()))
        await connections.start()
    role_slot = (model_slot.slot_for("modifier")
                 if config.generator_parallel_slots == 3
                 and hasattr(model_slot, "slot_for") else None)
    service = SelfModificationService(
        config, coordinator, repository=repository, root=root,
        images=BundleImages(docker), base_image_id=base_image_id,
        role_endpoint=config.generator_base_url, role_model=config.generator_model,
        role_slot=role_slot, model_slot=model_slot,
        sandbox_root=getattr(config, "sandbox_root", None),
        model_base_url=model_base_url, model_api_key=model_api_key, docker=docker,
        connections=connections,
    )
    await service.prepare()
    return service
