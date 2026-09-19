"""Application wiring: A's deployment, the capability-gap hook and one loop.

The app owns these objects; a worker never reaches them. Preparing builds A's
bundle image from the repository tree, removes every other bundle image and the
directories an earlier start left, and serves delegated work from A. A
structured gap report from A then stops A's execution, keeps its task
cancelable, and asks the user whether to build the capability. Only an explicit
yes, from chat or the task card, runs exactly one loop, announced at each
milestone. Canceling the task stops the loop immediately. A second report while
one waits or runs is declined, not queued.

When B finishes the request, its files are committed on the checked-out branch,
after a tag marks the state before it, and B serves as A; the replaced A is torn
down once its work settles. A B that fails,
is canceled or is interrupted by a restart leaves nothing behind.
"""

import asyncio
import contextlib
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..agents_store import AgentStore
from ..channels import ChannelHub
from ..connections import GUIDE as connection_guide_text
from ..connections import ConnectionService, GoogleAccount, google_store
from ..connectors.manager import GUIDE as connector_guide_text
from ..connectors.manager import ConnectorManager
from ..engine.sandbox.manager import SandboxDeployment, SandboxManager
from ..scheduling import Scheduler, ScheduleStore
from .agents import AgentDeveloper, DockerChecks
from .deployment import (
    BundleImages,
    Deployment,
    Deployments,
    SubagentBundle,
    materialize_skills,
)
from .docker import pinned_docker, resolve_image
from .loop import TERMINAL, DeploymentSwitch, Outcome, SelfModificationLoop
from .paused import PausedBuild, clear, load, save
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


def connect_notice(gap, name):
    """Issue #28: a connector for the gap comes before the offer to build."""
    return (f"I can't do that yet, but {name} can: {_capability(gap)}. "
            "Want me to connect it? Your request picks back up as soon as "
            "you allow it on the provider's page.")


def connect_declined_notice(gap, name):
    return (f"Okay, I won't connect {name}. This request stays blocked "
            f"without this capability: {_capability(gap)}.")


def connecting_notice(name):
    return (f"Opening {name}'s sign-in in your browser; finish it there. "
            "Your request resumes the moment you allow it.")


def connector_brief(name, tools):
    """The resumed worker's brief: the capability exists now, so use it."""
    return (
        f"The user just connected their {name} account. Your toolset now "
        f"includes: {', '.join(tools)}.\n"
        "1. Use them to finish the request. The earlier work shows this was "
        "not possible before; that has changed, so don't report the same "
        "gap again.\n"
        f"2. If a {name} tool fails or reports the connection is "
        "unavailable, report blocked with its error.")


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
    if kind == "loop_gave_up":
        return (f"Gave up: {_line(data.get('reason'))} failed "
                f"{data.get('repeats')} attempts in a row.")
    if kind == "resuming":
        return "The new capability passed. Resuming the request."
    if kind == "attempt_finished":
        return "The resumed request finished."
    if kind == "promoted":
        return (f"Committed on {data.get('branch')} as {data.get('commit')}; it "
                f"now serves new work. Roll back with: git reset --hard "
                f"{data.get('tag')}")
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


class SelfModificationService:
    def __init__(self, config, coordinator, *, repository, root, images,
                 base_image_id, role_endpoint, role_model, manager_factory=None,
                 role_slot=None, sandbox_root=None, model_slot=None,
                 model_base_url=None, model_api_key=None, loop_factory=None,
                 completer=model_completer, docker=None, run_checks=None,
                 development_manager_factory=None, connections=None,
                 connectors=None, promote=promote, retire_poll=1.0,
                 continuation_poll=1.0):
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
        #: Connector lookup and consent (issue #28), or None when unavailable.
        self._connectors = (connectors if connectors is not None else
                            getattr(connections, "connectors", None))
        self._continuation_poll = continuation_poll
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
        #: A mid-build step the implementation paused on, awaiting the user.
        self._pending_step = None
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
                                 connected=(lambda: tuple(
                                     self._connectors.connected())
                                     if self._connectors is not None else ()),
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
        root = self._directory("dev-" + name)
        manager = SandboxManager(
            self._config, model_slot=self._model_slot, development=True,
            deployment=SandboxDeployment(self._base_image_id, empty, root))
        #: Removed with the manager: a build leaves no workspace behind.
        manager.selfmod_paths = (empty, root)
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
        self._coordinator.connect_proposal = self.connect_proposal
        self._coordinator.step_proposal = self.step_proposal
        self._record("a_serving", {"image_id": verified.image_id,
                                   "removed_images": removed})
        return verified

    def _guide(self):
        parts = []
        if self._connections is not None:
            if getattr(self._connections, "google", None) is not None:
                parts.append(connection_guide_text)
            if self._connectors is not None and self._connectors.connected():
                parts.append(connector_guide_text)
        return "\n\n".join(parts) or None

    async def _promote(self, candidate, tests):
        """Commit B's files where the work lives, then let B serve as A."""
        feature = self.status.get("feature") or tests.interface.get("tool_name")
        try:
            saved = await asyncio.to_thread(self._promote_files, self._repository,
                                            self.baseline, candidate, feature)
        except (PromotionError, OSError, ValueError) as error:
            await self._on_event("promotion_failed", {"reason": str(error)})
        else:
            self.status["commit"] = saved.commit
            self.status["rollback_tag"] = saved.tag
            await self._on_event("promoted", {"branch": saved.branch,
                                              "commit": saved.commit[:12],
                                              "tag": saved.tag,
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

    def _build_loop(self, session_id, task_id, request, gap, resume=None):
        admitted = {"admission": self._admission, "lane": self._lane}
        author = self._completer(self._role_endpoint, self._role_model,
                                 slot=self._role_slot, **admitted)
        reviewer = self._completer(self._role_endpoint, self._role_model,
                                   slot=self._role_slot, **admitted)
        def step_handler(step, tree, plan):
            # The build pauses here until the user acts; this loop owns one
            # session and task, so capture them rather than read self.status.
            return self._step(step, session_id=session_id, task_id=task_id)

        async def pause_handler(step, tree, plan, attempt, tests, feedback):
            # Persist before the build blocks on the step, so a restarted
            # process can resume it; the record is dropped when the loop ends.
            await asyncio.to_thread(
                save, self._root, PausedBuild(
                    session_id=session_id, task_id=task_id, request=request,
                    gap=gap, attempt=attempt, feedback=feedback, plan=plan,
                    step=step, tree=tree, tests=tests,
                    connections=self._guide(),
                    baseline_sha256=self.baseline.sha256))
            self._log("step_paused",
                      f"Build paused on a {step.get('kind')} step.")

        develop = AgentDeveloper(
            request=request, gap=gap,
            baseline=self.baseline, policy=self.policy, protected=PROTECTED,
            manager_factory=self._development_manager_factory,
            run_checks=self._run_checks or DockerChecks(
                self._docker, self._base_image_id),
            on_event=self._on_event, connections=self._guide(),
            on_step=step_handler, on_pause=pause_handler,
            resume=(resume.plan, resume.tree, resume.step)
            if resume is not None else None,
        )
        switch = DeploymentSwitch(
            deployments=self.deployments, images=self._images,
            coordinator=self._coordinator, session_id=session_id,
            parent_task_id=task_id, request=request,
            base_image_id=self._base_image_id, launch=LAUNCH,
            stage=lambda verified: self._deployment(verified, "B"),
            promote=self._promote, discard=self._discard,
            on_event=self._on_event,
            # The serving-surface check must see what really is connected, and
            # what the build ships: a tool gated behind a connector is judged
            # on its registration path once that connector signs in.
            connected=(lambda: tuple(self._connectors.connected())
                       if self._connectors is not None else ()),
            shipped=(lambda: self._connectors.ids()
                     if self._connectors is not None else ()),
        )
        return SelfModificationLoop(
            author_tests=authoring(
                request, baseline=self.baseline, policy=self.policy,
                author=author, reviewer=reviewer, connections=self._guide()),
            develop=develop, switch=switch, on_event=self._on_event)

    def proposal(self, session_id, task_id):
        """The capability build waiting for the user's go/no-go on this task."""
        pending = self._proposal
        if (pending and not pending.get("connector")
                and pending["session_id"] == session_id and pending[
                    "task_id"] == task_id):
            return {"missing_capability": pending["gap"].get("missing_capability"),
                    "modification_request": pending["gap"].get(
                        "modification_request")}
        return None

    def connect_proposal(self, session_id, task_id):
        """The connector offered for this task's gap, or None."""
        pending = self._proposal
        if (pending and pending.get("connector")
                and pending["session_id"] == session_id
                and pending["task_id"] == task_id):
            return {"connector": pending["connector"],
                    "name": pending["connector_name"],
                    "missing_capability": pending["gap"].get("missing_capability")}
        return None

    def step_proposal(self, session_id, task_id):
        """A paused build's question awaiting the user's answer on this task.

        Only an ``ask`` step needs a chat answer; a ``connect`` step is settled
        by the sign-in window the user is already shown.
        """
        pending = self._pending_step
        if (pending and pending["session_id"] == session_id
                and pending["task_id"] == task_id
                and pending["step"].get("kind") == "ask"):
            return dict(pending["step"])
        return None

    async def handle_gap(self, session_id, gap):
        """A's gap pauses its task and asks the user; nothing runs without a yes.

        A connector that covers the gap is offered first (issue #28); only
        when none exists does the task ask whether to build the capability.
        """
        task_id = gap.get("task_id")
        async with self._lock:
            building = self._runner is not None and not self._runner.done()
            if building or self._loop is not None or self._proposal is not None:
                self._record("gap_declined", {
                    "task_id": task_id,
                    "reason": "another capability build is waiting or running"})
                return None
            connector = (self._connectors.find(gap)
                         if self._connectors is not None else None)
            self._proposal = {"session_id": session_id, "task_id": task_id,
                              "gap": gap,
                              "connector": connector.id if connector else None,
                              "connector_name": connector.name if connector
                              else None}
            self.activity = []
            now = datetime.now(UTC).isoformat()
            self.status = {"state": ("awaiting_connect" if connector
                                      else "awaiting_approval"),
                           "session_id": session_id,
                           "task_id": task_id, "continuation_task_id": None,
                           "attempt": 0,
                           "connector": connector.id if connector else None,
                           "missing_capability": gap.get("missing_capability"),
                           "started_at": now, "updated_at": now}
        self._record("connect_proposed" if connector else "gap_proposed", {
            "task_id": task_id,
            "connector": connector.id if connector else None,
            "missing_capability": gap.get("missing_capability")})
        text = (connect_notice(gap, connector.name) if connector
                else proposal_notice(gap))
        self._log("proposed", ("Waiting for your yes to connect "
                               + connector.name + "." if connector else
                               "Waiting for your go-ahead to build: "
                               + _capability(gap) + "."))
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
            connector_id = pending["connector"]
            if not approve:
                self.status.update(state="declined",
                                   updated_at=datetime.now(UTC).isoformat())
        if connector_id:
            name = pending["connector_name"]
            self._record("connect_decision", {"task_id": task_id,
                                              "connector": connector_id,
                                              "approved": bool(approve)})
            self._log("decision", f"You approved connecting {name}." if approve
                      else f"You chose not to connect {name}.")
            if not approve:
                await self._notice(connect_declined_notice(gap, name),
                                   notify=announce)
                return {"task_id": task_id, "build": "declined"}
            self.status.update(state="running")
            self._stop_requested = False
            # The task is created before any await: a cancel arriving in this
            # window must find a live runner, not open a sign-in nobody
            # asked to keep. The notice is the coroutine's first act.
            self._runner = asyncio.create_task(
                self._connect_and_resume(session_id, task_id, connector_id,
                                         name, announce=announce))
            return {"task_id": task_id, "build": "started"}
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

    async def _run(self, session_id, task_id, gap, resume=None):
        if resume is None:
            clear(self._root)
        try:
            async with self._lock:
                if resume is None:
                    request = await asyncio.to_thread(
                        lambda: self._coordinator.store.get(
                            session_id, task_id)["original_message"])
                else:
                    request = resume.request
                self._loop = self._loop_factory(session_id, task_id, request, gap,
                                                resume=resume)
            if resume is not None:
                self.status.update(
                    state="running", session_id=session_id, task_id=task_id,
                    attempt=resume.attempt,
                    missing_capability=gap.get("missing_capability"),
                    updated_at=datetime.now(UTC).isoformat())
                self._log("resumed",
                          f"Resuming the paused build (attempt {resume.attempt}).")
            else:
                self._record("gap_accepted", {
                    "task_id": task_id,
                    "missing_capability": gap.get("missing_capability")})
            seed = None if resume is None else {
                "tests": resume.tests, "attempt": resume.attempt,
                "feedback": resume.feedback}
            self._job = asyncio.create_task(self._loop.run(gap, seed=seed))
            outcome = await self._job
        except asyncio.CancelledError:
            if not self._stop_requested:
                raise
            outcome = Outcome(False, "stopped by the user", stopped=True)
            await self._notice(STOPPED_NOTICE)
        finally:
            self._loop = self._job = None
            clear(self._root)
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

    async def _connect_and_resume(self, session_id, task_id, connector_id,
                                  name, *, announce=True):
        """Store the granted connector, then resume the same request on it."""
        try:
            await self._notice(connecting_notice(name),
                               message_id="selfmod-connect-" + task_id,
                               notify=announce)
            entry = await self._connectors.connect(connector_id)
        except asyncio.CancelledError:
            if not self._stop_requested:
                raise
            self.status.update(state="stopped",
                               updated_at=datetime.now(UTC).isoformat())
            await self._notice(STOPPED_NOTICE)
            return
        except (RuntimeError, OSError, ValueError, httpx.HTTPError) as error:
            # The connect alone failed: the request stays blocked and askable.
            self._record("connect_failed", {"task_id": task_id,
                                            "connector": connector_id,
                                            "reason": str(error)[:2048]})
            self.status.update(state="failed",
                               updated_at=datetime.now(UTC).isoformat())
            self._log("connect_failed",
                      f"The connection to {name} did not complete.")
            await self._notice(f"The connection to {name} didn't complete: "
                               f"{str(error).rstrip('.')}. Ask me again "
                               "whenever you're ready.")
            return
        self._record("connect_succeeded", {"task_id": task_id,
                                           "connector": connector_id})
        self._log("connected", f"Connected to {name}.")
        await self._notice(f"Connected to {name}. Resuming your request.")
        brief = connector_brief(name, entry.get("tools") or [])
        try:
            request = await asyncio.to_thread(
                lambda: self._coordinator.store.get(
                    session_id, task_id)["original_message"])
            task = await self._coordinator.submit(
                session_id, f"connect-{task_id}", brief, request,
                parent_task_id=task_id, continuation=True)
            await self._coordinator.release_held(session_id, task["task_id"])
            self.status["continuation_task_id"] = task["task_id"]
            while True:
                final = await asyncio.to_thread(self._coordinator.store.get,
                                                session_id, task["task_id"])
                if final["state"] in TERMINAL:
                    break
                await asyncio.sleep(self._continuation_poll)
        except asyncio.CancelledError:
            if not self._stop_requested:
                raise
            self.status.update(state="stopped",
                               updated_at=datetime.now(UTC).isoformat())
            await self._notice(STOPPED_NOTICE)
            return
        except (KeyError, ValueError, OSError) as error:
            self._record("connect_resume_failed", {"task_id": task_id,
                                                   "reason": str(error)[:2048]})
            self.status.update(state="failed",
                               updated_at=datetime.now(UTC).isoformat())
            return
        self._record("connect_resumed", {"task_id": task_id,
                                         "connector": connector_id,
                                         "continuation_task_id":
                                             task["task_id"],
                                         "state": final["state"]})
        if final["state"] == "completed":
            await self._complete_original(session_id, task_id, task["task_id"])
            self.status.update(state="finished",
                               updated_at=datetime.now(UTC).isoformat())
        elif final["state"] == "canceled":
            await self._notice(f"Your request was stopped; {name} stays "
                               "connected.")
            self.status.update(state="stopped",
                               updated_at=datetime.now(UTC).isoformat())
        else:
            # The user heard "Resuming"; a continuation that dies quietly
            # would leave the original task interrupted with no last word.
            reason = " ".join(str(final.get("error") or "no reason given")
                              .split())[:200].rstrip(".")
            await self._notice(f"{name} is connected, but the request could "
                               f"not be finished: {reason}. Ask me again "
                               "whenever you're ready.")
            await self._complete_original(session_id, task_id, task["task_id"])
            self.status.update(state="failed",
                               updated_at=datetime.now(UTC).isoformat())

    # -- a step only a human can take -------------------------------------

    async def _step(self, step, *, session_id, task_id):
        """Handle the implementation's pause: connect a service or ask a
        question, then hand the outcome back so the build can resume."""
        kind = step.get("kind")
        if kind == "connect":
            return await self._step_connect(step, session_id, task_id)
        if kind == "ask":
            return await self._step_ask(step, session_id, task_id)
        return ("That step is not something I can do. Proceed with what you "
                "can, or say plainly in your summary what is missing.")

    async def _step_connect(self, step, session_id, task_id):
        connector_id = str(step.get("connector") or "").strip()
        connectors = self._connectors
        if connectors is None or connector_id not in connectors:
            return (f"Connecting {connector_id or 'that service'} is not "
                    "available. Proceed without it if you can, or say plainly "
                    "in your summary what is missing.")
        try:
            name = connectors.get(connector_id).name
        except Exception:
            name = connector_id
        self._log("step_connect_requested", f"Connecting {name}.")
        await self._notice(
            f"To build this I need to connect {name}. I'm opening a sign-in "
            "window - please complete it so I can continue.")
        try:
            entry = await connectors.connect(connector_id)
        except asyncio.CancelledError:
            raise
        except (RuntimeError, OSError, ValueError, httpx.HTTPError) as error:
            self._record("step_connect_failed", {
                "task_id": task_id, "connector": connector_id,
                "reason": str(error)[:2048]})
            self._log("step_connect_failed", f"Connecting {name} failed.")
            return (f"Connecting {name} did not complete: "
                    f"{str(error).rstrip('.')}. Proceed without it if you can, "
                    "or say plainly in your summary what is missing.")
        self._record("step_connect_succeeded", {
            "task_id": task_id, "connector": connector_id})
        self._log("step_connect_succeeded", f"Connected {name}.")
        await self._notice(f"Connected to {name}. Continuing the build.")
        tools = ", ".join(str(t) for t in (entry.get("tools") or []))
        return (f"{name} is now connected"
                + (f" ({tools})" if tools else "")
                + ". Continue with the build.")

    async def _step_ask(self, step, session_id, task_id):
        question = " ".join(str(step.get("question") or "").split())
        if not question:
            return ("You asked a question without stating it. State the "
                    "question, or proceed with what you can.")
        future = asyncio.get_running_loop().create_future()
        self._pending_step = {"session_id": session_id, "task_id": task_id,
                              "step": dict(step), "future": future}
        self.status["pending_step"] = dict(step)
        self._log("step_ask_requested", question)
        self._record("step_ask_requested", {"task_id": task_id,
                                            "question": question[:2048]})
        try:
            await self._notice(f"To finish the build I need your input: "
                               f"{question}",
                               message_id="selfmod-step-" + task_id)
            answer = await future
        finally:
            # A stop may land while we are still announcing the question.
            self._forget_step(future)
        self._log("step_ask_answered",
                  " ".join(str(answer).split())[:200])
        self._record("step_ask_answered", {"task_id": task_id,
                                           "answer": " ".join(
                                               str(answer).split())[:2048]})
        return "You answered: " + " ".join(str(answer).split())

    def _forget_step(self, future):
        if self._pending_step is not None and \
                self._pending_step["future"] is future:
            self._pending_step = None
            if not future.done():
                future.cancel()
        self.status.pop("pending_step", None)

    async def answer_step(self, session_id, task_id, answer):
        """Resolve a paused build's question with the user's answer."""
        pending = self._pending_step
        if (pending is None or pending["session_id"] != session_id
                or pending["task_id"] != task_id
                or pending["future"].done()):
            raise ValueError("No build is waiting for an answer on that task.")
        pending["future"].set_result(" ".join(str(answer).split()))
        return {"task_id": task_id, "answered": True}

    def _log(self, kind, text):
        self.activity.append({"at": datetime.now(UTC).isoformat(), "kind": kind,
                              "text": text})
        del self.activity[:-200]

    async def _complete_original(self, session_id, task_id, continuation_id):
        """The original task takes the resumed request's outcome: its answer,
        or its failure — never a silence that leaves the task interrupted."""
        store = self._coordinator.store

        def complete():
            resumed = store.get(session_id, continuation_id)
            if resumed["state"] == "completed":
                store.update(session_id, task_id, state="completed",
                             progress=resumed["progress"], result=resumed["result"])
            elif resumed["state"] in {"blocked", "interrupted"}:
                # The resumed request ended short of an answer; the original
                # task says so instead of waiting on it forever.
                store.update(session_id, task_id, state="blocked",
                             error=str(resumed.get("error")
                                       or "The resumed request could not "
                                          "finish."))

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
        elif kind == "loop_gave_up":
            reason = (str(data.get("reason") or "no reason recorded")
                      .splitlines()[0][:200].rstrip("."))
            await self._notice(f"I stopped retrying this build: {reason} "
                               f"failed {data.get('repeats')} attempts in a "
                               "row. Ask me again once that changes.")
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


def notice_delivery(notify, channels=None):
    """What a due job means here: a notice into the task that booked it,
    then the off-device fan-out the seam plan assigns to the host.

    ``notify`` is ``TaskStore.notify``, called as ``notify(session_id,
    task_id, message_id, text, kind=...)``. ``channels`` is a
    ``channels.ChannelHub``, or None while sending is not wired. The
    payload shape is checked here, not at the relay: the relay carries
    payloads untouched, so a bad job fails with this as its recorded
    reason instead of half-delivering. The Notice always posts; a failed
    channel send fails the job with its reason — no retry, by contract.
    """

    async def deliver(job: dict) -> None:
        payload = job["payload"]
        missing = [name for name in ("session_id", "task_id", "text")
                   if not isinstance(payload.get(name), str)
                   or not payload[name].strip()]
        if missing:
            raise ValueError(f"a notice needs {', '.join(missing)}")
        channel = payload.get("channel", "")
        if not isinstance(channel, str):
            raise ValueError("a notice channel must be a string")
        await asyncio.to_thread(
            notify, payload["session_id"], payload["task_id"],
            f"scheduled-{job['job_id']}", payload["text"].strip(),
            kind="reminder")
        if channels is None:
            return
        try:
            targets = channels.targets(channel)
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        failures = []
        for name in targets:
            try:
                await channels.send(name, payload["text"].strip())
            except Exception as error:  # noqa: BLE001 - recorded, never retried
                failures.append(f"{name}: {type(error).__name__}: {error}")
        if failures:
            raise RuntimeError("channel send failed: "
                               + "; ".join(failures)[:400])

    return deliver


async def install(config, coordinator, *, repository=None, root=None,
                  model_slot=None, model_base_url=None, model_api_key=None):
    """Build and serve A, then hand the coordinator its gap hook."""
    repository = Path(repository or Path(__file__).resolve().parents[3])
    root = Path(root or (config.data_dir / "selfmod")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    docker = await pinned_docker(root / "docker-cli")
    base_image_id = await resolve_image(docker, config.sandbox_container_image)
    # Accounts and connectors, served to workers over loopback. The service
    # starts even with nothing connected: a connect mid-session must be able
    # to hand its tools a relay without restarting anything.
    google = (GoogleAccount(google_store())
              if GoogleAccount.available(google_store()) else None)
    connectors = ConnectorManager()
    # The durable-state seam for worker-built capabilities, and the host's
    # channel hub for delivery-time sends (.agent/seam-architecture-plan.md).
    agent_store = AgentStore(Path(config.data_dir) / "agents")
    channels = ChannelHub()
    # The generic "do this at time T" primitive: due jobs become task
    # notices. It starts with the app because a job may be due while the
    # server that booked it is gone; the schedule file is its memory.
    scheduler = Scheduler(
        ScheduleStore(Path(config.data_dir) / "schedules.json"),
        notice_delivery(coordinator.store.notify, channels))
    await scheduler.start()
    connections = ConnectionService(google, connectors, scheduler,
                                    store=agent_store, channels=channels)
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
    _resume_paused_build(service, root)
    return service


def _resume_paused_build(service, root):
    """Pick up a build that paused on a human step before a restart.

    The record is honored only while the tree it captured still matches the
    deployed baseline; a drifted tree would resume against the wrong source.
    """
    try:
        paused = load(root)
    except (OSError, ValueError) as error:
        _LOG.warning("Could not read the paused-build record: %s", error)
        clear(root)
        return
    if paused is None:
        return
    if paused.baseline_sha256 != service.baseline.sha256:
        _LOG.warning("Paused build's baseline no longer matches; not resuming.")
        service._record("resume_abandoned",
                        {"reason": "baseline changed since the build paused"})
        clear(root)
        return

    async def claim():
        # The claim takes the same lock as handle_gap and decide: a gap
        # approved in this window would otherwise start a second build at
        # once, and the runner must be visible to stop() and close().
        async with service._lock:
            building = (service._runner is not None
                        and not service._runner.done())
            if (building or service._loop is not None
                    or service._proposal is not None):
                service._record("resume_abandoned",
                                {"reason": "a build or proposal is already "
                                           "in flight"})
                clear(root)
                return
            service._runner = asyncio.create_task(
                service._run(paused.session_id, paused.task_id, paused.gap,
                             resume=paused))

    task = asyncio.create_task(claim())
    # Tracked like _retire's teardown: close() must be able to cancel a
    # claim that has not run yet, so a shutdown during startup can start
    # no build after the loop is torn down. The record stays for the next
    # start either way.
    service._retiring.add(task)
    task.add_done_callback(service._retiring.discard)
