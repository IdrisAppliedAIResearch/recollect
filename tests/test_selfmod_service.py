"""App wiring: A serves from its own bundle, and one gap runs one loop."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import recollect.selfmod.service as service_module
from recollect.engine.sandbox.manager import SandboxDeployment
from recollect.selfmod.deployment import TaskDeployments, materialize_skills
from recollect.selfmod.loop import Outcome
from recollect.selfmod.service import SelfModificationService
from tests.selfmod_fake_images import BASE
from tests.test_selfmod_loop import Images

REPOSITORY = Path(__file__).resolve().parents[1]
GAP = {"task_id": "task-a", "missing_capability": "calendar write",
       "modification_request": "add a calendar tool"}


class Coordinator:
    def __init__(self):
        self.deployments = self.on_gap = None
        self.commands = []
        self.store = SimpleNamespace(
            get=lambda session_id, task_id: {"original_message": "book the room"})

    async def command(self, session_id, task_id, request_id, operation, *args,
                      **kwargs):
        self.commands.append((task_id, operation))


class Loop:
    def __init__(self, outcome, entered=None, release=None):
        self.outcome, self.entered, self.release = outcome, entered, release
        self.runs, self.stopped = [], False

    async def run(self, gap):
        self.runs.append(gap)
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        return self.outcome

    def stop(self):
        self.stopped = True


@pytest.fixture
def service(tmp_path):
    coordinator = Coordinator()
    loops = []

    def manager(receipt, name):
        skills = materialize_skills(receipt.bundle, tmp_path / ("skills-" + name))
        return SimpleNamespace(deployment=SandboxDeployment(
            receipt.image_id, skills, tmp_path / ("root-" + name)))

    def loop_factory(journal, session_id, task_id, request, gap):
        if not loops:
            loops.append(Loop(Outcome(True, "done")))
        loops[0].request = request
        return loops[0]

    value = SelfModificationService(
        SimpleNamespace(), coordinator, repository=REPOSITORY, root=tmp_path / "root",
        images=Images(), base_image_id=BASE, image_environment=("PATH=/usr/bin",),
        role_endpoint="http://127.0.0.1:8001/v1", role_model="local",
        runtime_factory=lambda: None, manager_factory=manager,
        loop_factory=loop_factory,
    )
    value.coordinator, value.loops = coordinator, loops
    return value


def kinds(service):
    return [r.value["kind"] for r in service._journal.verify()]


async def test_prepare_serves_a_from_its_bundle_and_installs_the_gap_hook(service):
    verified = await service.prepare()
    assert service.router.serving.role == "A"
    assert service.router.serving.image_id == verified.image_id
    assert isinstance(service.coordinator.deployments, TaskDeployments)
    assert service.coordinator.on_gap == service.handle_gap
    assert "a_registered" in kinds(service)
    await service.close()


async def test_a_second_install_on_one_root_does_not_collide(tmp_path, monkeypatch):
    """A later boot must not trip over the directories an earlier one left."""
    monkeypatch.setattr(
        service_module, "SandboxManager",
        lambda config, **kwargs: SimpleNamespace(deployment=kwargs["deployment"]))
    root = tmp_path / "root"
    for _ in range(2):
        value = SelfModificationService(
            SimpleNamespace(), Coordinator(), repository=REPOSITORY, root=root,
            images=Images(), base_image_id=BASE, image_environment=(),
            role_endpoint="http://127.0.0.1:8001/v1", role_model="local",
            runtime_factory=lambda: None,
        )
        assert (await value.prepare()).image_id
        assert value.router.serving.role == "A"
        await value.close()
    assert len(list(root.glob("skills-a-*"))) == 2


async def test_gap_cancels_a_task_and_runs_the_loop_on_the_original_request(service):
    await service.prepare()
    outcome = await service.coordinator.on_gap("session", GAP)
    assert outcome == Outcome(True, "done")
    assert service.coordinator.commands == [("task-a", "cancel")]
    [loop] = service.loops
    assert loop.runs == [GAP] and loop.request == "book the room"
    assert kinds(service)[-2:] == ["gap_accepted", "loop_finished"]
    await service.close()


async def test_a_second_gap_is_declined_while_a_loop_is_running(service):
    await service.prepare()
    entered, release = asyncio.Event(), asyncio.Event()
    service.loops.append(Loop(Outcome(False, "stopped"), entered, release))
    first = asyncio.create_task(service.handle_gap("session", GAP))
    await asyncio.wait_for(entered.wait(), 5)
    assert await service.handle_gap("session", GAP) is None
    assert "gap_declined" in kinds(service)
    release.set()
    assert await first == Outcome(False, "stopped")
    # The loop already finished, so a later user stop has nothing to stop.
    service.stop()
    assert not service.loops[0].stopped
    await service.close()
