"""Frozen task scope and byte-level candidate policy owned by the controller.

Snapshots must be complete, quiescent, trusted captures of regular files. This
module never reads a worker filesystem and cannot enforce mounts or detect links.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass


def digest(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def require_digest(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a lowercase SHA-256 digest")


def require_path(path: str) -> None:
    # One portable spelling prevents Windows aliases from defeating Linux scope.
    if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", path):
        raise ValueError("Expected a portable relative file path")
    for part in path.split("/"):
        if part in {"", ".", ".."} or part.endswith("."):
            raise ValueError("Noncanonical path")
        stem = part.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
            r"(?:COM|LPT)[0-9]", stem
        ):
            raise ValueError("Reserved path")


def require_tuple(value: tuple) -> None:
    if type(value) is not tuple:
        raise ValueError("Frozen collections must be tuples")


def unique_paths(paths: tuple[str, ...], *, files: bool = False) -> None:
    require_tuple(paths)
    folded = set()
    prefixes = {}
    for path in paths:
        require_path(path)
        if path.lower() in folded:
            raise ValueError("Duplicate or case-aliased paths")
        folded.add(path.lower())
        for i in range(1, len(path.split("/")) + 1):
            prefix = "/".join(path.split("/")[:i])
            previous = prefixes.setdefault(prefix.lower(), prefix)
            if previous != prefix:
                raise ValueError("Case-aliased path components")
    if files:
        for path in folded:
            if any("/".join(path.split("/")[:i]) in folded
                   for i in range(1, len(path.split("/")))):
                raise ValueError("A file cannot also be a directory")


@dataclass(frozen=True)
class File:
    path: str
    content: bytes

    def __post_init__(self):
        require_path(self.path)
        if type(self.content) is not bytes:
            raise ValueError("File content must be immutable bytes")


@dataclass(frozen=True)
class Snapshot:
    files: tuple[File, ...]

    def __post_init__(self):
        require_tuple(self.files)
        unique_paths(tuple(f.path for f in self.files), files=True)

    @property
    def sha256(self) -> str:
        return digest([
            {"path": f.path, "bytes": len(f.content),
             "sha256": hashlib.sha256(f.content).hexdigest()}
            for f in sorted(self.files, key=lambda f: f.path)
        ])


@dataclass(frozen=True)
class ChangePolicy:
    baseline_sha256: str
    modify: tuple[str, ...] = ()
    create_under: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()

    def __post_init__(self):
        require_digest(self.baseline_sha256)
        for paths in (self.modify, self.create_under, self.delete):
            unique_paths(paths)
        # Identical modify/delete entries are legal; alternate spellings are not.
        unique_paths(tuple(sorted(set(self.modify + self.create_under + self.delete))))

    @property
    def sha256(self) -> str:
        return digest(asdict(self))

    def permits(self, path: str, operation: str) -> bool:
        require_path(path)
        if operation == "modify":
            return path in self.modify
        if operation == "delete":
            return path in self.delete
        if operation == "create":
            return any(path.startswith(root + "/") for root in self.create_under)
        return False


@dataclass(frozen=True)
class Requirement:
    id: str
    acceptance: str
    evidence: str

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip()
                   for v in (self.id, self.acceptance, self.evidence)):
            raise ValueError("Requirements need identity, acceptance and evidence")


@dataclass(frozen=True)
class TaskContract:
    original_request: str
    requirements: tuple[Requirement, ...]
    development_checks: tuple[str, ...]
    policy_sha256: str

    def __post_init__(self):
        require_digest(self.policy_sha256)
        require_tuple(self.requirements)
        require_tuple(self.development_checks)
        ids = tuple(r.id for r in self.requirements)
        if (not isinstance(self.original_request, str)
                or not self.original_request.strip()):
            raise ValueError("The original request is required")
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("Requirements must be nonempty and uniquely identified")
        if (not self.development_checks
                or any(not isinstance(c, str) or not c.strip()
                       for c in self.development_checks)
                or len(set(self.development_checks)) != len(self.development_checks)):
            raise ValueError("Freeze a nonempty, unique development check inventory")

    @property
    def sha256(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class PlannedChange:
    path: str
    operation: str
    requirement_ids: tuple[str, ...]
    reason: str

    def __post_init__(self):
        require_path(self.path)
        require_tuple(self.requirement_ids)
        if self.operation not in {"create", "modify", "delete"}:
            raise ValueError("Unsupported operation")
        if (not self.reason.strip() or not self.requirement_ids
                or any(not isinstance(i, str) or not i.strip()
                       for i in self.requirement_ids)
                or len(set(self.requirement_ids)) != len(self.requirement_ids)):
            raise ValueError("Every change needs a reason and unique requirement IDs")


@dataclass(frozen=True)
class Verification:
    requirement_id: str
    method: str

    def __post_init__(self):
        if not self.requirement_id.strip() or not self.method.strip():
            raise ValueError("Every requirement needs a check or later checkpoint")


@dataclass(frozen=True)
class Plan:
    contract_sha256: str
    changes: tuple[PlannedChange, ...]
    verification: tuple[Verification, ...]

    def __post_init__(self):
        require_digest(self.contract_sha256)
        require_tuple(self.changes)
        require_tuple(self.verification)
        unique_paths(tuple(c.path for c in self.changes))

    @property
    def sha256(self) -> str:
        return digest(asdict(self))

    def validate(self, contract: TaskContract, policy: ChangePolicy) -> None:
        if (self.contract_sha256 != contract.sha256
                or contract.policy_sha256 != policy.sha256):
            raise ValueError("Plan changed the frozen contract or policy")
        ids = {r.id for r in contract.requirements}
        covered = [v.requirement_id for v in self.verification]
        if set(covered) != ids or len(covered) != len(ids):
            raise ValueError("Plan must cover every requirement exactly once")
        if not self.changes:
            raise ValueError("A modification plan needs an executable change")
        for change in self.changes:
            if (not set(change.requirement_ids) <= ids
                    or not policy.permits(change.path, change.operation)):
                raise ValueError("Change is outside the frozen task scope")


def reconstruct_candidate(
    baseline: Snapshot, proposed: Snapshot, policy: ChangePolicy,
    plan: Plan, contract: TaskContract,
) -> Snapshot:
    """Reject the whole forbidden snapshot, then import only its approved delta."""
    plan.validate(contract, policy)
    if baseline.sha256 != policy.baseline_sha256:
        raise ValueError("Baseline drift")
    old = {f.path: f.content for f in baseline.files}
    new = {f.path: f.content for f in proposed.files}
    # Also reject case-only renames, even when delete/create are both allowed.
    unique_paths(tuple(sorted(set(old) | set(new))))
    if not set(policy.modify + policy.delete) <= old.keys():
        raise ValueError("Modify/delete grants must identify existing baseline files")
    if any(root in old or any(root.startswith(p + "/") for p in old)
           for root in policy.create_under):
        raise ValueError("Creation directory collides with a baseline file")
    actual = set()
    result = dict(old)
    for path in sorted(old.keys() | new.keys()):
        if path in old and path in new and old[path] == new[path]:
            continue
        operation = "create" if path not in old else (
            "delete" if path not in new else "modify"
        )
        if not policy.permits(path, operation):
            raise ValueError(f"Forbidden {operation}: {path}")
        actual.add((path, operation))
        if operation == "delete":
            del result[path]
        else:
            result[path] = new[path]
    if actual != {(c.path, c.operation) for c in plan.changes}:
        raise ValueError("Actual changes differ from the reviewed plan")
    return Snapshot(tuple(File(p, content) for p, content in sorted(result.items())))
