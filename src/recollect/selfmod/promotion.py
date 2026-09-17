"""Save a proven build into the repository, on the branch it was built from.

B's tree is written back to the files it came from and committed where the work
already lives, so each build accumulates on one line of history instead of
forking a branch per feature. The commit before it is tagged, so rolling back is
one command and a restart.
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
    commit: str
    #: Tag on the commit before the build: the state to roll back to.
    tag: str
    previous: str
    branch: str
    paths: tuple[str, ...]


def _format(repository, paths):
    """Apply the repository's own mechanical lint fixes to what the build wrote.

    The offline checks cannot run the repository's linter, so a candidate that
    passes them can still fail the gate on import order alone.
    """
    try:
        subprocess.run(("uv", "run", "--no-sync", "ruff", "check", "--fix",
                        "--quiet", "--", *paths), cwd=repository,
                       capture_output=True, text=True, creationflags=NO_WINDOW,
                       check=False, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return  # the build is saved either way; the gate reports the rest


def _git(repository, *args):
    result = subprocess.run(("git", *args), cwd=repository, capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=NO_WINDOW, check=False)
    if result.returncode:
        raise PromotionError(f"git {args[0]} failed: "
                             + (result.stderr or result.stdout).strip()[:512])
    return result.stdout.strip()


def rollback_tag(feature, now=None):
    slug = re.sub(r"[^a-z0-9]+", "-", str(feature or "capability").lower()).strip("-")
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"selfmod-before-{slug[:40] or 'capability'}-{stamp}"


def promote(repository, baseline, candidate, feature, *, now=None,
            format_files=None):
    """Commit B's changed files on the branch the checkout is already on."""
    repository = Path(repository)
    before = {f.path: f.content for f in baseline.files}
    changed = [f for f in candidate.files
               if f.path not in PACKAGE_MARKERS and before.get(f.path) != f.content]
    if not changed:
        raise PromotionError("The build changed no files")
    top = Path(_git(repository, "rev-parse", "--show-toplevel")).resolve()
    if top != repository.resolve():
        raise PromotionError(f"{repository} is not the root of a git repository")
    previous = _git(repository, "rev-parse", "HEAD")
    branch = _git(repository, "rev-parse", "--abbrev-ref", "HEAD")
    tag = rollback_tag(feature, now)
    paths = tuple(repository_path(f.path) for f in changed)
    # The state to return to, named before anything is written.
    _git(repository, "tag", tag, previous)
    try:
        for file, path in zip(changed, paths, strict=True):
            target = repository / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file.content)
        (format_files or _format)(repository, paths)
        _git(repository, "add", "--", *paths)
        _git(repository, "commit", "--quiet", "-m",
             f"Self-modification: add the {feature or 'new'} capability",
             "-m", f"Built on {previous[:12]}. Roll back with: "
                   f"git reset --hard {tag}",
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
        _git(repository, "tag", "--delete", tag)
        raise
    return Promotion(_git(repository, "rev-parse", "HEAD"), tag, previous, branch,
                     paths)
