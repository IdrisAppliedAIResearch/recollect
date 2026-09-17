"""Application wiring: A's deployment, the capability-gap hook and one loop.

The app owns these objects; a worker never reaches them. Preparing builds A's
bundle image from the repository tree and registers it as the serving
deployment, so delegated work runs on A. A structured gap report from A then
stops A's execution, keeps its task cancelable, and runs exactly one loop,
journaled and announced at each milestone. Canceling the task stops the loop
immediately. A second report while a loop runs is declined, not queued.
"""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..engine.sandbox.manager import SandboxDeployment, SandboxManager
from .contracts import File, Snapshot
from .deployment import (
    BundleImages,
    DeploymentRouter,
    DeploymentSandboxes,
    SubagentBundle,
    TaskDeployments,
    materialize_skills,
)
from .docker_runtime import DockerFixtureRuntime
from .files import materialize
from .integration import DevelopmentSettings
from .journal import IntegrityError, Journal
from .loop import DeploymentSwitch, Outcome, RoundDeveloper, SelfModificationLoop
from .native_runtime import NativeDocker
from .roles import LocalRoleModel, RoleSettings
from .subagent_tree import BUNDLE_PYTHONPATH, LAUNCH, baseline, change_policy
from .tests_first import authoring, model_completer

DOCKER_ENVIRONMENT = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT"}
ENTRYPOINT = "driver.py"
GAP_NOTICE = ("I can't do that yet, so I'm building the capability and will pick "
              "your request back up when it's ready.")
STOPPED_NOTICE = "Stopped building the capability."


def docker_environment():
    return {k: v for k, v in os.environ.items()
            if k.upper() in DOCKER_ENVIRONMENT}


async def _capture(argv, environment):
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=environment,
        **({"creationflags": subprocess.CREATE_NO_WINDOW}
           if sys.platform == "win32" else {}),
    )
    out, err = await process.communicate()
    if process.returncode:
        raise IntegrityError("Docker CLI probe failed: "
                             + err.decode(errors="replace")[:512])
    return out


async def pinned_docker(root, *, executable=None, environment=None):
    """Freeze the absolute CLI, a private config directory and the endpoint."""
    environment = docker_environment() if environment is None else dict(environment)
    found = executable or shutil.which("docker")
    if not found:
        raise IntegrityError("No Docker CLI is available for bundle images")
    executable = Path(found).resolve()
    materialize(root, Snapshot((File("config.json", b'{"auths":{}}\n'),)))
    endpoint = (await _capture(
        [str(executable), "context", "inspect", "--format",
         "{{.Endpoints.docker.Host}}"], environment)).decode().strip()
    docker = NativeDocker((str(executable), "--config", str(root), "--host",
                           endpoint), tuple(environment.items()))
    return docker, endpoint, executable


async def resolve_image(docker, tag):
    """The immutable ID and environment of an already-present local image."""
    code, out, err = await docker.run("image", "inspect", tag)
    if code:
        raise IntegrityError("Base image is not present locally: "
                             + err.decode(errors="replace")[:512])
    value = json.loads(out)
    if len(value) != 1:
        raise IntegrityError("Ambiguous base image identity")
    return value[0]["Id"], tuple(value[0].get("Config", {}).get("Env") or ())


class SelfModificationService:
    def __init__(self, config, coordinator, *, repository, root, images,
                 base_image_id, image_environment, role_endpoint, role_model,
                 runtime_factory, manager_factory=None, role_slot=None,
                 sandbox_root=None,
                 model_slot=None, model_base_url=None, model_api_key=None,
                 loop_factory=None, completer=model_completer):
        self._config, self._coordinator = config, coordinator
        self._repository, self._root = Path(repository), Path(root)
        self._images = images
        self._base_image_id = base_image_id
        self._image_environment = tuple(image_environment)
        self._role_endpoint, self._role_model = role_endpoint, role_model
        self._role_slot, self._model_slot = role_slot, model_slot
        self._runtime_factory = runtime_factory
        self._manager_factory = manager_factory or self._sandbox_manager
        self._model_base_url, self._model_api_key = model_base_url, model_api_key
        self._loop_factory = loop_factory or self._build_loop
        self._completer = completer
        self._root.mkdir(parents=True, exist_ok=True)
        # Each install owns its directories, so a later boot never collides with
        # the materialized skills or sandbox root an earlier one left behind.
        self._token = uuid.uuid4().hex
        # Sandbox roots must sit outside any git repository: opencode scopes
        # the project to the enclosing repo root, so a root under the app's
        # data directory would hand the worker this whole codebase.
        self._sandbox_root = Path(
            sandbox_root or getattr(config, "sandbox_root", None) or self._root)
        self._journal = Journal.create(self._root / ("service-" + uuid.uuid4().hex))
        routing = Journal.create(self._root / ("routing-" + uuid.uuid4().hex))
        self._journals = [self._journal, routing]
        self.router = DeploymentRouter(routing)
        self.sandboxes = DeploymentSandboxes(self.router)
        self.baseline = self.policy = None
        self._lock = asyncio.Lock()
        self._loop = self._job = None
        self._stop_requested = False
        self._notices = 0
        #: What the running or last loop is doing, for the status API.
        self.status = {"state": "idle"}
        # On one model slot the loop queues behind conversation turns; with
        # three it takes the modifier lane.
        self._admission = model_slot if hasattr(model_slot, "slot_for") else None
        self._lane = "modifier" if role_slot is not None else "worker"

    async def _record(self, kind, data):
        await asyncio.to_thread(self._journal.append, kind, data)

    def _sandbox_manager(self, verified, name):
        self._sandbox_root.mkdir(parents=True, exist_ok=True)
        skills = materialize_skills(
            verified.bundle,
            self._sandbox_root / f"skills-{name}-{self._token}")
        manager = SandboxManager(self._config, model_slot=self._model_slot,
                                 deployment=SandboxDeployment(
                                     verified.image_id, skills,
                                     self._sandbox_root
                                     / f"root-{name}-{self._token}",
                                     python_path=BUNDLE_PYTHONPATH))
        if self._model_base_url is not None:
            manager.configure_model(self._model_base_url, self._model_api_key)
        return manager

    async def prepare(self):
        """Serve A from its own verified bundle image and accept gap reports."""
        self.baseline = await asyncio.to_thread(baseline, self._repository)
        self.policy = change_policy(self.baseline)
        bundle = SubagentBundle(self.baseline, self._base_image_id, LAUNCH)
        image_id = await self._images.build(bundle)
        verified = await self._images.verify(bundle, image_id)
        await asyncio.to_thread(self.router.register_a, verified)
        manager = await asyncio.to_thread(self._manager_factory, verified, "a")
        await asyncio.to_thread(self.sandboxes.register, "A", manager, verified)
        self._coordinator.deployments = TaskDeployments(self.router, self.sandboxes)
        self._coordinator.on_gap = self.handle_gap
        self._coordinator.on_cancel = self.cancel_requested
        await self._record("a_registered", {"bundle_digest": bundle.digest,
                                            "image_id": verified.image_id})
        return verified

    def _build_loop(self, journal, session_id, task_id, request, gap):
        admitted = {"admission": self._admission, "lane": self._lane}
        author = self._completer(self._role_endpoint, self._role_model,
                                 slot=self._role_slot, **admitted)
        reviewer = self._completer(self._role_endpoint, self._role_model,
                                   slot=self._role_slot, **admitted)
        develop = RoundDeveloper(
            self._root / ("rounds-" + task_id), request=request,
            baseline=self.baseline, policy=self.policy,
            settings=DevelopmentSettings(self._base_image_id,
                                         self._image_environment, ENTRYPOINT),
            role_settings=lambda checks: RoleSettings(
                self._role_endpoint, self._role_model, checks, slot=self._role_slot),
            runtime_factory=self._runtime_factory,
            model_factory=lambda settings: LocalRoleModel(settings, **admitted),
        )
        switch = DeploymentSwitch(
            router=self.router, sandboxes=self.sandboxes, images=self._images,
            coordinator=self._coordinator, session_id=session_id,
            parent_task_id=task_id, request=request,
            base_image_id=self._base_image_id, launch=LAUNCH,
            manager_factory=lambda receipt: self._manager_factory(
                receipt, "b-" + receipt.image_id[7:19]),
            on_event=self._on_event,
        )
        return SelfModificationLoop(
            journal, author_tests=authoring(
                request, baseline=self.baseline, policy=self.policy,
                author=author, reviewer=reviewer),
            develop=develop, switch=switch, on_event=self._on_event)

    async def handle_gap(self, session_id, gap):
        task_id = gap.get("task_id")
        async with self._lock:
            if self._loop is not None:
                await self._record("gap_declined", {
                    "task_id": task_id, "reason": "a loop is already running"})
                return None
            request = await asyncio.to_thread(
                lambda: self._coordinator.store.get(
                    session_id, task_id)["original_message"])
            journal = Journal.create(self._root / ("loop-" + uuid.uuid4().hex))
            self._journals.append(journal)
            self._loop = self._loop_factory(journal, session_id, task_id, request,
                                            gap)
        await self._record("gap_accepted", {
            "task_id": task_id,
            "missing_capability": gap.get("missing_capability")})
        now = datetime.now(UTC).isoformat()
        self.status = {"state": "running", "session_id": session_id,
                       "task_id": task_id, "continuation_task_id": None,
                       "attempt": 0, "milestone": GAP_NOTICE,
                       "started_at": now, "updated_at": now}
        self._stop_requested = False
        loop = self._loop
        try:
            await self._interrupt(session_id, task_id)
            self._job = asyncio.create_task(loop.run(gap))
            try:
                outcome = await self._job
            except asyncio.CancelledError:
                if not self._stop_requested:
                    raise
                outcome = Outcome(False, "stopped by the user", stopped=True)
                await self._notice(STOPPED_NOTICE)
        finally:
            self._loop = self._job = None
        self.status.update(
            state=("stopped" if outcome.stopped
                   else "finished" if outcome.finished else "failed"),
            updated_at=datetime.now(UTC).isoformat())
        await self._record("loop_finished", {"task_id": task_id,
                                             "finished": outcome.finished,
                                             "stopped": outcome.stopped,
                                             "detail": outcome.detail})
        return outcome

    async def _interrupt(self, session_id, task_id):
        """A's execution stops; its task stays active so its card can cancel."""
        try:
            await self._coordinator.interrupt_for_selfmod(session_id, task_id,
                                                          GAP_NOTICE)
        except Exception as error:
            await self._record("interrupt_failed", {"task_id": task_id,
                                                    "reason": str(error)[:2048]})
        await self._notice(GAP_NOTICE, message_id="selfmod-gap-" + task_id)

    async def _notice(self, text, *, message_id=None):
        """One milestone on the original task: a notification and its progress."""
        session_id, task_id = self.status.get("session_id"), self.status.get("task_id")
        if not task_id:
            return
        self._notices += 1
        self.status.update(milestone=text, updated_at=datetime.now(UTC).isoformat())
        try:
            await asyncio.to_thread(
                self._coordinator.store.notify, session_id, task_id,
                message_id or f"selfmod-{task_id}-{self._notices}", text)
            await asyncio.to_thread(self._coordinator.store.update, session_id,
                                    task_id, progress=text)
        except Exception as error:
            await self._record("notice_failed", {"task_id": task_id,
                                                 "reason": str(error)[:2048]})

    async def _on_event(self, kind, data):
        attempt = data.get("attempt")
        if kind == "tests_frozen":
            await self._notice(f"Tests are ready: I'll add {data.get('tool_name')} "
                               f"and check it with {data.get('checks')} tests.")
        elif kind == "attempt_started":
            self.status["attempt"] = attempt
            await self._notice(f"Attempt {attempt}: building and testing.")
        elif kind == "attempt_failed":
            reason = (str(data.get("reason") or "no reason recorded")
                      .splitlines()[0][:200].rstrip("."))
            await self._notice(f"Attempt {attempt} didn't pass: {reason}. "
                               "Trying again.")
        elif kind == "resuming":
            self.status["continuation_task_id"] = data.get("task_id")
            await self._notice("The new capability passed. Resuming your request.")
        elif kind == "loop_stopped":
            await self._notice(STOPPED_NOTICE)

    def cancel_requested(self, session_id, task_id):
        """Coordinator hook: canceling the original or resumed task stops the loop."""
        status = self.status
        if (status.get("state") == "running"
                and session_id == status.get("session_id")
                and task_id in {status.get("task_id"),
                                status.get("continuation_task_id")}):
            self.stop()

    def stop(self):
        """Deterministic stop: cancel the running loop now, including its step."""
        if self._job is None or self._job.done():
            return False
        self._stop_requested = True
        self._job.cancel()
        return True

    async def close(self):
        self.stop()
        for journal in self._journals:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(journal.close)


async def install(config, coordinator, *, repository=None, root=None,
                  model_slot=None, model_base_url=None, model_api_key=None):
    """Build and register A, then hand the coordinator its gap hook."""
    repository = Path(repository or Path(__file__).resolve().parents[3])
    root = Path(root or (config.data_dir / "selfmod")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    docker, endpoint, executable = await pinned_docker(
        root / ("cli-" + uuid.uuid4().hex))
    base_image_id, image_environment = await resolve_image(
        docker, config.sandbox_container_image)
    # Role containers bind-mount this root, so it also stays out of the repo.
    sandbox_root = Path(getattr(config, "sandbox_root", None) or root)
    shared = sandbox_root / ("runtimes-" + uuid.uuid4().hex)
    shared.mkdir(parents=True, exist_ok=True)
    role_slot = (model_slot.slot_for("modifier")
                 if config.generator_parallel_slots == 3
                 and hasattr(model_slot, "slot_for") else None)
    service = SelfModificationService(
        config, coordinator, repository=repository, root=root,
        images=BundleImages(docker), base_image_id=base_image_id,
        image_environment=image_environment,
        role_endpoint=config.generator_base_url, role_model=config.generator_model,
        runtime_factory=lambda: DockerFixtureRuntime(executable, shared, endpoint),
        role_slot=role_slot, model_slot=model_slot, sandbox_root=sandbox_root,
        model_base_url=model_base_url, model_api_key=model_api_key,
    )
    await service.prepare()
    return service
