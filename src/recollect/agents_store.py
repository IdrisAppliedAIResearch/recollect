"""The durable-state seam for worker-built capabilities.

A sandbox workspace is scrubbed every invocation, so a capability the
implementation agent builds — a calendar, a habit list — cannot keep state
in the sandbox. The harness therefore offers one generic, namespaced,
size-bounded store under the data directory; capabilities address it over
the connection relay and never invent a new endpoint for their state.

Names are advisory, not a sandbox: one relay key and one shared tool
process mean a namespace prevents accidents, not malice (see
``.agent/seam-architecture-plan.md``). Values are schema-versioned files,
written atomically like ``scheduling.ScheduleStore``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = 1
#: A namespace is a small directory, not a database: room for every
#: capability one user might install, each holding bounded entries.
MAX_NAMESPACES = 64
MAX_KEYS_PER_NAMESPACE = 128
MAX_VALUE_BYTES = 16_384

#: Lower-case slug. Dots and slashes are excluded outright, so a namespace
#: can never traverse; keys additionally keep the file name in one piece.
_NAMESPACE_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentStore:
    """``data_dir/agents/<ns>/<key>.json`` with validated, atomic writes."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        # The relay serves put() from its thread pool, and the quota checks
        # are check-then-act on the tree: only under this lock do they hold.
        self._lock = threading.Lock()

    def _namespace(self, namespace: str) -> Path:
        if not _NAMESPACE_RE.fullmatch(namespace):
            raise ValueError(f"invalid namespace {namespace!r}")
        path = self._root / namespace
        # Defense in depth beyond the regex: the resolved path stays inside.
        if path.resolve().parent != self._root.resolve():
            raise ValueError(f"invalid namespace {namespace!r}")
        return path

    def _entry(self, namespace: str, key: str) -> Path:
        if not _KEY_RE.fullmatch(key):
            raise ValueError(f"invalid key {key!r}")
        return self._namespace(namespace) / f"{key}.json"

    def namespaces(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(entry.name for entry in self._root.iterdir()
                      if entry.is_dir())

    def list(self, namespace: str) -> list[dict]:
        """Metadata (key, bytes, updated_at) for every entry, sorted."""
        directory = self._namespace(namespace)
        if not directory.is_dir():
            return []
        found = []
        for path in sorted(directory.glob("*.json")):
            if not path.is_file():
                continue
            found.append({"key": path.stem, "bytes": path.stat().st_size,
                          "updated_at": _read(path)["updated_at"]})
        return found

    def get(self, namespace: str, key: str):
        """The stored value, or ``KeyError`` when the entry is absent."""
        path = self._entry(namespace, key)
        if not path.is_file():
            raise KeyError(f"{namespace}/{key}")
        return _read(path)["data"]

    def put(self, namespace: str, key: str, value) -> dict:
        """Atomically store one JSON value; returns its metadata."""
        path = self._entry(namespace, key)
        try:
            encoded = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValueError("a stored value must be JSON") from error
        if len(encoded.encode("utf-8")) > MAX_VALUE_BYTES:
            raise ValueError(f"a stored value is limited to "
                             f"{MAX_VALUE_BYTES} bytes")
        directory = path.parent
        with self._lock:
            if not path.is_file() and \
                    len(list(directory.glob("*.json"))) >= MAX_KEYS_PER_NAMESPACE:
                raise ValueError(f"at most {MAX_KEYS_PER_NAMESPACE} entries per "
                                 f"namespace")
            if not directory.is_dir() and \
                    len(self.namespaces()) >= MAX_NAMESPACES:
                raise ValueError(f"at most {MAX_NAMESPACES} namespaces")
            directory.mkdir(parents=True, exist_ok=True)
            record = {"schema": SCHEMA, "data": value, "updated_at": _now()}
            data = json.dumps(record, sort_keys=True,
                              ensure_ascii=False).encode("utf-8")
            staging = directory / f".{uuid.uuid4().hex}.json.tmp"
            try:
                staging.write_bytes(data)
                os.replace(staging, path)
            except BaseException:
                staging.unlink(missing_ok=True)
                raise
        return {"key": key, "bytes": len(data), "updated_at": record["updated_at"]}

    def delete(self, namespace: str, key: str) -> None:
        """Remove one entry; ``KeyError`` when absent."""
        path = self._entry(namespace, key)
        if not path.is_file():
            raise KeyError(f"{namespace}/{key}")
        path.unlink()


def _read(path: Path) -> dict:
    """One validated record; malformed files fail as ValueError."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"the stored entry {path.name} could not be read") from error
    if (not isinstance(record, dict) or record.get("schema") != SCHEMA
            or "data" not in record
            or not isinstance(record.get("updated_at"), str)):
        raise ValueError(f"the stored entry {path.name} is malformed")
    return record
