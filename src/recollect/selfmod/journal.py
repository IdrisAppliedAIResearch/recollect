"""Append-only local archive with independent readback and process ownership.

The root must be on a trusted local filesystem, outside every worker mount.
Flush requests are not a guarantee against hardware that does not honor them.
All methods block: integration must run them off the serving event loop.
"""

import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .contracts import File, Snapshot

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 128 * 1024 * 1024
MAX_FILES = 4096
EMPTY_SNAPSHOT = Snapshot(())


class IntegrityError(ValueError):
    pass


def encode(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode(data: bytes) -> dict:
    try:
        value = json.loads(data)
        if not isinstance(value, dict) or encode(value) != data:
            raise ValueError("Noncanonical JSON")
        return value
    except (ValueError, TypeError, UnicodeError) as exc:
        raise IntegrityError("Invalid canonical JSON record") from exc


def inventory(snapshot: Snapshot) -> list[dict]:
    if (
        len(snapshot.files) > MAX_FILES
        or sum(len(f.content) for f in snapshot.files) > MAX_RECORD_BYTES
        or any(len(f.content) > MAX_FILE_BYTES for f in snapshot.files)
    ):
        raise IntegrityError("Evidence exceeds frozen archive limits")
    return [
        dict(path=f.path, bytes=len(f.content), sha256=sha256(f.content))
        for f in sorted(snapshot.files, key=lambda f: f.path)
    ]


def regular(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        or not (
            stat.S_ISDIR(info.st_mode)
            if directory
            else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        )
    ):
        raise IntegrityError("Evidence paths must be regular and unlinked")
    return info


def root_path(value: Path) -> Path:
    path = value.absolute()
    for parent in reversed(path.parents):
        regular(parent, directory=True)
    regular(path, directory=True)
    return path


def write_new(path: Path, data: bytes) -> None:
    regular(path.parent, directory=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


@dataclass(frozen=True)
class Anchor:
    sequence: int
    sha256: str


@dataclass(frozen=True)
class Record:
    anchor: Anchor
    body: bytes
    files: Snapshot

    @property
    def value(self) -> dict:
        return decode(self.body)


_SCHEMA = (
    "CREATE TABLE records (seq INTEGER PRIMARY KEY, body BLOB NOT NULL, "
    "sha TEXT UNIQUE NOT NULL)",
    "CREATE TABLE files (seq INTEGER NOT NULL REFERENCES records(seq) "
    "DEFERRABLE INITIALLY DEFERRED, path TEXT NOT NULL, content BLOB NOT NULL, "
    "PRIMARY KEY(seq,path))",
    "CREATE TRIGGER records_insert BEFORE INSERT ON records BEGIN "
    "SELECT CASE WHEN NEW.seq != (SELECT COALESCE(MAX(seq),0)+1 FROM records) "
    "THEN RAISE(ABORT,'records must append') END; END",
    "CREATE TRIGGER files_insert BEFORE INSERT ON files BEGIN "
    "SELECT CASE WHEN EXISTS(SELECT 1 FROM records WHERE seq=NEW.seq) "
    "OR EXISTS(SELECT 1 FROM files WHERE seq=NEW.seq AND path=NEW.path) "
    "THEN RAISE(ABORT,'archived files are immutable') END; END",
    *(
        f"CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'archive is append only'); END"
        for table in ("records", "files")
        for action in ("UPDATE", "DELETE")
    ),
)


def _schema_rows(connection):
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()


@lru_cache(maxsize=1)
def _expected_schema():
    connection = sqlite3.connect(":memory:")
    try:
        for sql in _SCHEMA:
            connection.execute(sql)
        return _schema_rows(connection)
    finally:
        connection.close()


def read_archive(root: Path, expected: Anchor | None) -> tuple[Record, ...]:
    """Verify a consistent snapshot against a head held outside the database."""
    records = inspect_archive(root)
    actual = records[-1].anchor if records else None
    if actual != expected:
        raise IntegrityError("Journal head mismatch: truncation or unexpected records")
    return records


def inspect_archive(root: Path) -> tuple[Record, ...]:
    """Unanchored inspection for recovery only; a valid prefix is not a trusted head."""
    path = root_path(root) / "journal.sqlite"
    regular(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.execute("BEGIN")
        if (
            connection.execute("PRAGMA user_version").fetchone()[0] != 1
            or connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete"
            or _schema_rows(connection) != _expected_schema()
        ):
            raise IntegrityError("Journal schema/settings mismatch")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise IntegrityError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise IntegrityError("Orphaned evidence")
        result = []
        previous = None
        for seq, body, sha in connection.execute(
            "SELECT seq,body,sha FROM records ORDER BY seq"
        ):
            if type(body) is not bytes or len(body) > MAX_FILE_BYTES:
                raise IntegrityError("Invalid journal body")
            value = decode(body)
            if (
                set(value)
                != {"version", "sequence", "previous", "kind", "data", "files"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or type(value["sequence"]) is not int
                or seq != len(result) + 1
                or value["sequence"] != seq
                or value["previous"] != previous
                or sha256(body) != sha
                or not isinstance(value["kind"], str)
                or not isinstance(value["data"], dict)
            ):
                raise IntegrityError("Invalid journal chain")
            try:
                snapshot = Snapshot(
                    tuple(
                        File(p, b)
                        for p, b in connection.execute(
                            "SELECT path,content FROM files WHERE seq=? ORDER BY path",
                            (seq,),
                        )
                    )
                )
                if encode(inventory(snapshot)) != encode(value["files"]):
                    raise IntegrityError("Archived file inventory/hash mismatch")
            except (TypeError, ValueError) as exc:
                raise IntegrityError("Invalid archived files") from exc
            result.append(Record(Anchor(seq, sha), body, snapshot))
            previous = sha
        return tuple(result)
    finally:
        connection.close()


@contextlib.contextmanager
def _claim_owner(path: Path):
    regular(path)
    with path.open("r+b") as owner:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield owner


class Journal:
    """One process-lifetime owner; failed writes poison this instance permanently."""

    @classmethod
    def create(cls, root: Path, *, fault: Callable[[str], None] = lambda _: None):
        # Never reopen or reuse an attempt directory for primary execution.
        root.parent.mkdir(parents=True, exist_ok=True)
        root_path(root.parent)
        root.mkdir(mode=0o700)
        write_new(root / "owner.lock", b"1")
        journal = cls(root, fault=fault)
        try:
            write_new(root / "journal.sqlite", b"")
            with journal._connection(create=True) as connection:
                for sql in _SCHEMA:
                    connection.execute(sql)
                connection.execute("PRAGMA user_version=1")
                connection.commit()
            journal._remember_identity()
            read_archive(root, None)
            return journal
        except BaseException:
            journal.close()
            raise

    @classmethod
    def recover(cls, root: Path):
        """Acquire a stopped archive for accounting, preserving hot-journal bytes."""
        journal = cls(root, recovery_only=True)
        try:
            capture = journal.root / ("recovery-" + uuid.uuid4().hex)
            capture.mkdir(mode=0o700)
            for name in ("journal.sqlite", "journal.sqlite-journal"):
                source = journal.root / name
                if source.exists():
                    regular(source)
                    write_new(capture / name, source.read_bytes())
            # SQLite may need a writable open to roll back a hot transaction.
            with journal._connection() as connection:
                connection.execute("SELECT COUNT(*) FROM records").fetchone()
            journal._remember_identity()
            records = inspect_archive(root)
            journal._head = records[-1].anchor if records else None
            return journal
        except BaseException:
            journal.close()
            raise

    def __init__(self, root: Path, *, fault=lambda _: None, recovery_only=False):
        self.root = root_path(root)
        self.recovery_only = recovery_only
        self.fault = fault
        self.poisoned = False
        self._head = None
        self._identity = None
        self._owner = None
        self._resources = contextlib.ExitStack()
        try:
            self._owner = self._resources.enter_context(
                _claim_owner(self.root / "owner.lock")
            )
        except BaseException:
            self._resources.close()
            raise

    @property
    def head(self) -> Anchor | None:
        return self._head

    def _identities(self):
        paths = (
            (self.root, True),
            (self.root / "journal.sqlite", False),
            (self.root / "owner.lock", False),
        )
        return tuple(
            (s.st_dev, s.st_ino) for p, d in paths for s in (regular(p, directory=d),)
        )

    def _remember_identity(self):
        self._identity = self._identities()

    def verify(self) -> tuple[Record, ...]:
        if self._owner is None or self.poisoned:
            raise IntegrityError("Journal owner is closed or poisoned")
        try:
            if self._identities() != self._identity:
                raise IntegrityError("Archive physical identity changed")
            return read_archive(self.root, self._head)
        except BaseException:
            self.poisoned = True
            raise

    @contextlib.contextmanager
    def _connection(self, *, create=False):
        path = self.root / "journal.sqlite"
        regular(path)
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)
        try:
            if create:
                connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=EXTRA")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA recursive_triggers=ON")
            values = tuple(
                connection.execute(f"PRAGMA {name}").fetchone()[0]
                for name in (
                    "journal_mode",
                    "synchronous",
                    "foreign_keys",
                    "recursive_triggers",
                    "locking_mode",
                )
            )
            if values != ("delete", 3, 1, 1, "normal"):
                raise IntegrityError("Required SQLite settings are unavailable")
            yield connection
        finally:
            connection.close()

    def append(self, kind: str, data: dict, files: Snapshot = EMPTY_SNAPSHOT) -> Record:
        try:
            self.verify()
            seq = self._head.sequence + 1 if self._head else 1
            body = encode(
                dict(
                    version=1,
                    sequence=seq,
                    previous=self._head.sha256 if self._head else None,
                    kind=kind,
                    data=data,
                    files=inventory(files),
                )
            )
            if len(body) > MAX_FILE_BYTES:
                raise IntegrityError("Journal body exceeds the frozen limit")
            head = Anchor(seq, sha256(body))
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for file in files.files:
                    connection.execute(
                        "INSERT INTO files VALUES (?,?,?)",
                        (seq, file.path, file.content),
                    )
                connection.execute(
                    "INSERT INTO records VALUES (?,?,?)", (seq, body, head.sha256)
                )
                self.fault("journal.before_commit:" + kind)
                connection.commit()
                self.fault("journal.after_commit:" + kind)
            self.fault("journal.before_readback:" + kind)
            records = read_archive(self.root, head)
            self.fault("journal.after_readback:" + kind)
            self._head = head
            return records[-1]
        except BaseException:
            self.poisoned = True
            raise

    def close(self):
        if self._owner is not None:
            self._resources.close()
            self._owner = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
