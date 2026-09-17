"""Bundle identity, image verification and cleanup, and A/B task routing."""

from dataclasses import replace

import pytest

from recollect.selfmod.contracts import File, IntegrityError, Snapshot
from recollect.selfmod.deployment import (
    BundleImages,
    Deployment,
    Deployments,
    SubagentBundle,
    bundle_tar,
    read_bundle_tar,
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
    return bundle(Snapshot((File("extension.py", b"generated capability\n"),
                            File("dependencies.lock", b"httpx==0.28.1\n"))))


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


def deployment(role, image_id, value=None):
    return Deployment(role, verified(value or bundle(), image_id), object())


def test_new_work_runs_on_a_and_the_resumed_request_on_b():
    a = deployment("A", IMAGE_A)
    routes = Deployments(a)
    assert routes.bind("task-existing") is a
    with pytest.raises(IntegrityError, match="already bound"):
        routes.bind("task-existing")
    with pytest.raises(IntegrityError, match="staged B"):
        routes.link_continuation("task-continued", "task-original")
    b = deployment("B", IMAGE_B, b_bundle())
    routes.stage_b(b)
    with pytest.raises(IntegrityError, match="already staged"):
        routes.stage_b(b)
    routes.link_continuation("task-continued", "task-original")
    assert routes.bind("task-new") is a
    assert routes.manager_for("task-continued") is b.manager
    assert routes.manager_for("task-new") is a.manager
    # Work that predates routing runs on A.
    assert routes.manager_for("task-unbound") is a.manager


def test_promoted_b_serves_everything_and_a_discarded_b_serves_nothing():
    a = deployment("A", IMAGE_A)
    routes = Deployments(a)
    routes.bind("task-old")
    b = deployment("B", IMAGE_B, b_bundle())
    routes.stage_b(b)
    routes.link_continuation("task-continued", "task-original")
    assert routes.promote() is a
    assert routes.a is b and b.role == "A" and routes.b is None
    assert routes.manager_for("task-old") is b.manager
    assert routes.bind("task-later") is b
    with pytest.raises(IntegrityError, match="promote"):
        routes.promote()
    retry = deployment("B", "sha256:" + "d" * 64, b_bundle())
    routes.stage_b(retry)
    routes.link_continuation("task-retry", "task-original")
    assert routes.discard_b() is retry and routes.discard_b() is None
    assert routes.manager_for("task-retry") is b.manager


async def test_remove_and_sweep_leave_only_the_serving_image():
    fake, value = FakeImages(), bundle()
    stale = "sha256:" + "d" * 64
    for image_id in (IMAGE_A, IMAGE_B, stale):
        fake.add(value, image_id)
    images = BundleImages(fake)
    assert await images.sweep(keep=IMAGE_A) == [IMAGE_B, stale]
    assert fake.removed == [IMAGE_B, stale]
    assert IMAGE_A in fake.images and fake.images.keys() >= {BASE}
    await images.remove(IMAGE_A)
    assert IMAGE_A not in fake.images


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


async def test_build_reuses_a_verified_image_for_identical_bundle_bytes():
    fake = FakeImages()
    value = bundle()
    # A forged label on different bytes is skipped, never reused.
    forged = "sha256:" + "e" * 64
    fake.add(value, forged, tar=bundle_tar(b_bundle()))
    image_id = "sha256:" + "d" * 64
    fake.add(value, image_id)
    # The fake has no commit: building a new image would fail the test.
    assert await BundleImages(fake).build(value) == image_id
