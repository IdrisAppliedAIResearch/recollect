"""Save a proven build into the repository as its own git branch.

B's tree is written back to the files it came from and committed on a new
branch, so the next start builds A from it. The previous branch is untouched:
rolling back is ``git switch <previous>`` and a restart.
"""

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .subagent_tree import PACKAGE_MARKERS, repository_path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class PromotionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Promotion:
    branch: str
    previous: str
    commit: str
    paths: tuple[str, ...]


def _git(repository, *args):
    result = subprocess.run(("git", *args), cwd=repository, capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=NO_WINDOW, check=False)
    if result.returncode:
        raise PromotionError(f"git {args[0]} failed: "
                             + (result.stderr or result.stdout).strip()[:512])
    return result.stdout.strip()


def branch_name(feature, now=None):
    slug = re.sub(r"[^a-z0-9]+", "-", str(feature or "capability").lower()).strip("-")
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"selfmod/{slug[:40] or 'capability'}-{stamp}"


def promote(repository, baseline, candidate, feature, *, now=None):
    """Commit B's changed files on a new branch checked out in ``repository``."""
    repository = Path(repository)
    before = {f.path: f.content for f in baseline.files}
    changed = [f for f in candidate.files
               if f.path not in PACKAGE_MARKERS and before.get(f.path) != f.content]
    if not changed:
        raise PromotionError("The build changed no files")
    top = Path(_git(repository, "rev-parse", "--show-toplevel")).resolve()
    if top != repository.resolve():
        raise PromotionError(f"{repository} is not the root of a git repository")
    previous = _git(repository, "rev-parse", "--abbrev-ref", "HEAD")
    back = ("switch", previous)
    if previous == "HEAD":
        previous = _git(repository, "rev-parse", "HEAD")
        back = ("switch", "--detach", previous)
    branch = branch_name(feature, now)
    paths = tuple(repository_path(f.path) for f in changed)
    _git(repository, "switch", "--create", branch)
    try:
        for file, path in zip(changed, paths, strict=True):
            target = repository / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file.content)
        _git(repository, "add", "--", *paths)
        _git(repository, "commit", "--quiet", "-m",
             f"Self-modification: add the {feature or 'new'} capability",
             "-m", f"Built from {previous}. Roll back with: git switch {previous}",
             "--", *paths)
    except PromotionError:
        # Put the checkout back exactly as it was, so A never serves a half save.
        _git(repository, "reset", "--quiet", "--", *paths)
        for file, path in zip(changed, paths, strict=True):
            target = repository / path
            if file.path in before:
                target.write_bytes(before[file.path])
            else:
                target.unlink(missing_ok=True)
        _git(repository, *back)
        _git(repository, "branch", "--delete", "--force", branch)
        raise
    return Promotion(branch, previous, _git(repository, "rev-parse", "HEAD"), paths)
