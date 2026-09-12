import os

import pytest

from recollect.selfmod.checkpoints import bundle, validate_bundle, verify_materialized
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.controller import PREFLIGHT_CHECKS
from recollect.selfmod.journal import IntegrityError, decode, encode, sha256
from tests.selfmod_checkpoint_helpers import EVIDENCE, create, good, through_baseline


@pytest.fixture
def prepared(tmp_path):
    controller = create(tmp_path / "attempt")
    controller.preflight(good(PREFLIGHT_CHECKS), EVIDENCE)
    record = next(
        r
        for r in controller.journal.verify()
        if r.value["kind"] == "checkpoint_prepared"
    )
    yield controller, record
    controller.close()


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "extra",
        "corrupted",
        "extra_directory",
        "hardlink",
    ],
)
def test_materialized_bundle_requires_exact_inventory_and_regular_files(
    prepared, change
):
    controller, record = prepared
    root = controller.journal.root / "checkpoints" / record.value["data"]["name"]
    target = root / "fixture.txt"
    if change == "missing":
        target.unlink()
    elif change == "extra":
        (root / "unexpected.txt").write_bytes(b"extra")
    elif change == "corrupted":
        target.write_bytes(b"changed")
    elif change == "extra_directory":
        (root / "empty-extra").mkdir()
    else:
        os.link(target, root.parent / "outside-hardlink")
    with pytest.raises((IntegrityError, FileNotFoundError)):
        verify_materialized(root, record.files)
    with pytest.raises((IntegrityError, FileNotFoundError)):
        controller.receive_request()
    assert not controller._eligible


def rewritten(files, edit):
    manifest = decode(next(f.content for f in files.files if f.path == "manifest.json"))
    edit(manifest)
    raw = encode(manifest)
    digest = sha256(raw)
    return digest, Snapshot(
        tuple(
            File(
                f.path,
                raw
                if f.path == "manifest.json"
                else (digest + "  manifest.json\n").encode()
                if f.path == "manifest.sha256"
                else f.content,
            )
            for f in files.files
        )
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("checkpoint_sequence", True),
        ("checkpoint_id", "CP1.1"),
        ("checkpoint_id", "CP2.1.retry1"),
        ("candidate_number", 1),
        ("previous_checkpoint_sha256", "0" * 64),
        ("gate", 1),
        ("reasons", "not-an-array"),
        ("observations", []),
        ("registrations", {}),
        ("unexpected_future_cp6_hash", "0" * 64),
    ],
)
def test_schema_rejects_forged_but_self_consistently_hashed_manifests(
    prepared,
    field,
    value,
):
    _, record = prepared
    digest, files = rewritten(record.files, lambda m: m.update({field: value}))
    with pytest.raises(IntegrityError):
        validate_bundle(files, digest)


def test_inventory_length_boolean_is_not_an_integer_equivalent(prepared):
    _, record = prepared

    def change(manifest):
        manifest["files"][0]["bytes"] = True

    digest, files = rewritten(record.files, change)
    with pytest.raises(IntegrityError, match="inventory"):
        validate_bundle(files, digest)


def test_bundle_cannot_inventory_its_manifest_or_checksum(prepared):
    _, record = prepared
    metadata = decode(
        next(f.content for f in record.files.files if f.path == "manifest.json")
    )
    metadata.pop("files")
    for path in ("manifest.json", "manifest.sha256", "MANIFEST.JSON"):
        with pytest.raises(IntegrityError, match="inventory themselves"):
            bundle(metadata, Snapshot((File(path, b"self"),)))


def test_previous_checkpoint_corruption_blocks_later_dispatch(tmp_path):
    controller = create(tmp_path / "attempt")
    try:
        through_baseline(controller)
        cp0 = (
            controller.journal.root
            / "checkpoints"
            / controller._checkpoints[0].name
            / "fixture.txt"
        )
        cp0.write_bytes(b"corruption after successful verification")
        with pytest.raises(IntegrityError):
            controller.dispatch("modify")
        assert not controller._eligible
    finally:
        controller.close()
