"""Exact file trees, the modifier's change scope and canonical encoding."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


class IntegrityError(ValueError):
    pass


def encode(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def unique_paths(paths: tuple[str, ...], *, files: bool = False) -> None:
    if type(paths) is not tuple:
        raise ValueError("Frozen collections must be tuples")
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
        unique_paths(tuple(f.path for f in self.files), files=True)

    @property
    def sha256(self) -> str:
        return digest([
            {"path": f.path, "bytes": len(f.content),
             "sha256": hashlib.sha256(f.content).hexdigest()}
            for f in sorted(self.files, key=lambda f: f.path)
        ])


def write_tree(root: Path, files: Snapshot) -> Path:
    """Write a snapshot into a new directory."""
    root = Path(root)
    root.mkdir(parents=True)
    for file in files.files:
        target = root.joinpath(*file.path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.content)
    return root


@dataclass(frozen=True)
class ChangePolicy:
    baseline_sha256: str
    modify: tuple[str, ...] = ()
    create_under: tuple[str, ...] = ()

    def __post_init__(self):
        require_digest(self.baseline_sha256)
        for paths in (self.modify, self.create_under):
            unique_paths(paths)

    @property
    def sha256(self) -> str:
        return digest(asdict(self))

    def permits(self, path: str, operation: str) -> bool:
        require_path(path)
        if operation == "modify":
            return path in self.modify
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
