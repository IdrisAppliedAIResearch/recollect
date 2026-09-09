"""Durable task mailboxes and immutable files, separate from episodic memory.

Methods are synchronous and open a short-lived SQLite transaction. The async
coordinator calls them through ``asyncio.to_thread``. No execution transcript is
ever written to the conversation's episode store.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .config import RecollectConfig
from .limits import validate_identifier

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TASK_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
MAX_TASK_RECORD_BYTES = 256 * 1024
MAX_MAILBOX_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 10_000
MAX_TASKS = 128
MAX_ARTIFACTS = 512
MAX_WORKSPACE_ENTRIES = 2_048
STATES = {
    "queued", "running", "blocked", "cancel-requested", "completed",
    "canceled", "interrupted",
}
_MEDIA_TYPES = {
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".json": "application/json",
}
_MUTABLE = {
    "state", "progress", "findings", "sources", "error", "partial", "quiet",
    "backend_session_id", "result", "result_revision", "checkpoint",
}


class TaskStorageFull(ValueError):
    """Retained task data reached a bound; user artifacts were not removed."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value, limit: int = MAX_RECORD_BYTES) -> str:
    result = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(result.encode("utf-8")) > limit:
        raise TaskStorageFull(f"Task record exceeds its {limit}-byte storage limit.")
    return result


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _regular(path: Path) -> os.stat_result:
    info = path.lstat()
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Task files must be regular files without links.")
    return info


def _directory(path: Path) -> None:
    info = path.lstat()
    if _linked(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("Task directories must not be links or reparse points.")


def _relative_name(name: str) -> PurePosixPath:
    parts = PurePosixPath(name)
    if (
        not name or len(name) > 240 or parts.is_absolute()
        or str(parts) != name or any(c in name for c in '\\:<>"|?*')
        or any(ord(c) < 32 for c in name)
    ):
        raise ValueError("Unsafe artifact filename.")
    for part in parts.parts:
        if part in {".", ".."} or part.endswith((".", " ")):
            raise ValueError("Unsafe artifact filename.")
        # Windows treats device names specially even when they have an extension.
        stem = part.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or (
            len(stem) == 4 and stem[:3] in {"COM", "LPT"}
            and stem[-1] in "123456789"
        ):
            raise ValueError("Unsafe artifact filename.")
    return parts


class TaskStore:
    def __init__(self, config: RecollectConfig) -> None:
        self.config = config

    def _root(self, session_id: str) -> Path:
        root = self.config.session_dir(session_id)
        if not root.is_dir():
            raise KeyError(f"No such session: {session_id}")
        _directory(self.config.sessions_dir)
        _directory(root)
        metadata = self.config.session_file(session_id, "session.json")
        _regular(metadata)
        if json.loads(metadata.read_text(encoding="utf-8")).get(
            "session_id"
        ) != session_id:
            raise ValueError("Session metadata does not match its directory.")
        return root

    @contextmanager
    def _db(self, session_id: str):
        root = self._root(session_id)
        path = root / "tasks.sqlite"
        if path.exists() or path.is_symlink():
            _regular(path)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = root / f"tasks.sqlite{suffix}"
            # Another short transaction may finish between these checks.
            with suppress(FileNotFoundError):
                _regular(sidecar)
        connection = sqlite3.connect(path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError("Task storage uses an unsupported schema version.")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            if version == 0:
                for statement in (
                    "CREATE TABLE IF NOT EXISTS tasks ("
                    "task_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, "
                    "record TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS messages ("
                    "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "message_id TEXT UNIQUE NOT NULL, task_id TEXT NOT NULL "
                    "REFERENCES tasks(task_id) ON DELETE CASCADE, "
                    "record TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS artifacts ("
                    "artifact_id TEXT PRIMARY KEY, task_id TEXT NOT NULL "
                    "REFERENCES tasks(task_id) ON DELETE CASCADE, "
                    "name TEXT NOT NULL, version INTEGER NOT NULL, "
                    "record TEXT NOT NULL, UNIQUE(task_id, name, version))",
                ):
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _get(connection, task_id: str) -> dict:
        validate_identifier(task_id)
        row = connection.execute(
            "SELECT record FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No such task: {task_id}")
        task = json.loads(row[0])
        task["artifacts"] = [json.loads(row[0]) for row in connection.execute(
            "SELECT record FROM artifacts WHERE task_id=? ORDER BY name, version",
            (task_id,),
        )]
        return task

    @staticmethod
    def _save(connection, task: dict) -> None:
        record = {key: value for key, value in task.items() if key != "artifacts"}
        record["updated_at"] = _now()
        connection.execute(
            "UPDATE tasks SET record=? WHERE task_id=?",
            (_json(record, MAX_TASK_RECORD_BYTES), task["task_id"]),
        )

    def start(self, session_id: str, request_id: str, objective: str,
              original_message: str, effort: str,
              parent_task_id: str | None = None) -> dict:
        if not request_id or len(request_id) > 256:
            raise ValueError("A bounded originating request ID is required.")
        if not objective.strip() or effort not in {"focused", "deep"}:
            raise ValueError("A task needs an objective and focused or deep effort.")
        with self._db(session_id) as connection:
            row = connection.execute(
                "SELECT task_id FROM tasks WHERE request_id=?", (request_id,),
            ).fetchone()
            if row:
                previous = self._get(connection, row[0])
                if any(previous[key] != value for key, value in (
                    ("objective", objective), ("original_message", original_message),
                    ("effort", effort), ("parent_task_id", parent_task_id),
                )):
                    raise ValueError("Request ID was already used for another task.")
                return previous
            if parent_task_id:
                self._get(connection, parent_task_id)
            if connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= (
                MAX_TASKS
            ):
                raise TaskStorageFull("Conversation task storage is full.")
            task = {
                "task_id": uuid.uuid4().hex, "session_id": session_id,
                "request_id": request_id, "objective": objective,
                "original_message": original_message, "effort": effort,
                "parent_task_id": parent_task_id, "state": "queued",
                "revision": 1, "accepted_revision": 0, "result_revision": None,
                "progress": "", "findings": [], "sources": [], "error": None,
                "partial": False, "quiet": False, "backend_session_id": None,
                "result": "", "checkpoint": {},
                "created_at": _now(), "updated_at": _now(),
            }
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?)",
                (task["task_id"], request_id, _json(task, MAX_TASK_RECORD_BYTES)),
            )
            self._message(connection, task, f"start:{request_id}", "main", "start",
                          {"objective": objective}, 1)
            return self._get(connection, task["task_id"])

    def get(self, session_id: str, task_id: str) -> dict:
        with self._db(session_id) as connection:
            return self._get(connection, task_id)

    def request(self, session_id: str, request_id: str) -> dict | None:
        with self._db(session_id) as connection:
            row = connection.execute(
                "SELECT task_id FROM tasks WHERE request_id=?", (request_id,),
            ).fetchone()
            return self._get(connection, row[0]) if row else None

    def list(self, session_id: str) -> list[dict]:
        with self._db(session_id) as connection:
            return [self._get(connection, row[0]) for row in connection.execute(
                "SELECT task_id FROM tasks ORDER BY rowid"
            ).fetchall()]

    def update(self, session_id: str, task_id: str, **changes) -> dict:
        if set(changes) - _MUTABLE:
            raise ValueError("Unsupported task field update.")
        if "state" in changes and changes["state"] not in STATES:
            raise ValueError("Unsupported task state.")
        for key in ("findings", "sources"):
            if key in changes and (
                not isinstance(changes[key], list)
                or any(not isinstance(item, str) for item in changes[key])
            ):
                raise ValueError(f"Task {key} must be a list of strings.")
        for key in ("partial", "quiet"):
            if key in changes and not isinstance(changes[key], bool):
                raise ValueError(f"Task {key} must be a boolean.")
        with self._db(session_id) as connection:
            task = self._get(connection, task_id)
            if (
                changes.get("result_revision") is not None
                and not 1 <= changes["result_revision"] <= task["revision"]
            ):
                raise ValueError("Result refers to an unknown instruction revision.")
            task.update(changes)
            self._save(connection, task)
            return self._get(connection, task_id)

    @staticmethod
    def _message(connection, task: dict, message_id: str, direction: str,
                 kind: str, payload: dict, revision: int) -> dict:
        if not message_id or len(message_id) > 300:
            raise ValueError("A bounded message ID is required.")
        if direction not in {"main", "subagent", "notification"}:
            raise ValueError("Unknown task message direction.")
        if not kind or len(kind) > 64 or not isinstance(payload, dict):
            raise ValueError("A task message needs a kind and an object payload.")
        record = {
            "message_id": message_id, "task_id": task["task_id"],
            "session_id": task["session_id"], "direction": direction,
            "kind": kind, "revision": revision, "payload": payload,
        }
        row = connection.execute(
            "SELECT seq, record FROM messages WHERE message_id=?", (message_id,),
        ).fetchone()
        if row:
            previous = json.loads(row["record"])
            if any(previous[key] != value for key, value in record.items()):
                raise ValueError("Message ID was already used for another message.")
            return {**previous, "seq": row["seq"]}
        if not 1 <= revision <= task["revision"]:
            raise ValueError("Message refers to an unknown instruction revision.")
        record["created_at"] = _now()
        encoded = _json(record)
        count, size = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(record AS BLOB))), 0) "
            "FROM messages WHERE task_id=?", (task["task_id"],),
        ).fetchone()
        if count >= MAX_MESSAGES or size + len(encoded.encode()) > MAX_MAILBOX_BYTES:
            raise TaskStorageFull("Task mailbox is full; retained work is unchanged.")
        cursor = connection.execute(
            "INSERT INTO messages (message_id, task_id, record) VALUES (?, ?, ?)",
            (message_id, task["task_id"], encoded),
        )
        return {**record, "seq": cursor.lastrowid}

    def message(self, session_id: str, task_id: str, message_id: str,
                direction: str, kind: str, payload: dict,
                revision: int | None = None) -> dict:
        with self._db(session_id) as connection:
            task = self._get(connection, task_id)
            if revision is None:
                previous = connection.execute(
                    "SELECT record FROM messages WHERE message_id=?", (message_id,),
                ).fetchone()
                revision = (
                    json.loads(previous[0])["revision"] if previous
                    else task["revision"]
                )
            return self._message(connection, task, message_id, direction, kind,
                                 payload, revision)

    def messages(self, session_id: str, task_id: str, after: int = 0,
                 direction: str | None = None, limit: int = 1_000) -> list[dict]:
        if after < 0 or not 1 <= limit <= 1_000:
            raise ValueError("Invalid mailbox cursor or page size.")
        with self._db(session_id) as connection:
            self._get(connection, task_id)
            rows = connection.execute(
                "SELECT seq, record FROM messages WHERE task_id=? AND seq>? "
                "ORDER BY seq", (task_id, after),
            )
            result = []
            for row in rows:
                record = json.loads(row["record"])
                if direction is None or record["direction"] == direction:
                    result.append({**record, "seq": row["seq"]})
                    if len(result) == limit:
                        break
            return result

    def all_messages(self, session_id: str, after: int = 0,
                     limit: int = 1_000) -> list[dict]:
        if after < 0 or not 1 <= limit <= 1_000:
            raise ValueError("Invalid mailbox cursor or page size.")
        with self._db(session_id) as connection:
            return [{**json.loads(row["record"]), "seq": row["seq"]}
                    for row in connection.execute(
                        "SELECT seq, record FROM messages WHERE seq>? "
                        "ORDER BY seq LIMIT ?", (after, limit),
                    )]

    def get_message(self, session_id: str, message_id: str) -> dict | None:
        with self._db(session_id) as connection:
            row = connection.execute(
                "SELECT seq, record FROM messages WHERE message_id=?", (message_id,),
            ).fetchone()
            return {**json.loads(row["record"]), "seq": row["seq"]} if row else None

    def recent_messages(self, session_id: str, direction: str, kind: str,
                        limit: int = 3) -> list[dict]:
        if not 1 <= limit <= 200:
            raise ValueError("Invalid recent message count.")
        with self._db(session_id) as connection:
            result = []
            for row in connection.execute(
                "SELECT seq, record FROM messages ORDER BY seq DESC"
            ):
                record = json.loads(row["record"])
                if record["direction"] == direction and record["kind"] == kind:
                    result.append({**record, "seq": row["seq"]})
                    if len(result) == limit:
                        break
            return list(reversed(result))

    def steer(self, session_id: str, task_id: str, message_id: str, text: str,
              reply_to: str | None = None) -> dict:
        if not text.strip():
            raise ValueError("Steering must contain an instruction.")
        with self._db(session_id) as connection:
            task = self._get(connection, task_id)
            payload = {"text": text, "reply_to": reply_to}
            existing = connection.execute(
                "SELECT record FROM messages WHERE message_id=?", (message_id,),
            ).fetchone()
            if existing:
                record = json.loads(existing[0])
                self._message(connection, task, message_id, "main", "steer",
                              payload, record["revision"])
                return task
            if task["state"] in {"completed", "canceled", "interrupted"}:
                raise ValueError("Finished work needs an explicit follow-up task.")
            if reply_to is not None:
                question = connection.execute(
                    "SELECT record FROM messages WHERE message_id=? AND task_id=?",
                    (reply_to, task_id),
                ).fetchone()
                if question is None or json.loads(question[0])["kind"] != "question":
                    raise ValueError("Steering refers to an unknown task question.")
            task["revision"] += 1
            self._save(connection, task)
            self._message(connection, task, message_id, "main", "steer", payload,
                          task["revision"])
            return self._get(connection, task_id)

    def accept_revision(self, session_id: str, task_id: str, revision: int) -> dict:
        with self._db(session_id) as connection:
            task = self._get(connection, task_id)
            if not task["accepted_revision"] <= revision <= task["revision"]:
                raise ValueError("Cannot accept a stale or unknown revision.")
            if revision == task["accepted_revision"]:
                return task
            task["accepted_revision"] = revision
            self._save(connection, task)
            self._message(connection, task, f"accepted:{task_id}:{revision}",
                          "subagent", "accepted", {}, revision)
            return self._get(connection, task_id)

    def notify(self, session_id: str, task_id: str, message_id: str, text: str,
               kind: str = "progress", user_message: str | None = None,
               source_refs: list | None = None, revision: int | None = None) -> dict:
        record = self.message(
            session_id, task_id, message_id, "notification", kind,
            {"text": text, "user_message": user_message,
             "source_refs": source_refs or []},
            revision,
        )
        return self._notification(record)

    @staticmethod
    def _notification(record: dict) -> dict:
        return {**record, **record["payload"],
                "notification_id": record["message_id"]}

    def get_notification(self, session_id: str, notification_id: str) -> dict | None:
        with self._db(session_id) as connection:
            row = connection.execute(
                "SELECT seq, record FROM messages WHERE message_id=?",
                (notification_id,),
            ).fetchone()
            if row is None:
                return None
            record = {**json.loads(row["record"]), "seq": row["seq"]}
            if record["direction"] != "notification":
                return None
            return self._notification(record)

    def notifications(self, session_id: str, after: int = 0,
                      limit: int = 200) -> list[dict]:
        if after < 0 or not 1 <= limit <= 1_000:
            raise ValueError("Invalid notification cursor or page size.")
        with self._db(session_id) as connection:
            result = []
            for row in connection.execute(
                "SELECT seq, record FROM messages WHERE seq>? ORDER BY seq", (after,),
            ):
                record = json.loads(row["record"])
                if record["direction"] == "notification":
                    result.append(self._notification({**record, "seq": row["seq"]}))
                    if len(result) == limit:
                        break
            return result

    def snapshot(self, session_id: str) -> dict:
        with self._db(session_id) as connection:
            tasks = [self._get(connection, row[0]) for row in connection.execute(
                "SELECT task_id FROM tasks ORDER BY rowid"
            ).fetchall()]
            notifications = []
            for row in connection.execute(
                "SELECT seq, record FROM messages ORDER BY seq DESC"
            ):
                record = json.loads(row["record"])
                if record["direction"] == "notification":
                    notifications.append(self._notification({
                        **record, "seq": row["seq"],
                    }))
                    if len(notifications) == 200:
                        break
            cursor = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages"
            ).fetchone()[0]
            return {"tasks": tasks, "notifications": list(reversed(notifications)),
                    "cursor": cursor}

    def recover(self, session_id: str) -> list[dict]:
        """Call at server startup, before admitting any worker."""
        with self._db(session_id) as connection:
            recovered = []
            ids = connection.execute("SELECT task_id FROM tasks").fetchall()
            for row in ids:
                task = self._get(connection, row[0])
                if task["state"] not in {"running", "cancel-requested"}:
                    continue
                task.update(
                    state="interrupted", backend_session_id=None,
                    error="The serving process stopped; saved work can be continued.",
                    partial=bool(
                        task["findings"] or task["result"] or task["artifacts"]
                    ),
                )
                self._save(connection, task)
                recovered.append(self._get(connection, task["task_id"]))
            return recovered

    def _archive(self, session_id: str, task_id: str) -> Path:
        root = self._root(session_id) / "task-artifacts"
        root.mkdir(exist_ok=True)
        _directory(root)
        path = root / validate_identifier(task_id)
        path.mkdir(exist_ok=True)
        _directory(path)
        return path

    def _workspace(self, session_id: str, workspace: Path) -> Path:
        self._root(session_id)
        workspace = Path(workspace).absolute()
        _directory(workspace)
        # The archive must never be copied into or restored within its own tree.
        storage = self.config.sessions_dir.resolve()
        resolved = workspace.resolve()
        if resolved.is_relative_to(storage) or storage.is_relative_to(resolved):
            raise ValueError("Task workspace must be separate from session storage.")
        for parent in workspace.parents:
            _directory(parent)
        return workspace

    @staticmethod
    def _check_parents(workspace: Path, path: Path) -> None:
        _directory(workspace)
        for part in path.relative_to(workspace).parents:
            _directory(workspace / part)
        if not path.resolve().is_relative_to(workspace.resolve()):
            raise ValueError("Artifact path leaves its workspace.")

    @classmethod
    def _read_stable(cls, workspace: Path, path: Path) -> bytes:
        cls._check_parents(workspace, path)
        before = _regular(path)
        if before.st_size > MAX_FILE_BYTES:
            raise TaskStorageFull("Artifact exceeds the 2 MiB per-file limit.")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            data = stream.read(MAX_FILE_BYTES + 1)
            finished = os.fstat(stream.fileno())
        after = _regular(path)
        cls._check_parents(workspace, path)
        signatures = {
            (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
             info.st_nlink)
            for info in (before, opened, finished, after)
        }
        # On Windows lstat and fstat expose different ctime semantics; each
        # still must match its own reading on the other side of the copy.
        if (
            len(signatures) != 1 or len(data) != before.st_size
            or before.st_ctime_ns != after.st_ctime_ns
            or opened.st_ctime_ns != finished.st_ctime_ns
        ):
            raise ValueError("Artifact changed while being saved; retry after writing.")
        if len(data) > MAX_FILE_BYTES:
            raise TaskStorageFull("Artifact exceeds the 2 MiB per-file limit.")
        data.decode("utf-8")
        return data

    def export_workspace(self, session_id: str, task_id: str, workspace: Path,
                         revision: int | None = None) -> list[dict]:
        workspace = self._workspace(session_id, workspace)
        files = []
        entries = 0
        size = 0
        for directory, children, names in os.walk(workspace, followlinks=False):
            for child in children:
                _directory(Path(directory) / child)
            entries += len(children) + len(names)
            if entries > MAX_WORKSPACE_ENTRIES:
                raise TaskStorageFull("Workspace has too many entries to checkpoint.")
            for filename in sorted(names):
                path = Path(directory) / filename
                _regular(path)
                name = path.relative_to(workspace).as_posix()
                _relative_name(name)
                if path.suffix.lower() not in _MEDIA_TYPES:
                    continue
                data = self._read_stable(workspace, path)
                size += len(data)
                if size > MAX_TASK_BYTES:
                    raise TaskStorageFull("Workspace exceeds the 32 MiB task limit.")
                files.append((name, data))
        created = []
        try:
            with self._db(session_id) as connection:
                task = self._get(connection, task_id)
                if revision is None:
                    revision = task["accepted_revision"] or task["revision"]
                if not 1 <= revision <= task["revision"]:
                    raise ValueError("Artifact refers to an unknown revision.")
                archive = self._archive(session_id, task_id)
                total = sum(item["size_bytes"] for item in task["artifacts"])
                count = len(task["artifacts"])
                result = []
                for name, data in files:
                    digest = hashlib.sha256(data).hexdigest()
                    previous = [item for item in task["artifacts"]
                                if item["name"] == name]
                    latest = max(
                        previous, key=lambda item: item["version"], default=None,
                    )
                    if latest and latest["sha256"] == digest:
                        result.append(latest)
                        continue
                    total += len(data)
                    count += 1
                    if count > MAX_ARTIFACTS:
                        raise TaskStorageFull("Retained artifact version limit reached")
                    if total > MAX_TASK_BYTES:
                        raise TaskStorageFull(
                            "Retained artifacts reached the 32 MiB task limit. "
                            "Previous versions remain available."
                        )
                    artifact_id = uuid.uuid4().hex
                    destination = archive / artifact_id
                    with destination.open("xb") as stream:
                        created.append(destination)
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    metadata = {
                        "artifact_id": artifact_id, "task_id": task_id,
                        "session_id": session_id, "name": name, "filename": name,
                        "version": latest["version"] + 1 if latest else 1,
                        "revision": revision, "sha256": digest,
                        "size_bytes": len(data), "created_at": _now(),
                        "media_type": _MEDIA_TYPES[PurePosixPath(name).suffix.lower()],
                    }
                    connection.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?)", (
                        artifact_id, task_id, name, metadata["version"],
                        _json(metadata),
                    ))
                    result.append(metadata)
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        return self.deliver_artifacts(session_id, task_id, result)

    def deliver_artifacts(self, session_id: str, task_id: str,
                          artifacts: list[dict]) -> list[dict]:
        """Copy verified files to the user's configured Downloads, never overwrite.

        Archive durability comes first. A failed desktop copy stays downloadable
        through the API and carries an explicit delivery error for the UI.
        """
        directory = self.config.downloads_dir
        if directory is None:
            return artifacts
        delivered = []
        for artifact in artifacts:
            saved = self.artifact(session_id, task_id, artifact["artifact_id"])
            record = {k: v for k, v in saved.items() if k != "path"}
            if record.get("download_path"):
                delivered.append(record)
                continue
            try:
                directory.mkdir(parents=True, exist_ok=True)
                _directory(directory)
                name = _relative_name(record["filename"]).name
                original = Path(name)
                data = self._read_stable(Path(saved["path"]).parent,
                                         Path(saved["path"]))
                for suffix in range(1_000):
                    filename = name if suffix == 0 else (
                        f"{original.stem} ({suffix + 1}){original.suffix}"
                    )
                    destination = directory / filename
                    try:
                        stream = destination.open("xb")
                    except FileExistsError:
                        continue
                    try:
                        with stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                    except BaseException:
                        destination.unlink(missing_ok=True)
                        raise
                    record["download_path"] = str(destination)
                    record.pop("download_error", None)
                    break
                else:
                    raise ValueError("Too many files with this name in Downloads.")
            except (OSError, ValueError) as error:
                record["download_error"] = str(error)[:500]
            with self._db(session_id) as connection:
                connection.execute(
                    "UPDATE artifacts SET record=? WHERE artifact_id=? AND task_id=?",
                    (_json(record), record["artifact_id"], task_id),
                )
            delivered.append(record)
        return delivered

    def artifact(self, session_id: str, task_id: str, artifact_id: str) -> dict:
        validate_identifier(artifact_id)
        with self._db(session_id) as connection:
            self._get(connection, task_id)
            row = connection.execute(
                "SELECT record FROM artifacts WHERE task_id=? AND artifact_id=?",
                (task_id, artifact_id),
            ).fetchone()
            if row is None:
                raise KeyError("No such artifact in this task.")
            record = json.loads(row[0])
        path = self._archive(session_id, task_id) / artifact_id
        data = self._read_stable(path.parent, path)
        if len(data) != record["size_bytes"] or hashlib.sha256(data).hexdigest() != (
            record["sha256"]
        ):
            raise ValueError("Saved artifact does not match its recorded content.")
        return {**record, "path": str(path.absolute())}

    def restore_workspace(self, session_id: str, task_id: str,
                          workspace: Path) -> list[dict]:
        workspace = self._workspace(session_id, workspace)
        task = self.get(session_id, task_id)
        latest = {}
        for item in task["artifacts"]:
            if item["name"] not in latest or item["version"] > (
                latest[item["name"]]["version"]
            ):
                latest[item["name"]] = item
        staged = []
        try:
            for name, item in latest.items():
                relative = _relative_name(name)
                destination = workspace.joinpath(*relative.parts)
                directory = workspace
                for part in relative.parts[:-1]:
                    directory = directory / part
                    directory.mkdir(exist_ok=True)
                    _directory(directory)
                self._check_parents(workspace, destination)
                if destination.exists() or destination.is_symlink():
                    _regular(destination)
                saved = self.artifact(session_id, task_id, item["artifact_id"])
                saved_path = Path(saved["path"])
                data = self._read_stable(saved_path.parent, saved_path)
                temporary = destination.with_name(f".restore-{uuid.uuid4().hex}")
                staged.append((temporary, destination))
                with temporary.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            for temporary, destination in staged:
                self._check_parents(workspace, destination)
                if destination.exists() or destination.is_symlink():
                    _regular(destination)
                os.replace(temporary, destination)
            return list(latest.values())
        finally:
            for temporary, _ in staged:
                temporary.unlink(missing_ok=True)

    def delete(self, session_id: str, task_id: str) -> None:
        with self._db(session_id) as connection:
            task = self._get(connection, task_id)
            if task["state"] in {"running", "cancel-requested"}:
                raise ValueError("Stop task workers before deleting their saved work.")
            archive = self._root(session_id) / "task-artifacts" / task_id
            paths = []
            if archive.exists():
                _directory(archive.parent)
                _directory(archive)
                for path in archive.iterdir():
                    validate_identifier(path.name)
                    _regular(path)
                    paths.append(path)
            connection.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        for path in paths:
            path.unlink()
        if archive.exists():
            archive.rmdir()

    def reset(self, session_id: str) -> None:
        tasks = self.list(session_id)
        if any(task["state"] in {"running", "cancel-requested"} for task in tasks):
            raise ValueError("Stop task workers before resetting their saved work.")
        for task in tasks:
            self.delete(session_id, task["task_id"])
