"""A proven build is committed on its own branch; the previous branch rolls back."""

import shutil
import subprocess
from datetime import UTC, datetime

import pytest

from recollect.selfmod import subagent_tree
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.promotion import PromotionError, promote, rollback_tag

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
NOW = datetime(2026, 9, 17, 12, 30, 5, tzinfo=UTC)
LOCK = subagent_tree.REPOSITORY_FILES[3][1]


def git(root, *args):
    return subprocess.run(("git", *args), cwd=root, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    for _, source in subagent_tree.REPOSITORY_FILES:
        (root / source).parent.mkdir(parents=True, exist_ok=True)
        (root / source).write_bytes(b"original " + source.encode() + b"\n")
    skill = root / subagent_tree.SKILLS_SOURCE / "recollect-reporting" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"report\n")
    git(root, "init", "--quiet", "--initial-branch", "main")
    # Exact bytes across checkouts, whatever the machine's line-ending setting.
    git(root, "config", "core.autocrlf", "false")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "base")
    return root


def built(tree):
    files = {f.path: f.content for f in tree.files}
    files["recollect/engine/mcp_research.py"] = b"TOOLS = ['event']\n"
    files["recollect/engine/subagent_tools/event.py"] = b"def event():\n    pass\n"
    return Snapshot(tuple(File(p, c) for p, c in sorted(files.items())))


def test_build_is_committed_where_the_work_lives_and_a_rebuilds_from_it(repository):
    tree = subagent_tree.baseline(repository)
    candidate = built(tree)
    saved = promote(repository, tree, candidate, "create event", now=NOW)
    assert saved.tag == "selfmod-before-create-event-20260917-123005"
    assert saved.branch == "main"
    # One accumulating line of history: no branch is created.
    assert git(repository, "branch", "--show-current") == "main"
    assert git(repository, "branch", "--list") == "* main"
    assert saved.previous == git(repository, "rev-parse", "HEAD~1")
    assert set(saved.paths) == {"src/recollect/engine/mcp_research.py",
                                "src/recollect/engine/subagent_tools/event.py"}
    assert git(repository, "status", "--porcelain") == ""
    assert f"git reset --hard {saved.tag}" in git(repository, "log", "-1",
                                                  "--format=%B")
    # The next start serves exactly what B served.
    assert subagent_tree.baseline(repository).sha256 == candidate.sha256
    assert subagent_tree.change_policy(candidate).permits(
        "recollect/engine/subagent_tools/event.py", "modify")
    # Rolling back is one command against the tag the build left.
    git(repository, "reset", "--hard", "--quiet", saved.tag)
    assert subagent_tree.baseline(repository).sha256 == tree.sha256


def test_a_second_build_accumulates_on_the_first(repository):
    tree = subagent_tree.baseline(repository)
    first = promote(repository, tree, built(tree), "create event", now=NOW)
    after = subagent_tree.baseline(repository)
    second_tree = {f.path: f.content for f in after.files}
    second_tree["recollect/engine/subagent_tools/invite.py"] = (
        b"def invite():\n    pass\n")
    second = promote(repository, after,
                     Snapshot(tuple(File(p, c) for p, c in sorted(
                         second_tree.items()))), "send invite", now=NOW)
    assert second.previous == first.commit
    assert git(repository, "branch", "--list") == "* main"
    assert git(repository, "log", "--oneline").count("Self-modification") == 2
    # The first build is still there after the second.
    assert (repository / "src" / subagent_tree.TOOLS / "event.py").exists()


def test_a_failed_commit_leaves_the_checkout_exactly_as_it_was(repository,
                                                              monkeypatch):
    tree = subagent_tree.baseline(repository)
    hook = repository / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", newline="\n")
    hook.chmod(0o755)
    with pytest.raises(PromotionError, match="commit"):
        promote(repository, tree, built(tree), "create event", now=NOW)
    assert git(repository, "branch", "--show-current") == "main"
    assert git(repository, "tag", "--list") == ""
    assert git(repository, "status", "--porcelain") == ""
    assert subagent_tree.baseline(repository).sha256 == tree.sha256


def test_nothing_changed_or_no_repository_is_refused(repository, tmp_path):
    tree = subagent_tree.baseline(repository)
    with pytest.raises(PromotionError, match="no files"):
        promote(repository, tree, tree, "x")
    outside = tmp_path / "plain"
    shutil.copytree(repository, outside, ignore=shutil.ignore_patterns(".git"))
    with pytest.raises(PromotionError):
        promote(outside, tree, built(tree), "x")


def test_tag_names_are_safe_git_refs():
    assert rollback_tag("HTTP request!", NOW) == (
        "selfmod-before-http-request-20260917-123005")
    assert rollback_tag(None, NOW) == "selfmod-before-capability-20260917-123005"
    assert subagent_tree.repository_path("skills/a/SKILL.md") == (
        subagent_tree.SKILLS_SOURCE + "/a/SKILL.md")
    assert subagent_tree.repository_path("dependencies.lock") == LOCK
    with pytest.raises(ValueError):
        subagent_tree.repository_path("recollect/__init__.py")


def test_the_repository_lints_what_the_build_wrote_before_it_is_committed(
    repository,
):
    """The offline checks cannot run ruff, so promotion applies its fixes."""
    tree = subagent_tree.baseline(repository)
    seen = []

    def format_files(root, paths):
        seen.append((root, paths))
        (root / paths[0]).write_bytes(b"sorted\n")

    saved = promote(repository, tree, built(tree), "create event", now=NOW,
                    format_files=format_files)
    assert seen and seen[0][0] == repository
    assert set(seen[0][1]) == set(saved.paths)
    # What the formatter changed is what landed in the commit.
    assert git(repository, "status", "--porcelain") == ""
    assert (repository / saved.paths[0]).read_bytes() == b"sorted\n"
