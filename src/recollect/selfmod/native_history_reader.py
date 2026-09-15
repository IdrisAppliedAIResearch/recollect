"""Standalone stdlib reader for pinned OpenCode 1.18.18 committed EventV2 rows.

The CLI is root-only and has no caller-selectable path. ``read_page`` accepts
an explicit path for fixture tests. A trusted launcher must own this script and
its transport; these bytes do not attest runtime containment or process stop.
Large canonical rows use base64 fragments so *every* response fits MAX_PAGE_BYTES.
``offset``/``row_sha256`` continue such a row without advancing ``after``.
"""

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

DATABASE_PATH = Path("/state/data/opencode/opencode.db")
MAX_PAGE_BYTES = 1024 * 1024
MAX_ROW_BYTES = 16 * 1024 * 1024
ROW_KEYS = {"id", "aggregate_id", "seq", "type", "data"}
EVENT_TYPES = {
    "session.created", "session.updated", "session.deleted", "message.updated",
    "message.removed", "message.part.updated", "message.part.removed",
}


class HistoryReadError(ValueError):
    def __init__(self, message, *, raw_prefix=b""):
        super().__init__(message)
        self.raw_prefix = raw_prefix[:MAX_PAGE_BYTES]


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise HistoryReadError("Duplicate JSON key")
        result[key] = value
    return result


def _number(value):
    number = float(value)
    if not math.isfinite(number):
        raise HistoryReadError("Nonfinite JSON number")
    return number


def strict_json(raw):
    def reject(value):
        raise HistoryReadError("Nonfinite JSON constant: " + value)

    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=reject,
                          parse_float=_number)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise HistoryReadError("Malformed native JSON") from exc


def _integer(value, minimum=-1):
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise HistoryReadError("Invalid sequence or offset")


def _sha(value):
    if type(value) is not str or not re.fullmatch("[0-9a-f]{64}", value):
        raise HistoryReadError("Invalid row digest")


def validate_request(session_id, after, through, last_event_sha256,
                     offset=0, row_sha256=None):
    if type(session_id) is not str or not re.fullmatch(
        r"ses_[A-Za-z0-9]+", session_id
    ):
        raise HistoryReadError("Invalid session identity")
    _integer(after)
    if through is not None:
        _integer(through, 0)
        if through < after:
            raise HistoryReadError("Watermark behind requested cursor")
    if last_event_sha256 is not None:
        _sha(last_event_sha256)
        if after < 0:
            raise HistoryReadError("Prior identity without a prior row")
    _integer(offset, 0)
    if offset:
        if offset >= MAX_ROW_BYTES or through is None:
            raise HistoryReadError("Invalid fragment continuation")
        _sha(row_sha256)
    elif row_sha256 is not None:
        raise HistoryReadError("Unexpected fragment identity")


def validate_row(row, session_id, sequence):
    if type(row) is not dict or set(row) != ROW_KEYS:
        raise HistoryReadError("Malformed native event row")
    if (type(row["seq"]) is not int or row["seq"] != sequence
            or row["aggregate_id"] != session_id
            or type(row["id"]) is not str or not row["id"]
            or type(row["data"]) is not str or type(row["type"]) is not str):
        raise HistoryReadError("Foreign or malformed event identity")
    kind = row["type"].removesuffix(".1")
    if kind not in EVENT_TYPES:
        raise HistoryReadError("Unknown durable event type/version")
    if sequence == 0 and kind != "session.created":
        raise HistoryReadError("Missing session creation event")
    if sequence > 0 and kind == "session.created":
        raise HistoryReadError("Duplicate session creation event")
    data = strict_json(row["data"])
    if type(data) is not dict:
        raise HistoryReadError("Native event data must be an object")
    if kind.startswith("session."):
        info = data.get("info")
        if type(info) is not dict or info.get("id") != session_id:
            raise HistoryReadError("Foreign session event")
    elif kind == "message.updated":
        info = data.get("info")
        if type(info) is not dict or info.get("sessionID") != session_id:
            raise HistoryReadError("Foreign message event")
    elif kind == "message.part.updated":
        part = data.get("part")
        if type(part) is not dict or part.get("sessionID") != session_id:
            raise HistoryReadError("Foreign message part event")
    elif data.get("sessionID") != session_id:
        raise HistoryReadError("Foreign removal event")
    # Check routing identities, not arbitrary tool output or user JSON text.
    for routed in (data, data.get("info"), data.get("part")):
        if (type(routed) is dict and "sessionID" in routed
                and routed["sessionID"] != session_id):
            raise HistoryReadError("Conflicting event ownership")
    encoded = canonical(row)
    if len(encoded) > MAX_ROW_BYTES:
        raise HistoryReadError("Native row exceeds 16 MiB")
    return encoded


def _regular(path, *, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or not (stat.S_ISDIR(info.st_mode) if directory
                    else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
        raise HistoryReadError("Native database paths must be regular, not links")
    return info.st_dev, info.st_ino


def _paths(path):
    identities = []
    for parent in reversed(path.parents):
        identities.append(_regular(parent, directory=True))
    identities.append(_regular(path))
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if os.path.lexists(sidecar):
            _regular(sidecar)
    return identities


def _schema(connection):
    for table, columns in (
        ("event", ["id", "aggregate_id", "seq", "type", "data"]),
        ("event_sequence", ["aggregate_id", "seq", "owner_id"]),
    ):
        if connection.execute(
            "SELECT type FROM sqlite_master WHERE name=?", (table,)
        ).fetchall() != [("table",)]:
            raise HistoryReadError("Missing native event table")
        actual = [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]
        if actual != columns:
            raise HistoryReadError("Native event schema mismatch")


def _row(connection, session_id, sequence):
    rows = connection.execute(
        "SELECT id,aggregate_id,seq,type,data FROM event "
        "WHERE aggregate_id=? AND seq=?", (session_id, sequence),
    ).fetchmany(2)
    if len(rows) != 1:
        raise HistoryReadError("Missing or duplicate native sequence")
    row = dict(zip(("id", "aggregate_id", "seq", "type", "data"), rows[0],
                   strict=True))
    try:
        encoded = validate_row(row, session_id, sequence)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        # JSON with ensure_ascii also preserves malformed Unicode for diagnosis.
        prefix = (json.dumps(row, ensure_ascii=True, default=repr)
                  .encode("ascii")[:MAX_PAGE_BYTES])
        raise HistoryReadError(str(exc), raw_prefix=prefix) from exc
    return row, encoded


def read_page(path, session_id, after=-1, through=None, last_event_sha256=None,
              *, offset=0, row_sha256=None):
    """Read one consistent snapshot. A watermark covers only committed events.

    Ownership is the opaque nullable event_sequence.owner_id; the host pins it
    across pages/captures. The creation event establishes session existence even
    when its current projection has been deleted. No SSE delta is synthesized.
    """
    validate_request(session_id, after, through, last_event_sha256,
                     offset, row_sha256)
    path = Path(path).absolute()
    identity = _paths(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        _schema(connection)
        heads = connection.execute(
            "SELECT seq,owner_id FROM event_sequence WHERE aggregate_id=?",
            (session_id,),
        ).fetchmany(2)
        if len(heads) != 1:
            raise HistoryReadError("Missing or duplicate session head")
        head, owner_id = heads[0]
        _integer(head, 0)
        if owner_id is not None and type(owner_id) is not str:
            raise HistoryReadError("Invalid native aggregate ownership")
        watermark = head if through is None else through
        if head < max(after, watermark):
            raise HistoryReadError("Native head behind requested history (rollback)")
        summary = connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT seq),MIN(seq),MAX(seq),"
            "COUNT(DISTINCT id),SUM(typeof(seq)!='integer') "
            "FROM event WHERE aggregate_id=?", (session_id,),
        ).fetchone()
        if summary != (head + 1, head + 1, 0, head, head + 1, 0):
            raise HistoryReadError("Native sequence gap, duplicate or head mismatch")
        # Bound allocation before fetching TEXT, including SQLite blobs in a
        # corrupted fixture. Canonical JSON overhead is checked after decoding.
        if connection.execute(
            "SELECT 1 FROM event WHERE aggregate_id=? AND "
            "(length(CAST(id AS BLOB))+length(CAST(aggregate_id AS BLOB))+"
            "length(CAST(type AS BLOB))+length(CAST(data AS BLOB)))>? LIMIT 1",
            (session_id, MAX_ROW_BYTES),
        ).fetchone():
            raise HistoryReadError("Native row exceeds 16 MiB")
        _row(connection, session_id, 0)
        prior = digest(_row(connection, session_id, after)[1]) if after >= 0 else None
        if last_event_sha256 is not None and prior != last_event_sha256:
            raise HistoryReadError("Prior row identity mismatch")
        page = dict(session_id=session_id, owner_id=owner_id, head=head,
                    watermark=watermark, after=after, complete=after == watermark,
                    prior_row_sha256=prior, last_event_sha256=prior,
                    rows=[], fragment=None)
        if offset and after == watermark:
            raise HistoryReadError("Fragment beyond watermark")
        for sequence in range(after + 1, watermark + 1):
            row, encoded = _row(connection, session_id, sequence)
            row_digest = digest(encoded)
            trial = {**page, "rows": [*page["rows"], row], "after": sequence,
                     "complete": sequence == watermark,
                     "last_event_sha256": row_digest}
            if not offset and len(canonical(trial)) <= MAX_PAGE_BYTES:
                page = trial
                continue
            if page["rows"]:
                break
            if offset and (row_digest != row_sha256 or offset >= len(encoded)):
                raise HistoryReadError("Fragment row identity mismatch")
            # Reserve envelope space, then account for base64 expansion exactly.
            room = (MAX_PAGE_BYTES - len(canonical(page)) - 512) // 4 * 3
            if room <= 0:
                raise HistoryReadError("Native page envelope exceeds byte bound")
            chunk = encoded[offset:offset + room]
            page["fragment"] = dict(
                seq=sequence, offset=offset, total_bytes=len(encoded),
                row_sha256=row_digest, data=base64.b64encode(chunk).decode("ascii"),
            )
            break
        if len(canonical(page)) > MAX_PAGE_BYTES:
            raise HistoryReadError("Native page exceeds 1 MiB")
        if _paths(path) != identity:
            raise HistoryReadError("Native database physical identity changed")
        return page
    except sqlite3.Error as exc:
        raise HistoryReadError("Native SQLite read failed: " + str(exc)) from exc
    finally:
        connection.close()


def _terminal_identity(info):
    return tuple(getattr(info, name) for name in (
        "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid", "st_size",
        "st_mtime_ns", "st_ctime_ns",
    ))


def _terminal_state(path):
    ancestry = _paths(path)
    permissions = [
        (info.st_mode, info.st_uid, info.st_gid)
        for parent in reversed(path.parents) for info in (parent.lstat(),)
    ]
    files = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        item = Path(str(path) + suffix)
        if os.path.lexists(item):
            files[suffix] = _terminal_identity(item.lstat())
    return (ancestry, permissions), files


def _terminal_descriptor_matches(fd, expected):
    actual = _terminal_identity(os.fstat(fd))
    # Windows fixtures expose creation time in lstat but change time in fstat.
    # Linux production keeps the full comparison, including nanosecond ctime.
    if sys.platform == "win32":
        return actual[:-1] == expected[:-1]
    return actual == expected


def read_terminal_page(path, session_id, *, scratch_dir, **request):
    """Read a private copy only after the trusted owner has stopped all writers.

    The caller must maintain namespace quiescence throughout; these metadata
    checks cannot establish that fact. Live readers must keep using read_page.
    SQLite may create WAL sidecars even for mode=ro. Give it private scratch,
    never write access to native state or immutable=1 that could ignore its WAL.
    """
    validate_request(session_id, request.get("after", -1), request.get("through"),
                     request.get("last_event_sha256"), request.get("offset", 0),
                     request.get("row_sha256"))
    path = Path(path).absolute()
    before = _terminal_state(path)
    with tempfile.TemporaryDirectory(prefix="native-history-", dir=scratch_dir) as tmp:
        copied = Path(tmp) / "opencode.db"
        for suffix, expected in before[1].items():
            if suffix == "-shm":
                # This transient lock/index is rebuilt from the copied WAL.
                continue
            flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
            fd = os.open(str(path) + suffix, flags)
            with os.fdopen(fd, "rb") as source:
                if not _terminal_descriptor_matches(source.fileno(), expected):
                    raise HistoryReadError("Terminal database identity changed")
                with open(str(copied) + suffix, "xb") as target:
                    while chunk := source.read(MAX_PAGE_BYTES):
                        target.write(chunk)
                if not _terminal_descriptor_matches(source.fileno(), expected):
                    raise HistoryReadError("Terminal database identity changed")
        if _terminal_state(path) != before:
            raise HistoryReadError("Terminal database identity changed")
        result = read_page(copied, session_id, **request)
        if _terminal_state(path) != before:
            raise HistoryReadError("Terminal database identity changed")
        return result


def main():
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise SystemExit("Native history reader must run as root")
    raw = sys.stdin.buffer.read(MAX_PAGE_BYTES + 1)
    try:
        if len(raw) > MAX_PAGE_BYTES:
            raise HistoryReadError("Reader request exceeds byte bound")
        request = strict_json(raw)
        if type(request) is not dict or not {"session_id", "after"} <= request.keys():
            raise HistoryReadError("Malformed reader request")
        if request.keys() - {"session_id", "after", "through", "last_event_sha256",
                             "offset", "row_sha256"}:
            raise HistoryReadError("Unknown reader request field")
        result = read_page(DATABASE_PATH, **request)
    except (ValueError, OSError, TypeError) as exc:
        prefix = getattr(exc, "raw_prefix", b"") or raw
        # Failed reads obey the same wire bound, including base64 and envelope.
        room = (MAX_PAGE_BYTES - 4096) // 4 * 3
        sys.stdout.buffer.write(canonical({
            "error": str(exc)[:1024],
            "raw_prefix": base64.b64encode(prefix[:room]).decode("ascii"),
            "raw_prefix_truncated": len(prefix) > room,
        }))
        raise SystemExit(1) from exc
    sys.stdout.buffer.write(canonical(result))


if __name__ == "__main__":
    main()
