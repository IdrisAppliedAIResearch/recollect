"""A paused build: the implementation stopped on a step only a human can take.

The record on disk is the build's resumable state: the captured tree, the
approved plan, the pending step and the frozen contract. A restarted process
loads it, re-requests the step (the user may still be away), and resumes in a
fresh session from the captured tree. It lives in the self-modification root,
not in the sandbox workspace, so it survives the workspace scrub on startup.
"""

import base64
import contextlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .contracts import File, Snapshot
from .tests_first import FrozenTests, Requirement

SCHEMA = 1


@dataclass(frozen=True)
class PausedBuild:
    """Everything needed to resume one attempt that paused on a human step."""

    session_id: str
    task_id: str
    request: str
    gap: dict
    attempt: int
    feedback: tuple
    plan: dict
    step: dict
    tree: Snapshot
    tests: FrozenTests
    connections: str
    baseline_sha256: str


def _file_out(file):
    return [file.path, base64.b64encode(file.content).decode()]


def _file_in(item):
    return File(item[0], base64.b64decode(item[1]))


def to_record(pause):
    return {
        "schema": SCHEMA,
        "session_id": pause.session_id,
        "task_id": pause.task_id,
        "request": pause.request,
        "gap": pause.gap,
        "attempt": pause.attempt,
        "feedback": list(pause.feedback),
        "plan": pause.plan,
        "step": pause.step,
        "tree": [_file_out(f) for f in pause.tree.files],
        "tests": {
            "interface": pause.tests.interface,
            "requirements": [asdict(r) for r in pause.tests.requirements],
            "checks": [_file_out(f) for f in pause.tests.checks],
            "unverified": list(pause.tests.unverified),
        },
        "connections": pause.connections,
        "baseline_sha256": pause.baseline_sha256,
    }


def from_record(record):
    tests = FrozenTests(
        interface=record["tests"]["interface"],
        requirements=tuple(Requirement(**r)
                           for r in record["tests"]["requirements"]),
        checks=tuple(_file_in(f) for f in record["tests"]["checks"]),
        unverified=tuple(record["tests"]["unverified"]),
    )
    return PausedBuild(
        session_id=record["session_id"],
        task_id=record["task_id"],
        request=record["request"],
        gap=record["gap"],
        attempt=record["attempt"],
        feedback=tuple(record["feedback"]),
        plan=record["plan"],
        step=record["step"],
        tree=Snapshot(tuple(_file_in(f) for f in record["tree"])),
        tests=tests,
        connections=record["connections"],
        baseline_sha256=record["baseline_sha256"],
    )


def path_for(root):
    return Path(root) / "paused_build.json"


def save(root, pause):
    """Write the record atomically; a half-written file is never loadable."""
    target = path_for(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(to_record(pause), sort_keys=True, ensure_ascii=False).encode(
        "utf-8")
    staging = target.with_suffix(".json.tmp")
    staging.write_bytes(data)
    staging.replace(target)


def load(root):
    """The paused build under ``root``, or None when the file is absent."""
    target = path_for(root)
    if not target.exists():
        return None
    record = json.loads(target.read_bytes().decode("utf-8"))
    if record.get("schema") != SCHEMA:
        raise ValueError("Unrecognized paused-build record")
    return from_record(record)


def clear(root):
    """Drop a finished or abandoned build's record."""
    with contextlib.suppress(FileNotFoundError):
        path_for(root).unlink()
