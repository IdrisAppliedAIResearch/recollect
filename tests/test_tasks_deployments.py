"""Deployment-aware tasks: A/B sandboxes and the held original-task continuation."""

import asyncio

import pytest

from recollect import tasks as tasks_module
from recollect.engine.sandbox.manager import SandboxDeployment, SandboxManager
from recollect.engine.sandbox.runner import TaskReport
from recollect.engine.subagent import SubagentResult
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.deployment import Deployment, Deployments, materialize_skills
from tests.selfmod_fake_images import verified
from tests.test_selfmod_deployment import BASE, IMAGE_A, IMAGE_B
from tests.test_tasks import environment, submit, wait_state  # noqa: F401


def bundle(reporting=b"generic reporting\n"):
    from recollect.selfmod.deployment import SubagentBundle

    return SubagentBundle(Snapshot((
        File("dependencies.lock", b"httpx==0.28.1\n"),
        File("skills/recollect-reporting/SKILL.md", reporting),
    )), BASE, (("entrypoint", "research"),))


@pytest.fixture
def routed(environment, tmp_path, monkeypatch):  # noqa: F811 - pytest fixture
    state = environment
    config = state.coordinator.config

    def deployment(role, image, source):
        skills = materialize_skills(source, tmp_path / ("skills-" + role))
        manager = SandboxManager(config, deployment=SandboxDeployment(
            image, skills, tmp_path / ("root-" + role)))
        return Deployment(role, verified(source, image), manager)

    state.a = deployment("A", IMAGE_A, bundle())
    state.b = deployment("B", IMAGE_B, bundle(b"B reporting\n"))
    state.deployments = Deployments(state.a)
    state.managers = []
    state.coordinator.deployments = state.deployments

    class Runner:
        def __init__(self, used, config):
            state.managers.append(used)

        async def run_continuous(self, session_id, task, **kwargs):
            state.calls.append((session_id, task))
            # Report IDs are unique across a conversation's task mailbox.
            await kwargs["report"](TaskReport(
                "accepted", "Accepted", kwargs["revision"],
                call_id=f"accept-{len(state.calls)}"))
            yield SubagentResult("task", "ok", "{}", "Finished")

    monkeypatch.setattr(tasks_module, "OpenCodeRunner", Runner)
    yield state


async def test_new_work_binds_to_a_and_runs_on_its_sandbox(routed):
    state = routed
    await state.coordinator.start()
    task = await submit(state)
    await wait_state(state, task["task_id"], "completed")
    assert state.deployments.is_bound(task["task_id"])
    assert state.managers == [state.a.manager]


async def test_continuation_is_held_until_released_then_runs_on_b(routed):
    state = routed
    await state.coordinator.start()
    original = await submit(state, "original")
    await wait_state(state, original["task_id"], "completed")
    b = state.b
    state.deployments.stage_b(b)
    held = await submit(state, "continue-original",
                        parent_task_id=original["task_id"], continuation=True)
    await asyncio.sleep(0.05)
    current = state.store.get(state.session_id, held["task_id"])
    assert current["state"] == "queued" and len(state.calls) == 1
    assert state.coordinator._has_owner(current)
    await state.coordinator.release_held(state.session_id, held["task_id"])
    await wait_state(state, held["task_id"], "completed")
    assert state.managers == [state.a.manager, b.manager]
    with pytest.raises(ValueError, match="held"):
        await state.coordinator.release_held(state.session_id, held["task_id"])
    # Once B is promoted, all later work runs on it.
    state.deployments.promote()
    later = await submit(state, "after-promotion")
    await wait_state(state, later["task_id"], "completed")
    assert state.managers[-1] is b.manager


async def test_structured_gap_report_starts_self_modification(routed, monkeypatch):
    state = routed
    gaps = []

    async def on_gap(session_id, gap):
        gaps.append((session_id, gap))

    report = ('I cannot do this.\n```capability_gap\n{"type": "capability_gap", '
              '"missing_capability": "calendar write", "attempted": ["search"], '
              '"modification_request": "add a calendar tool"}\n```')

    class Runner:
        def __init__(self, used, config):
            pass

        async def run_continuous(self, session_id, task, **kwargs):
            await kwargs["report"](TaskReport("blocked", report, kwargs["revision"],
                                              call_id="gap"))
            yield SubagentResult("task", "ok", "{}", "Blocked")

    monkeypatch.setattr(tasks_module, "OpenCodeRunner", Runner)
    state.coordinator.on_gap = on_gap
    await state.coordinator.start()
    task = await submit(state, "needs-a-new-capability")
    for _ in range(200):
        if gaps:
            break
        await asyncio.sleep(0.01)
    [(session_id, gap)] = gaps
    assert session_id == state.session_id and gap["task_id"] == task["task_id"]
    assert gap["missing_capability"] == "calendar write" and gap["revision"] == 1


async def test_held_continuation_requires_deployments_and_a_parent(routed):
    state = routed
    with pytest.raises(ValueError, match="parent"):
        await submit(state, "no-parent", continuation=True)
    state.coordinator.deployments = None
    with pytest.raises(ValueError, match="deployment"):
        await submit(state, "no-deployments", parent_task_id="x", continuation=True)
