"""Exact journal records as a long-evidence sidecar for checkpoint references.

The caller must bind the expected anchor outside this sidecar and keep both
roots outside worker mounts. Copy only a stopped/quiescent source. These blocking
operations belong off the event loop. They impose no aggregate work/length cap.
Journal.append binds each copied record to the head and reads back only that
record, so import cost is linear in copied records and bytes; the completed
sidecar is still fully re-verified once before its completion marker.

Controller integration must bind the relative sidecar location and exact anchor
in CP2/CP6, then exhaust iter_segments on verification. Do not flatten its yields
into one Snapshot. This module does not certify native capture or task success.
"""

from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .journal import (
    Anchor,
    IntegrityError,
    Journal,
    Record,
    encode,
    iter_archive,
    regular,
    require_anchor,
    root_path,
    validate_record,
    write_new,
)


def _completion(expected: Anchor | None) -> bytes:
    require_anchor(expected)
    return encode({"version": 1, "head": asdict(expected) if expected else None})


def iter_segments(root: Path, expected: Anchor | None) -> Iterator[Record]:
    """Verify the completion marker and exact archive end; exhaust to trust it."""
    receipt = _completion(expected)
    path = root_path(root) / "complete.json"
    try:
        regular(path)
        with path.open("rb") as source:
            observed = source.read(len(receipt) + 1)
    except FileNotFoundError as exc:
        raise IntegrityError("Segment copy is incomplete") from exc
    if observed != receipt:
        raise IntegrityError("Segment completion anchor mismatch or incomplete copy")
    with closing(iter_archive(root, expected)) as records:
        yield from records


def import_segments(
    records: Iterable[Record], destination: Path, expected: Anchor | None,
) -> Anchor | None:
    """Copy a full exact chain into a NEW Journal, one bounded record at a time.

    Reuses the original bodies, paths and hashes, without adding wrapper files to
    any record's budget. Input may be streamed from another transport; every
    record is independently checked. Caller owns/closes an input iterator when
    aborting. Failed or cancelled imports retain diagnostic bytes. No valid
    completion marker is published before input exhaustion and readback.
    Never reuse a destination for a retry, even after a failed import.
    """
    receipt = _completion(expected)
    with Journal.create(destination) as journal:
        previous = None
        for record in records:
            validate_record(record, previous)
            if expected is None or record.anchor.sequence > expected.sequence:
                raise IntegrityError("Journal head mismatch: unexpected records")
            value = record.value
            copied = journal.append(value["kind"], value["data"], record.files)
            if copied.anchor != record.anchor or copied.body != record.body:
                raise IntegrityError("Segment copy changed a record")
            previous = record.anchor
        if previous != expected:
            raise IntegrityError("Journal head mismatch: incomplete segment copy")
        for _ in journal.iter_verify():
            pass
        # Publication comes only after input exhaustion and independent readback.
        write_new(journal.root / "complete.json", receipt)
    return expected


def copy_archive(
    source: Path, destination: Path, expected: Anchor | None,
) -> Anchor | None:
    """Import an independently verified, exact-end archive as a complete sidecar."""
    with closing(iter_archive(source, expected)) as records:
        return import_segments(records, destination, expected)
