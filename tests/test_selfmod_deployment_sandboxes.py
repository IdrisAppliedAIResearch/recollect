"""Verified bundle launch integration: pinned image, bundle skills, routed managers."""

import copy
from dataclasses import replace

import pytest

from recollect.config import RecollectConfig
from recollect.engine.sandbox import configgen, isolation
from recollect.engine.sandbox.isolation import IsolationError
from recollect.engine.sandbox.manager import SandboxDeployment, SandboxManager
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.deployment import (
    DeploymentRouter,
    DeploymentSandboxes,
    SubagentBundle,
    materialize_skills,
)
from recollect.selfmod.journal import IntegrityError, Journal
from tests.selfmod_fake_images import verified
from tests.test_sandbox_isolation import _inspection

BASE = "sha256:" + "a" * 64
IMAGE_A, IMAGE_B = "sha256:" + "b" * 64, "sha256:" + "c" * 64


def bundle(skill=b"---\nname: recollect-reporting\n---\nGeneric reporting.\n"):
    return SubagentBundle(Snapshot((
        File("dependencies.lock", b"httpx==0.28.1\n"),
        File("skills/recollect-reporting/SKILL.md", skill),
        File("tools/generic.py", b"def run():\n    return 1\n"),
    )), BASE, (("entrypoint", "research"),))


def attest(document, workspace, config_dir, image, image_id=None):
    isolation.attest_container(
        document, name="recollect-subagent-test", image=image, workspace=workspace,
        config_dir=config_dir, host_port=41234, memory_mb=1024, pids=256, cpus=2.0,
        image_id=image_id,
    )


def test_resolved_image_pin_rejects_a_retargeted_reference(tmp_path):
    workspace, config_dir = tmp_path / "workspace", tmp_path / "config"
    workspace.mkdir()
    config_dir.mkdir()
    document = _inspection(workspace, config_dir)
    document[0]["Config"]["Image"] = IMAGE_B
    document[0]["Image"] = IMAGE_B
    attest(document, workspace, config_dir, IMAGE_B, IMAGE_B)
    retargeted = copy.deepcopy(document)
    retargeted[0]["Image"] = "sha256:" + "d" * 64
    with pytest.raises(IsolationError, match="resolved image"):
        attest(retargeted, workspace, config_dir, IMAGE_B, IMAGE_B)
    # Without a deployment pin, attestation is exactly the previous behavior.
    attest(retargeted, workspace, config_dir, IMAGE_B)


def test_configgen_copies_bundle_skills_or_packaged_default(tmp_path):
    source = materialize_skills(bundle(b"bundle skill\n"), tmp_path / "bundle-skills")
    common = dict(base_url="http://127.0.0.1:1/v1", model="m", api_key="k", steps=4,
                  continuous=True)
    configgen.write_config(tmp_path / "pinned", skills_source=source, **common)
    pinned = tmp_path / "pinned" / "skills" / "recollect-reporting" / "SKILL.md"
    assert pinned.read_bytes() == b"bundle skill\n"
    assert not (tmp_path / "pinned" / "skills" / "recollect-files").exists()
    configgen.write_config(tmp_path / "default", **common)
    assert (tmp_path / "default" / "skills" / "recollect-files" / "SKILL.md").exists()


def test_materialize_skills_writes_only_the_skills_subtree(tmp_path):
    destination = materialize_skills(bundle(), tmp_path / "skills")
    assert sorted(p.relative_to(destination).as_posix()
                  for p in destination.rglob("*") if p.is_file()) == [
        "recollect-reporting/SKILL.md"]
    no_skills = SubagentBundle(Snapshot((File("dependencies.lock", b"x\n"),)),
                               BASE, ())
    with pytest.raises(IntegrityError, match="skills"):
        materialize_skills(no_skills, tmp_path / "empty")


def test_pinned_manager_launches_only_its_image_from_its_own_root(tmp_path):
    config = RecollectConfig(embedding_model_path=tmp_path / "embedding.gguf",
                             data_dir=tmp_path / "var")
    default = SandboxManager(config)
    assert default.deployment is None
    assert default._launch_image() == config.sandbox_container_image
    assert default._skills_source() is None
    deployment = SandboxDeployment(IMAGE_B, tmp_path / "skills-b", tmp_path / "root-b")
    pinned = SandboxManager(config, deployment=deployment)
    assert pinned._launch_image() == IMAGE_B
    assert pinned._skills_source() == tmp_path / "skills-b"
    assert pinned._root == (tmp_path / "root-b").resolve()
    with pytest.raises(ValueError, match="immutable"):
        SandboxDeployment("recollect-opencode-sandbox:1.18.18", tmp_path, tmp_path)


@pytest.fixture
def routed(tmp_path):
    journal = Journal.create(tmp_path / "routing")
    router = DeploymentRouter(journal)
    receipt_a = verified(bundle(), IMAGE_A)
    router.register_a(receipt_a)
    config = RecollectConfig(embedding_model_path=tmp_path / "embedding.gguf",
                             data_dir=tmp_path / "var")

    def manager(image, name, source_bundle=None):
        skills = tmp_path / ("skills-" + name)
        materialize_skills(source_bundle or bundle(), skills)
        return SandboxManager(config, deployment=SandboxDeployment(
            image, skills, tmp_path / ("root-" + name)))

    router.receipt_a = receipt_a
    yield router, manager
    journal.close()


def test_selector_refuses_managers_for_a_different_image(routed):
    router, manager = routed
    sandboxes = DeploymentSandboxes(router)
    with pytest.raises(IntegrityError, match="recorded image"):
        sandboxes.register("A", manager(IMAGE_B, "wrong"), router.receipt_a)
    with pytest.raises(IntegrityError, match="recorded image"):
        sandboxes.register("B", manager(IMAGE_B, "unstaged"), router.receipt_a)
    edited = manager(IMAGE_A, "edited", bundle(b"edited on disk\n"))
    with pytest.raises(IntegrityError, match="skills differ"):
        sandboxes.register("A", edited, router.receipt_a)
    sandboxes.register("A", manager(IMAGE_A, "a"), router.receipt_a)
    with pytest.raises(IntegrityError, match="once"):
        sandboxes.register("A", manager(IMAGE_A, "again"), router.receipt_a)


def test_tasks_get_pinned_sandbox_and_unsealed_b_never_serves(
    routed,
):
    router, manager = routed
    sandboxes = DeploymentSandboxes(router)
    a_manager = manager(IMAGE_A, "a")
    sandboxes.register("A", a_manager, router.receipt_a)
    router.bind("task-existing")
    router.bind("task-original")
    b = replace(bundle(b"B generated reporting\n"))
    receipt_b = verified(b, IMAGE_B)
    router.stage_b(receipt_b)
    b_manager = manager(IMAGE_B, "b", b)
    sandboxes.register("B", b_manager, receipt_b)
    router.begin_activation()
    router.bind("task-new")
    router.link_continuation("task-continued", "task-original")
    assert sandboxes.manager_for("task-existing") is a_manager
    # Unsealed B work never gets a sandbox, including after rollback.
    with pytest.raises(IntegrityError, match="CP4 seal"):
        sandboxes.manager_for("task-new")
    router.rollback("cp4_failed_fixture")
    with pytest.raises(IntegrityError, match="CP4 seal"):
        sandboxes.manager_for("task-new")
    with pytest.raises(IntegrityError, match="CP4 seal"):
        sandboxes.manager_for("task-continued")
    with pytest.raises(KeyError):
        sandboxes.manager_for("task-unbound")
