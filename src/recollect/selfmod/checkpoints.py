"""Exact-byte checkpoint bundles with mandatory disk/archive agreement."""

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .contracts import File, Snapshot, require_digest
from .journal import (
    IntegrityError,
    Record,
    decode,
    encode,
    inventory,
    regular,
    root_path,
    sha256,
    write_new,
)

_MANIFEST_FIELDS = {
    "schema_version",
    "archive_sequence",
    "attempt_id",
    "checkpoint_id",
    "checkpoint_sequence",
    "previous_checkpoint_sha256",
    "registrations",
    "candidate_number",
    "candidate_sha256",
    "collection_started",
    "collection_completed",
    "seal_prepared",
    "actor_id",
    "artifacts",
    "observations",
    "gate",
    "reasons",
    "missing_evidence",
    "deviations",
    "files",
}


@dataclass(frozen=True)
class SealedCheckpoint:
    name: str
    manifest_sha256: str
    prepare_sequence: int
    seal_sequence: int
    manifest: bytes

    @property
    def value(self) -> dict:
        return decode(self.manifest)


def bundle(metadata: dict, evidence: Snapshot) -> tuple[str, Snapshot]:
    if not evidence.files:
        raise IntegrityError("Checkpoints require observed evidence")
    if any(
        f.path.lower() in {"manifest.json", "manifest.sha256"} for f in evidence.files
    ):
        raise IntegrityError("Manifest/receipt cannot inventory themselves")
    manifest = encode({**metadata, "files": inventory(evidence)})
    manifest_sha = sha256(manifest)
    result = Snapshot(
        (
            *evidence.files,
            File("manifest.json", manifest),
            File(
                "manifest.sha256", (manifest_sha + "  manifest.json\n").encode("ascii")
            ),
        )
    )
    validate_bundle(result, manifest_sha)
    return manifest_sha, result


def validate_bundle(files: Snapshot, expected_sha: str) -> dict:
    require_digest(expected_sha)
    contents = {f.path: f.content for f in files.files}
    try:
        manifest_bytes = contents["manifest.json"]
        checksum = contents["manifest.sha256"]
    except KeyError as exc:
        raise IntegrityError("Missing manifest or detached checksum") from exc
    if sha256(manifest_bytes) != expected_sha or checksum != (
        expected_sha + "  manifest.json\n"
    ).encode("ascii"):
        raise IntegrityError("Manifest/checksum mismatch")
    manifest = decode(manifest_bytes)
    if (
        set(manifest) != _MANIFEST_FIELDS
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or type(manifest["checkpoint_sequence"]) is not int
        or manifest["checkpoint_sequence"] < 1
        or type(manifest["archive_sequence"]) is not int
        or manifest["archive_sequence"] < 1
        or type(manifest["gate"]) is not bool
        or not re.fullmatch(
            r"(?:CP[01456]|CP2\.[1-3]|CP3\.[1-3](?:\.retry1)?)",
            manifest["checkpoint_id"],
        )
    ):
        raise IntegrityError("Invalid checkpoint manifest schema")
    if (
        set(manifest["registrations"]) not in (
            {"protocol", "checkpoints", "amendment", "runtime", "task_contract"},
            {"protocol", "checkpoints", "amendment", "timing_amendment",
             "runtime", "task_contract"},
        )
        or set(manifest["artifacts"]) != {"baseline", "evaluator"}
        or not isinstance(manifest["observations"], dict)
        or any(
            not isinstance(manifest[k], str) or not manifest[k].strip()
            for k in ("attempt_id", "actor_id")
        )
        or any(
            type(manifest[k]) is not list
            or any(not isinstance(s, str) or not s.strip() for s in manifest[k])
            for k in ("reasons", "missing_evidence", "deviations")
        )
    ):
        raise IntegrityError("Invalid checkpoint identity/observation fields")
    number = manifest["candidate_number"]
    candidate = manifest["candidate_sha256"]
    checkpoint = manifest["checkpoint_id"]
    if (
        (number is None) != (candidate is None)
        or number is not None
        and (type(number) is not int or not 1 <= number <= 3)
        or checkpoint in {"CP0", "CP1"}
        and number is not None
        or checkpoint.startswith(("CP2.", "CP3."))
        and number != int(checkpoint.split(".")[1])
        or checkpoint in {"CP4", "CP5"}
        and number is None
        or (manifest["checkpoint_sequence"] == 1)
        != (manifest["previous_checkpoint_sha256"] is None)
    ):
        raise IntegrityError("Checkpoint candidate/predecessor identity mismatch")
    stamps = [
        manifest[k]
        for k in ("collection_started", "collection_completed", "seal_prepared")
    ]
    for stamp in stamps:
        if (
            not isinstance(stamp, dict)
            or set(stamp) != {"monotonic_ns", "utc", "boot_id"}
            or type(stamp["monotonic_ns"]) is not int
            or stamp["monotonic_ns"] < 0
            or not isinstance(stamp["boot_id"], str)
            or not stamp["boot_id"]
        ):
            raise IntegrityError("Invalid checkpoint clock record")
        if datetime.fromisoformat(stamp["utc"]).utcoffset() != UTC.utcoffset(None):
            raise IntegrityError("Checkpoint timestamps must be UTC")
    if len({s["boot_id"] for s in stamps}) != 1 or [
        s["monotonic_ns"] for s in stamps
    ] != sorted(s["monotonic_ns"] for s in stamps):
        raise IntegrityError("Checkpoint collection clock changed or regressed")
    for value in manifest["registrations"].values():
        require_digest(value)
    for value in manifest["artifacts"].values():
        require_digest(value)
    for key in ("previous_checkpoint_sha256", "candidate_sha256"):
        if manifest[key] is not None:
            require_digest(manifest[key])
    evidence = Snapshot(
        tuple(
            f for f in files.files if f.path not in {"manifest.json", "manifest.sha256"}
        )
    )
    if not evidence.files or encode(inventory(evidence)) != encode(manifest["files"]):
        raise IntegrityError("Manifest evidence inventory mismatch or self-reference")
    return manifest


def materialize(root: Path, files: Snapshot, fault=lambda _: None) -> None:
    root_path(root.parent)
    root.mkdir(mode=0o700)
    for file in sorted(files.files, key=lambda f: f.path):
        target = root.joinpath(*file.path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        root_path(target.parent)
        fault("bundle.before_write:" + file.path)
        write_new(target, file.content)
        fault("bundle.after_write:" + file.path)
    # Windows durability is bounded by its VFS/storage contract; the archive
    # independently retains these exact bytes. Never claim a power-loss test.
    if os.name != "nt":
        for directory, _, _ in os.walk(root, topdown=False):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    verify_materialized(root, files)


def verify_materialized(root: Path, expected: Snapshot) -> None:
    root_path(root)
    wanted = {f.path: f.content for f in expected.files}
    directories = {
        "/".join(p.split("/")[:i]) for p in wanted for i in range(1, len(p.split("/")))
    }
    observed = set()
    observed_dirs = set()
    for directory, children, filenames in os.walk(root, followlinks=False):
        for child in children:
            path = Path(directory) / child
            regular(path, directory=True)
            observed_dirs.add(path.relative_to(root).as_posix())
        for name in filenames:
            path = Path(directory) / name
            regular(path)
            relative = path.relative_to(root).as_posix()
            if relative not in wanted:
                raise IntegrityError("Unlisted checkpoint file")
            with path.open("rb") as source:
                data = source.read(len(wanted[relative]) + 1)
            if data != wanted[relative] or sha256(data) != sha256(wanted[relative]):
                raise IntegrityError("Materialized evidence differs from archive")
            observed.add(relative)
    if observed != set(wanted) or observed_dirs != directories:
        raise IntegrityError("Missing or extra checkpoint paths")


def verify_checkpoint_chain(
    root: Path,
    records: tuple[Record, ...],
    *,
    attempt_id: str,
    registrations: dict,
    artifacts: dict,
) -> tuple[SealedCheckpoint, ...]:
    """Independent byte/identity verification; not a provider truth evaluator."""
    prepared = {}
    sealed = []
    consumed = set()
    for record in records:
        value = record.value
        if value["kind"] == "accounting_branch":
            raise IntegrityError(
                "Terminal accounting branch cannot authorize primary work"
            )
        if value["kind"] == "checkpoint_prepared":
            data = value["data"]
            manifest = validate_bundle(record.files, data["manifest_sha256"])
            if (
                manifest["attempt_id"] != attempt_id
                or encode(manifest["registrations"]) != encode(registrations)
                or encode(manifest["artifacts"]) != encode(artifacts)
            ):
                raise IntegrityError("Checkpoint frozen identity mismatch")
            name = bundle_name(manifest)
            if manifest["archive_sequence"] != record.anchor.sequence:
                raise IntegrityError("Bundle archive identity mismatch")
            if data["name"] != name:
                raise IntegrityError("Checkpoint path identity mismatch")
            prepared[record.anchor.sequence] = record
        elif value["kind"] == "checkpoint_sealed":
            data = value["data"]
            sequence = data["prepare_sequence"]
            if sequence not in prepared or sequence in consumed:
                raise IntegrityError("Missing or repeated checkpoint preparation")
            preparation = prepared[sequence]
            prep = preparation.value["data"]
            if data["manifest_sha256"] != prep["manifest_sha256"]:
                raise IntegrityError("Seal does not identify prepared bytes")
            manifest_bytes = next(
                f.content for f in preparation.files.files if f.path == "manifest.json"
            )
            manifest = decode(manifest_bytes)
            previous = sealed[-1].manifest_sha256 if sealed else None
            if (
                manifest["checkpoint_sequence"] != len(sealed) + 1
                or manifest["previous_checkpoint_sha256"] != previous
            ):
                raise IntegrityError("Checkpoint predecessor chain mismatch")
            verify_materialized(root / "checkpoints" / prep["name"], preparation.files)
            sealed.append(
                SealedCheckpoint(
                    prep["name"],
                    data["manifest_sha256"],
                    sequence,
                    record.anchor.sequence,
                    manifest_bytes,
                )
            )
            consumed.add(sequence)
    return tuple(sealed)


def bundle_name(manifest: dict) -> str:
    return (
        f"{manifest['checkpoint_sequence']:04d}-{manifest['checkpoint_id']}-"
        f"{manifest['archive_sequence']:06d}"
    )


@dataclass(frozen=True)
class AccountingInspection:
    checkpoints: tuple[SealedCheckpoint, ...]
    issues: tuple[dict, ...]


def inspect_accounting_chain(
    root: Path,
    records: tuple[Record, ...],
    *,
    attempt_id: str,
    registrations: dict,
    artifacts: dict,
) -> AccountingInspection:
    """Find a provable prefix without repairing damaged or abandoned evidence.

    Only an explicitly failed accounting branch can extend that prefix, and only
    with CP6. Primary progression must continue using verify_checkpoint_chain.
    """
    prepared = {}
    selected = []
    checkpoints = ()
    issues = []
    damaged = False
    accounting_only = False
    for record in records:
        value = record.value
        if value["kind"] == "checkpoint_prepared":
            prepared[record.anchor.sequence] = record
        elif value["kind"] == "accounting_branch":
            data = value["data"]
            if data.get("result") != "simulation_failed" or data.get(
                "verified_prefix"
            ) != [c.seal_sequence for c in checkpoints]:
                issues.append(
                    {
                        "record_sequence": record.anchor.sequence,
                        "reason": "accounting branch predecessor is unprovable",
                    }
                )
                damaged = True
            else:
                damaged = False
                accounting_only = True
        elif value["kind"] == "checkpoint_sealed":
            data = value["data"]
            preparation = prepared.get(data.get("prepare_sequence"))
            try:
                if damaged or preparation is None:
                    raise IntegrityError("Unprovable predecessor or preparation")
                if accounting_only:
                    manifest = validate_bundle(
                        preparation.files, data["manifest_sha256"]
                    )
                    if manifest["checkpoint_id"] != "CP6" or manifest["gate"]:
                        raise IntegrityError(
                            "Accounting cannot resume primary execution"
                        )
                candidate = (*selected, preparation, record)
                verified = verify_checkpoint_chain(
                    root,
                    candidate,
                    attempt_id=attempt_id,
                    registrations=registrations,
                    artifacts=artifacts,
                )
            except (ValueError, OSError, KeyError, TypeError) as exc:
                issues.append(
                    {
                        "record_sequence": record.anchor.sequence,
                        "prepare_sequence": data.get("prepare_sequence"),
                        "recorded_manifest_sha256": data.get("manifest_sha256"),
                        "reason": str(exc),
                    }
                )
                damaged = True
            else:
                selected.extend((preparation, record))
                checkpoints = verified
    return AccountingInspection(checkpoints, tuple(issues))
