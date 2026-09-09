"""Pairing credentials never acquire shared permissions or follow file links."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from recollect.private_files import _open, read_private, replace_private, write_private


@pytest.fixture
def private_root():
    with tempfile.TemporaryDirectory(prefix="recollect-private-files-") as temporary:
        yield Path(temporary)


def test_private_creation_and_atomic_replacement(private_root):
    path = private_root / "pairing.json"
    write_private(path, b"original")
    assert read_private(path) == b"original"
    replace_private(path, b"replacement")
    assert read_private(path) == b"replacement"
    assert list(private_root.iterdir()) == [path]
    with pytest.raises(FileExistsError):
        write_private(path, b"must not overwrite")
    assert read_private(path) == b"replacement"


def test_creation_is_private_before_first_write(private_root):
    path = private_root / "new.key"
    fd = _open(path, create=True)
    try:
        assert os.fstat(fd).st_size == 0
        if os.name == "nt":
            acl = subprocess.run(
                ["icacls.exe", str(path)], text=True, capture_output=True,
                timeout=10, check=True,
            ).stdout
            assert "(I)" not in acl
            assert len([line for line in acl.splitlines() if ":(" in line]) == 1
        else:
            assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        os.write(fd, b"secret")
    finally:
        os.close(fd)
    assert read_private(path) == b"secret"


def test_hard_link_and_directory_are_refused(private_root):
    path = private_root / "secret"
    write_private(path, b"original")
    link = private_root / "link"
    os.link(path, link)
    with pytest.raises(ValueError, match="without links"):
        read_private(link)
    with pytest.raises(ValueError, match="without links"):
        replace_private(link, b"changed")
    assert path.read_bytes() == b"original"
    with pytest.raises((OSError, ValueError)):
        read_private(private_root)


def test_symbolic_links_are_refused_including_dangling_links(private_root):
    target = private_root / "secret"
    write_private(target, b"original")
    link = private_root / "symlink"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Creating a test symlink is unavailable: {error}")
    with pytest.raises((OSError, ValueError)):
        read_private(link)
    link.unlink()
    link.symlink_to(private_root / "missing")
    with pytest.raises((OSError, ValueError)):
        write_private(link, b"must not follow")
    assert not (private_root / "missing").exists()


def test_existing_permissions_are_restricted(private_root):
    path = private_root / "existing-token"
    path.write_bytes(b"test-only")
    if os.name == "nt":
        changed = subprocess.run(
            ["icacls.exe", str(path), "/grant", "*S-1-5-11:(R)"],
            capture_output=True, text=True, timeout=10,
        )
        assert changed.returncode == 0, changed.stderr
    else:
        path.chmod(0o644)
    assert read_private(path) == b"test-only"
    if os.name == "nt":
        acl = subprocess.run(
            ["icacls.exe", str(path)], capture_output=True, text=True,
            timeout=10, check=True,
        ).stdout
        assert "Authenticated Users" not in acl
        assert "S-1-5-11" not in acl
        assert "(I)" not in acl
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
