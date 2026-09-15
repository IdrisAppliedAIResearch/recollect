"""A/B bundle identity and CP4-gated routing with non-target fixtures."""

from dataclasses import replace

import pytest

from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.controller import Controller
from recollect.selfmod.deployment import (
    DeploymentRouter,
    SubagentBundle,
    bundle_tar,
    read_bundle_tar,
)
from recollect.selfmod.journal import IntegrityError, Journal
from tests.selfmod_checkpoint_helpers import (
    ARTIFACT,
    config,
    evaluate,
    submit,
    through_baseline,
)
from tests.selfmod_fake_images import FakeImages, verified

BASE = "sha256:" + "a" * 64
IMAGE_A, IMAGE_B = "sha256:" + "b" * 64, "sha256:" + "c" * 64


def bundle(candidate=None):
    candidate = candidate or Snapshot((
        File("dependencies.lock", b"httpx==0.28.1\n"),
        File("tools/research.py", b"def research():\n    return 'generic'\n"),
    ))
    return SubagentBundle(candidate, BASE, (("entrypoint", "research"),))


def b_bundle():
    return bundle(Snapshot((*ARTIFACT.files,
                            File("dependencies.lock", b"httpx==0.28.1\n"))))


@pytest.fixture
def controller(tmp_path):
    value = Controller.create(tmp_path / "controller", config())
    through_baseline(value)
    yield value
    value.journal.close()


def accepted_controller(controller, candidate):
    submit(controller, artifact=candidate)
    evaluate(controller)
    return controller


@pytest.fixture
def router(tmp_path):
    journal = Journal.create(tmp_path / "routing")
    value = DeploymentRouter(journal)
    value.journal = journal
    yield value
    journal.close()


def good_checks(router, **overrides):
    values = dict(b_healthy=True, a_available=True, target_quiescent=True,
                  baseline_empty=True)
    values.update(overrides)
    return router.activation_checks(**values)


def test_bundle_digest_binds_candidate_lock_base_and_launch():
    first = bundle()
    assert first.digest == bundle().digest
    changed = [
        bundle(Snapshot((*first.candidate.files, File("extra.py", b"x")))),
        replace(first, base_image_id="sha256:" + "d" * 64),
        replace(first, launch=(("entrypoint", "other"),)),
    ]
    assert len({first.digest, *(b.digest for b in changed)}) == 4
    with pytest.raises(ValueError, match="lock"):
        SubagentBundle(Snapshot((File("tool.py", b"x"),)), BASE, ())
    with pytest.raises(ValueError, match="immutable"):
        SubagentBundle(first.candidate, "recollect:latest", ())


def test_bundle_archive_roundtrip_rejects_links_and_escapes():
    value = bundle()
    files, manifest = read_bundle_tar(bundle_tar(value))
    assert files == value.candidate and manifest is not None
    import io
    import tarfile

    for kind in ("symlink", "escape"):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as archive:
            info = tarfile.TarInfo("recollect-bundle/link" if kind == "symlink"
                                   else "other/file")
            if kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                archive.addfile(info)
            else:
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
        with pytest.raises(IntegrityError):
            read_bundle_tar(raw.getvalue())


def test_existing_tasks_keep_a_new_tasks_bind_b_and_continuation_waits_for_cp4(
    router, controller,
):
    router.register_a(verified(bundle(), IMAGE_A))
    existing = router.bind("task-existing")
    original = router.bind("task-original")
    assert existing.role == original.role == "A"
    b = b_bundle()
    accepted_controller(controller, b.candidate)
    router.stage_b(verified(b, IMAGE_B))
    router.begin_activation()
    assert router.bind("task-new").role == "B"
    with pytest.raises(IntegrityError, match="CP4 seal"):
        router.route("task-new")
    router.link_continuation("task-original-continued", "task-original")
    with pytest.raises(IntegrityError, match="CP4 seal"):
        router.route("task-original-continued")
    checks = good_checks(router)
    assert all(checks.values())
    assert router.seal(controller, checks) is True
    assert controller._phase == "continue"
    assert router.route("task-new").role == "B"
    with pytest.raises(IntegrityError, match="release"):
        router.route("task-original-continued")
    continued = router.release_continuation("task-original-continued")
    assert continued.role == "B" and continued.image_id == IMAGE_B
    # Existing A work is not moved; A drains only when it finishes.
    assert router.route("task-existing").role == "A"
    assert not router.drained("A")
    router.finish("task-existing")
    router.finish("task-original")
    assert router.drained("A")


@pytest.mark.parametrize("fault", ["unhealthy", "a_unavailable", "no_continuation",
                                   "wrong_candidate"])
def test_failed_cp4_rolls_new_work_back_to_a_and_keeps_continuation_blocked(
    router, controller, fault,
):
    router.register_a(verified(bundle(), IMAGE_A))
    router.bind("task-original")
    b = b_bundle()
    accepted_controller(controller, b.candidate if fault != "wrong_candidate"
                        else Snapshot((File("different.py", b"x"),)))
    router.stage_b(verified(b, IMAGE_B))
    router.begin_activation()
    if fault != "no_continuation":
        router.link_continuation("task-continued", "task-original")
    checks = good_checks(router, b_healthy=fault != "unhealthy",
                         a_available=fault != "a_unavailable")
    assert router.seal(controller, checks) is False
    assert controller._phase != "continue"
    assert router.serving.role == "A"
    assert router.bind("task-after-failure").role == "A"
    if fault != "no_continuation":
        with pytest.raises(IntegrityError):
            router.release_continuation("task-continued")


def test_explicit_rollback_restores_recorded_a_digest(router, controller):
    a = bundle()
    router.register_a(verified(a, IMAGE_A))
    router.bind("task-original")
    b = b_bundle()
    accepted_controller(controller, b.candidate)
    router.stage_b(verified(b, IMAGE_B))
    router.begin_activation()
    router.link_continuation("task-continued", "task-original")
    assert router.seal(controller, good_checks(router))
    router.rollback("operator_rollback_fixture")
    serving = router.serving
    assert serving.role == "A" and serving.bundle_digest == a.digest
    assert serving.image_id == IMAGE_A
    assert router.bind("task-later").role == "A"


def test_crash_during_unsealed_activation_recovers_to_a(tmp_path, controller):
    root = tmp_path / "routing"
    with Journal.create(root) as journal:
        router = DeploymentRouter(journal)
        router.register_a(verified(bundle(), IMAGE_A))
        router.bind("task-original")
        b = b_bundle()
        router.stage_b(verified(b, IMAGE_B))
        router.begin_activation()
        assert router.bind("task-during").role == "B"
    with Journal.recover(root) as journal:
        recovered = DeploymentRouter.recover(journal)
        assert recovered.serving.role == "A"
        # Work bound to the unsealed B is voided: never served, never moved.
        with pytest.raises(IntegrityError, match="CP4 seal"):
            recovered.route("task-during")
        with pytest.raises(IntegrityError, match="never-rolled-back"):
            recovered.begin_activation()
        assert [r.value["data"]["reason"] for r in journal.verify()
                if r.value["kind"] == "deployment_rolled_back"] == [
            "recovered_unsealed_activation"]


def test_routing_state_is_replayed_exactly_from_receipts(tmp_path, controller):
    root = tmp_path / "routing"
    with Journal.create(root) as journal:
        router = DeploymentRouter(journal)
        router.register_a(verified(bundle(), IMAGE_A))
        router.bind("task-original")
        b = b_bundle()
        accepted_controller(controller, b.candidate)
        router.stage_b(verified(b, IMAGE_B))
        router.begin_activation()
        router.link_continuation("task-continued", "task-original")
        router.seal(controller, good_checks(router))
        router.release_continuation("task-continued")
        epoch = router.epoch
    with Journal.recover(root) as journal:
        replayed = DeploymentRouter(journal)
        assert replayed.serving.role == "B" and replayed.epoch == epoch
        assert replayed.route("task-continued").role == "B"
        with pytest.raises(IntegrityError, match="already"):
            replayed.release_continuation("task-continued")


def test_router_refuses_out_of_order_operations(router):
    with pytest.raises(IntegrityError):
        router.stage_b(verified(b_bundle(), IMAGE_B))
    router.register_a(verified(bundle(), IMAGE_A))
    with pytest.raises(IntegrityError):
        router.begin_activation()
    with pytest.raises(IntegrityError):
        router.link_continuation("t", "missing")
    router.bind("task")
    with pytest.raises(IntegrityError):
        router.bind("task")


def test_failed_cp4_never_serves_work_bound_during_activation(router, controller):
    router.register_a(verified(bundle(), IMAGE_A))
    router.bind("task-original")
    b = b_bundle()
    accepted_controller(controller, b.candidate)
    router.stage_b(verified(b, IMAGE_B))
    router.begin_activation()
    router.bind("task-during")
    router.link_continuation("task-continued", "task-original")
    assert router.seal(controller, good_checks(router, b_healthy=False)) is False
    with pytest.raises(IntegrityError, match="CP4 seal"):
        router.route("task-during")
    with pytest.raises(IntegrityError):
        router.begin_activation()


def test_router_accepts_only_verify_receipts(router):
    from recollect.selfmod.deployment import VerifiedImage

    with pytest.raises(IntegrityError, match="verify"):
        router.register_a(bundle())
    with pytest.raises(IntegrityError, match="only from verify"):
        VerifiedImage(bundle(), IMAGE_A, object())


@pytest.mark.parametrize("fault", ["labels", "base", "bytes", "missing_layer"])
def test_verify_rejects_copied_labels_wrong_base_or_changed_bytes(fault):
    fake, value = FakeImages(), bundle()
    if fault == "labels":
        fake.add(value, IMAGE_B, labels={"recollect.bundle": "0" * 64})
    elif fault == "base":
        fake.add(value, IMAGE_B, layers=["sha256:other", "sha256:base-2", "x"])
    elif fault == "missing_layer":
        fake.add(value, IMAGE_B, layers=["sha256:base-1", "sha256:base-2"])
    else:
        fake.add(value, IMAGE_B, tar=bundle_tar(bundle(Snapshot((
            File("dependencies.lock", b"httpx==0.28.1\n"),
            File("tools/research.py", b"changed\n"))))))
    with pytest.raises(IntegrityError):
        verified(value, IMAGE_B, fake)
