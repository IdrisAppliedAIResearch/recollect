"""Portable file trees and the modifier's change scope."""

import pytest

from recollect.selfmod.contracts import ChangePolicy, File, Snapshot, write_tree


@pytest.mark.parametrize("path", [
    "../runner.py", "/root.py", "a/../b", "a//b", "a/./b", "a/", "a\\b",
    "C:/a", "file:stream", "a. /b", "a./b", "NUL.txt", "x/COM1.py", "CON/b",
    "LPT9", "a\x00b", "café.py", "a b.py",
])
def test_reject_nonportable_or_aliased_paths(path):
    with pytest.raises(ValueError):
        File(path, b"")


@pytest.mark.parametrize("paths", [
    ("a.py", "a.py"), ("a.py", "A.py"), ("a", "a/b"), ("a", "A/b"),
    ("Tools/a.py", "tools/b.py"),
])
def test_reject_snapshot_path_collisions(paths):
    with pytest.raises(ValueError):
        Snapshot(tuple(File(path, b"") for path in paths))


def test_snapshot_digest_ignores_order_and_tracks_bytes(tmp_path):
    tree = Snapshot((File("a.py", b"1"), File("b/c.py", b"2")))
    assert tree.sha256 == Snapshot(tuple(reversed(tree.files))).sha256
    assert tree.sha256 != Snapshot((File("a.py", b"1"), File("b/c.py", b"3"))).sha256
    write_tree(tmp_path / "tree", tree)
    assert (tmp_path / "tree" / "b" / "c.py").read_bytes() == b"2"


def test_policy_grants_exact_modifications_and_recursive_creation():
    policy = ChangePolicy("0" * 64, modify=("runner.py",),
                          create_under=("extensions",))
    assert policy.permits("runner.py", "modify")
    assert not policy.permits("other.py", "modify")
    assert policy.permits("extensions/nested/new.py", "create")
    assert not policy.permits("extensions-other/new.py", "create")
    assert not policy.permits("extensions", "create")
    assert not policy.permits("runner.py", "delete")
    assert not policy.permits("extensions/new.py", "rename")
