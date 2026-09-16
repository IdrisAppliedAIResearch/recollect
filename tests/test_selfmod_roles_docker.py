"""Opt-in real role processes with scripted inference, plus a local-model probe."""

import asyncio
import json
import os
import threading

import httpx
import pytest

from recollect.selfmod.contracts import File, Requirement, TaskContract
from recollect.selfmod.development import Stage
from recollect.selfmod.integration import DevelopmentSettings
from recollect.selfmod.journal import IntegrityError
from recollect.selfmod.roles import LocalRoleModel
from recollect.selfmod.round import ModificationRound
from tests.selfmod_round_helpers import role_contexts, round_config, submitted
from tests.test_selfmod_roles import CHECKS, response, settings
from tests.test_selfmod_runtime_docker import TransportProbe
from tests.test_selfmod_runtime_docker import live as live

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RECOLLECT_RUN_SELFMOD_DOCKER_TESTS") != "1",
        reason="opt in to real isolated role qualification",
    ),
]

PLAN = {"changes": [{"path": "editable.py", "operation": "modify",
                     "requirement_ids": ["value"], "reason": "nécessaire — task"}],
        "verification": [{"requirement_id": "value", "method": "frozen unit check"}]}
REVIEW = {"approved": True, "findings": [],
          "rationale": "Review’s conclusion: the narrow edit addresses the task."}
EDIT = {"edits": [{"path": "editable.py", "text": "value = 2\n"}]}


def development(live):
    fixture = live.spec()
    contract = TaskContract(
        "Change editable.py so its value equals 2. Preserve every other source file.",
        (Requirement("value", "editable.py contains value = 2", "unit check"),),
        ("unit", "regression"), fixture.policy.sha256,
    )
    controller = ModificationRound.create(
        live.archive / "controller", round_config(contract, fixture.baseline.sha256))
    live.controllers.append(controller)
    dev = controller.open_development(
        baseline=fixture.baseline, policy=fixture.policy,
        settings=DevelopmentSettings(fixture.image_id, fixture.image_environment,
                                     fixture.entrypoint),
    )
    return controller, dev


def model(profile, result, calls=None):
    def handle(request):
        if calls is not None:
            calls.append(json.loads(request.content))
        return response(result)

    return LocalRoleModel(profile, transport=httpx.MockTransport(handle))


async def role(live, dev, action, profile, result=None, calls=None):
    runtime = live.runtime()
    await dev.run_role(dev.authorize(action), profile, runtime,
                       model=model(profile, result, calls) if result is not None
                       else None)
    assert not await live.ids(runtime._spec)
    return runtime


async def implementation(live, dev, profile, edit=EDIT):
    await role(live, dev, "plan", profile, PLAN)
    await role(live, dev, "review", profile, REVIEW)
    return await role(live, dev, "execute", profile, edit)


async def test_all_roles_use_separate_containers_and_seal_owned_evidence(live):
    controller, dev = development(live)
    profile, calls, runtimes = settings(), [], []
    for action, result in (("plan", PLAN), ("review", REVIEW), ("execute", EDIT),
                           ("checks", None), ("review", REVIEW)):
        runtimes.append(await role(live, dev, action, profile, result, calls))
    assert dev.stage == Stage.READY and len(calls) == 4
    assert len({r._worker.container_id for r in runtimes}) == 5
    contexts = role_contexts(controller)
    assert len({c["request_id"] for c in contexts}) == 4
    assert contexts[0]["actor_id"] == contexts[2]["actor_id"]
    assert len({contexts[i]["actor_id"] for i in (0, 1, 3)}) == 3
    for runtime in (runtimes[0], runtimes[1], runtimes[3], runtimes[4]):
        assert runtime._spec.policy.modify == runtime._spec.policy.create_under == ()
    number, _ = dev.submit(dev.authorize("submit"))
    assert number == 1
    candidate = submitted(controller)
    assert candidate["candidate/editable.py"] == b"value = 2\n"
    for record in controller.journal.verify():
        if record.value["kind"] == "development_role":
            for file in record.files.files:
                assert candidate[f"records/{record.anchor.sequence}/{file.path}"] == (
                    file.content
                )


async def test_driver_revises_rejections_and_failed_real_checks_without_submitting(
    live,
):
    controller, dev = development(live)
    profile, calls, runtimes = settings(), [], []
    reject = {**REVIEW, "approved": False, "rationale": "Revise the proposal"}
    wrong = {"edits": [{"path": "editable.py", "text": "value = 3\n"}]}
    replies = iter([PLAN, reject, PLAN, REVIEW, wrong, EDIT, reject, EDIT, REVIEW])

    def runtime_factory():
        runtime = live.runtime()
        runtimes.append(runtime)
        return runtime

    await dev.run_until_ready(
        profile, runtime_factory,
        model_factory=lambda p: model(p, next(replies), calls),
    )
    assert dev.stage == Stage.READY and controller._number == 0
    assert len({r._worker.container_id for r in runtimes}) == 12
    for runtime in runtimes:
        assert not await live.ids(runtime._spec)
    contexts = role_contexts(controller)
    assert contexts[1]["candidate"] == []
    assert contexts[1]["candidate_sha256"] is None
    assert "before any code is written" in calls[1]["messages"][0]["content"]
    assert any(r["kind"] == "checks" and not r["report"]["results"][0][1]
               for r in contexts[-1]["history"])
    assert dev.submit(dev.authorize("submit"))[0] == 1
    assert submitted(controller)["candidate/editable.py"] == b"value = 2\n"


async def test_failed_check_is_revisable_not_a_success_or_driver_crash(live):
    controller, dev = development(live)
    profile = settings(checks=(File("unit.py", b"raise SystemExit(7)\n"), CHECKS[1]))
    await implementation(live, dev, profile)
    await role(live, dev, "checks", profile)
    assert dev.stage == Stage.IMPLEMENT
    with pytest.raises(IntegrityError):
        dev.authorize("submit")
    assert any(r.value["kind"] == "development_input" and
               r.value["data"].get("kind") == "checks"
               for r in controller.journal.verify())


async def test_check_child_cannot_forge_parent_report_or_mutate_source(live):
    attack = b"""
import errno, os
from pathlib import Path
for path in (f'/proc/{os.getppid()}/fd/1', f'/proc/{os.getppid()}/mem',
             '/work/source/editable.py', '/work/forged.json', '/input/spec.json'):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        assert exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}, (path, exc)
    else:
        os.close(descriptor)
        raise RuntimeError('result/source boundary writable: ' + path)
print('{"checks":[{"name":"unit","passed":true,"exitcode":0}]}')
raise SystemExit(7)
"""
    _, dev = development(live)
    profile = settings(checks=(File("unit.py", attack), CHECKS[1]))
    await implementation(live, dev, profile)
    await role(live, dev, "checks", profile)
    assert dev.stage == Stage.IMPLEMENT
    record = [r for r in dev._controller.journal.verify()
              if r.value["kind"] == "development_role"
              and r.value["data"].get("state") == "verified"][-1]
    unit = record.value["data"]["result"]["checks"][0]
    assert unit["exitcode"] == 7 and unit["passed"] is False


@pytest.mark.parametrize("exitcode", [0, 7])
async def test_check_log_boundary_fits_encoded_report(live, exitcode):
    _, dev = development(live)
    script = ("import sys\nsys.stdout.write('x' * 32768)\n"
              f"raise SystemExit({exitcode})\n").encode()
    profile = settings(checks=(File("unit.py", script), CHECKS[1]))
    await implementation(live, dev, profile)
    await role(live, dev, "checks", profile)
    assert dev.stage == (Stage.CODE_REVIEW if exitcode == 0 else Stage.IMPLEMENT)


async def test_check_log_overflow_is_terminal_and_retains_failure(live):
    controller, dev = development(live)
    script = b"import sys\nsys.stdout.write('x' * 32769)\n"
    profile = settings(checks=(File("unit.py", script), CHECKS[1]))
    await implementation(live, dev, profile)
    with pytest.raises(IntegrityError, match="Unusable"):
        await role(live, dev, "checks", profile)
    assert not controller.eligible
    failures = [f.path for r in controller.journal.verify()
                if r.value["kind"] == "development_failure" for f in r.files.files]
    assert any(p.endswith("report.json") for p in failures)


async def test_role_revision_recreates_allowed_new_file_from_original_baseline(live):
    _, dev = development(live)
    profile = settings()
    plan = {**PLAN, "changes": [*PLAN["changes"], {
        "path": "generated/new.py", "operation": "create",
        "requirement_ids": ["value"], "reason": "non-target revision fixture",
    }]}
    await role(live, dev, "plan", profile, plan)
    await role(live, dev, "review", profile, REVIEW)
    for value in ("first revision", "second revision"):
        edit = {"edits": [*EDIT["edits"], {"path": "generated/new.py", "text": value}]}
        await role(live, dev, "execute", profile, edit)
    await role(live, dev, "checks", profile)
    await role(live, dev, "review", profile, REVIEW)
    dev.submit(dev.authorize("submit"))
    assert submitted(dev._controller)["candidate/generated/new.py"] == (
        b"second revision"
    )


@pytest.mark.parametrize("point", ["inference", "release"])
async def test_role_cancellation_accounts_failure_and_never_submits(live, point):
    controller, dev = development(live)
    profile = settings()
    entered = asyncio.Event()
    released, proceed = threading.Event(), threading.Event()
    probe = TransportProbe()

    def hook(kind, _):
        if kind == "release":
            released.set()
            assert proceed.wait(3), "test release barrier timed out"

    probe.hook = hook

    async def handle(request):
        entered.set()
        if point == "inference":
            await asyncio.Event().wait()
        return response(PLAN)

    broker = LocalRoleModel(profile, transport=httpx.MockTransport(handle))
    runtime = live.runtime(probe)
    task = asyncio.create_task(dev.run_role(dev.authorize("plan"), profile,
                                           runtime, model=broker))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if point == "release":
            assert await asyncio.to_thread(released.wait, 8)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and dev._busy
    finally:
        proceed.set()
        if not task.done():
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not controller.eligible
    archived = [f.path for r in controller.journal.verify()
                if r.value["kind"] in {"development_role", "development_failure"}
                for f in r.files.files]
    assert "model-request.json" in archived
    with pytest.raises(AssertionError, match="no candidate"):
        submitted(controller)
    if point == "inference":
        assert runtime._spec is None
    else:
        assert not await live.ids(runtime._spec)


async def test_model_call_count_is_audit_only_not_a_dispatch_limit(live):
    controller, dev = development(live)
    profile = settings()
    controller._role_model_calls = 100_000
    calls = []
    await role(live, dev, "plan", profile, PLAN, calls)
    await role(live, dev, "review", profile, REVIEW, calls)
    assert len(calls) == 2 and controller._role_model_calls == 100_002
    assert dev.stage == Stage.IMPLEMENT
    assert all("max_tokens" not in call for call in calls)


async def test_check_script_change_is_rejected_before_any_model_or_container(live):
    _, dev = development(live)
    profile = settings()
    await role(live, dev, "plan", profile, PLAN)
    altered = settings(checks=(File("unit.py", b"pass\n"), CHECKS[1]))
    calls = []
    with pytest.raises(IntegrityError, match="profile changed"):
        await role(live, dev, "review", altered, REVIEW, calls)
    assert not calls


@pytest.mark.skipif(
    os.environ.get("RECOLLECT_RUN_SELFMOD_MODEL_TESTS") != "1",
    reason="separate opt-in for real local-model non-target role probe",
)
async def test_local_model_plans_reviews_and_modifies_non_target_fixture(live):
    _, dev = development(live)
    profile = settings(model="Qwen3.8-27B-UD-Q4_K_XL.gguf")
    for action in ("plan", "review", "execute", "checks", "review"):
        await role(live, dev, action, profile)
    assert dev.stage == Stage.READY
    number, _ = dev.submit(dev.authorize("submit"))
    assert number == 1


async def test_replacement_edit_and_checks_import_tree_and_pinned_packages(live):
    """Checks run in the role container with the candidate tree and /opt/python."""
    _, dev = development(live)
    check = File("unit.py", (
        b"import httpx\n"
        b"import editable\n"
        b"assert editable.value == 2, editable.value\n"
        b"assert httpx.MockTransport\n"
    ))
    profile = settings(checks=(check, CHECKS[1]))
    replacement = {"edits": [{"path": "editable.py", "replace": [
        {"old": "value = 1", "new": "value = 2"}]}]}
    await implementation(live, dev, profile, replacement)
    await role(live, dev, "checks", profile)
    assert dev.stage == Stage.CODE_REVIEW
