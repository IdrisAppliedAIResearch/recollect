"""Write exact snapshot bytes to disk and verify them back, with no extras."""

import os
from pathlib import Path

from .contracts import Snapshot
from .journal import IntegrityError, regular, root_path, sha256, write_new


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
                raise IntegrityError("Unlisted materialized file")
            with path.open("rb") as source:
                data = source.read(len(wanted[relative]) + 1)
            if data != wanted[relative] or sha256(data) != sha256(wanted[relative]):
                raise IntegrityError("Materialized bytes differ from the snapshot")
            observed.add(relative)
    if observed != set(wanted) or observed_dirs != directories:
        raise IntegrityError("Missing or extra materialized paths")
