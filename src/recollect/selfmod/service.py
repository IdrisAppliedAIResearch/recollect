"""Application wiring: A's deployment, the capability-gap hook and one loop.

The app owns these objects; a worker never reaches them. Preparing builds A's
bundle image from the repository tree and registers it as the serving
deployment, so delegated work runs on A. A structured gap report from A then
cancels that task and runs exactly one self-modification loop, journaled from
acceptance to outcome. A second report while a loop runs is declined, not queued.
"""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import uuid
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
from .loop import DeploymentSwitch, RoundDeveloper, SelfModificationLoop
from .native_runtime import NativeDocker
from .roles import RoleSettings
from .subagent_tree import BUNDLE_PYTHONPATH, LAUNCH, baseline, change_policy
from .tests_first import authoring, model_completer

DOCKER_ENVIRONMENT = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT"}
ENTRYPOINT = "driver.py"


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
        self._journal = Journal.create(self._root / ("service-" + uuid.uuid4().hex))
        routing = Journal.create(self._root / ("routing-" + uuid.uuid4().hex))
        self._journals = [self._journal, routing]
        self.router = DeploymentRouter(routing)
        self.sandboxes = DeploymentSandboxes(self.router)
        self.baseline = self.policy = None
        self._lock = asyncio.Lock()
        self._loop = None

    async def _record(self, kind, data):
        await asyncio.to_thread(self._journal.append, kind, data)

    def _sandbox_manager(self, verified, name):
        skills = materialize_skills(
            verified.bundle, self._root / f"skills-{name}-{self._token}")
        manager = SandboxManager(self._config, model_slot=self._model_slot,
                                 deployment=SandboxDeployment(
                                     verified.image_id, skills,
                                     self._root / f"root-{name}-{self._token}",
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
        await self._record("a_registered", {"bundle_digest": bundle.digest,
                                            "image_id": verified.image_id})
        return verified

    def _build_loop(self, journal, session_id, task_id, request, gap):
        author = self._completer(self._role_endpoint, self._role_model,
                                 slot=self._role_slot)
        reviewer = self._completer(self._role_endpoint, self._role_model,
                                   slot=self._role_slot)
        develop = RoundDeveloper(
            self._root / ("rounds-" + task_id), request=request,
            baseline=self.baseline, policy=self.policy,
            settings=DevelopmentSettings(self._base_image_id,
                                         self._image_environment, ENTRYPOINT),
            role_settings=lambda checks: RoleSettings(
                self._role_endpoint, self._role_model, checks, slot=self._role_slot),
            runtime_factory=self._runtime_factory,
        )
        switch = DeploymentSwitch(
            router=self.router, sandboxes=self.sandboxes, images=self._images,
            coordinator=self._coordinator, session_id=session_id,
            parent_task_id=task_id, request=request,
            base_image_id=self._base_image_id, launch=LAUNCH,
            manager_factory=lambda receipt: self._manager_factory(
                receipt, "b-" + receipt.image_id[7:19]),
        )
        return SelfModificationLoop(
            journal, author_tests=authoring(request, author=author,
                                            reviewer=reviewer),
            develop=develop, switch=switch)

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
        loop = self._loop
        try:
            await self._cancel(session_id, task_id)
            outcome = await loop.run(gap)
        finally:
            self._loop = None
        await self._record("loop_finished", {"task_id": task_id,
                                             "finished": outcome.finished,
                                             "detail": outcome.detail})
        return outcome

    async def _cancel(self, session_id, task_id):
        """A's own task stops; A's tree and deployment are never touched."""
        try:
            await self._coordinator.command(session_id, task_id,
                                            "selfmod-cancel-" + task_id, "cancel")
        except Exception as error:
            await self._record("cancel_failed", {"task_id": task_id,
                                                 "reason": str(error)[:2048]})

    def stop(self):
        """User stop: the running loop finishes its step and starts no attempt."""
        if self._loop is not None:
            self._loop.stop()

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
    shared = root / ("runtimes-" + uuid.uuid4().hex)
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
        role_slot=role_slot, model_slot=model_slot,
        model_base_url=model_base_url, model_api_key=model_api_key,
    )
    await service.prepare()
    return service
