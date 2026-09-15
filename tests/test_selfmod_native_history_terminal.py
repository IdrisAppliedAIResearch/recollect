"""Terminal private-copy fixtures; process quiescence is the collector's duty."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from recollect.selfmod import native_history_reader as reader
from tests import test_selfmod_native_history as history_fixtures
from tests.test_selfmod_native_history import SESSION, event, insert
from tests.test_selfmod_native_history import db as db


@pytest.mark.parametrize("failed_setup", [False, True])
def test_history_fixture_closes_connection(tmp_path, monkeypatch, failed_setup):
    connect = sqlite3.connect
    opened = []

    def tracked(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    def fail(*args):
        raise RuntimeError("fixture setup failed")

    monkeypatch.setattr(history_fixtures.sqlite3, "connect", tracked)
    if failed_setup:
        monkeypatch.setattr(history_fixtures, "insert", fail)
        with pytest.raises(RuntimeError, match="fixture setup failed"):
            db.__wrapped__(tmp_path)
    else:
        db.__wrapped__(tmp_path)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


@pytest.mark.parametrize("retained_wal", [False, True])
def test_terminal_copy_reads_exact_history_without_changing_source(db, retained_wal):
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        insert(connection, event(1))
        connection.execute("UPDATE event_sequence SET seq=1")
        connection.commit()
        if not retained_wal:
            connection.close()
        assert Path(str(db) + "-wal").exists() is retained_wal
        before = reader._terminal_state(db)
        raw = {suffix: Path(str(db) + suffix).read_bytes() for suffix in before[1]}
        result = reader.read_terminal_page(db, SESSION, scratch_dir=db.parent)
        assert result["rows"] == [event(0), event(1)]
        assert reader._terminal_state(db) == before
        assert raw == {suffix: Path(str(db) + suffix).read_bytes() for suffix in raw}
        assert not list(db.parent.glob("native-history-*"))
    finally:
        connection.close()


def test_terminal_copy_keeps_prior_digest_check_and_cleans_failure(db):
    first = reader.read_page(db, SESSION)
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("UPDATE event SET data=data||' '")
    with pytest.raises(reader.HistoryReadError, match="Prior row identity mismatch"):
        reader.read_terminal_page(
            db, SESSION, scratch_dir=db.parent, after=0, through=0,
            last_event_sha256=first["last_event_sha256"],
        )
    assert not list(db.parent.glob("native-history-*"))


@pytest.mark.parametrize("phase", ["copy", "read"])
@pytest.mark.parametrize("fault", ["rewrite", "sidecar"])
def test_terminal_copy_rejects_source_changes(db, monkeypatch, phase, fault):
    def mutate():
        if fault == "rewrite":
            db.write_bytes(db.read_bytes() + b"changed")
        else:
            Path(str(db) + "-wal").write_bytes(b"changed")

    if phase == "copy":
        original = reader.os.fstat
        calls = 0

        def fstat(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                mutate()
            return original(fd)

        monkeypatch.setattr(reader.os, "fstat", fstat)
    else:
        original = reader.read_page

        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            mutate()
            return result

        monkeypatch.setattr(reader, "read_page", read)
    with pytest.raises(reader.HistoryReadError, match="Terminal database identity"):
        reader.read_terminal_page(db, SESSION, scratch_dir=db.parent)
    assert not list(db.parent.glob("native-history-*"))


def test_terminal_copy_rejects_linked_sidecar(db):
    os.link(db, str(db) + "-wal")
    with pytest.raises(reader.HistoryReadError, match="regular, not links"):
        reader.read_terminal_page(db, SESSION, scratch_dir=db.parent)
    assert not list(db.parent.glob("native-history-*"))


def test_terminal_copy_ignores_uncommitted_wal_tail(db):
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        insert(connection, event(1))
        connection.execute("UPDATE event_sequence SET seq=1")
        connection.commit()
        connection.execute("PRAGMA cache_size=1")
        connection.execute("UPDATE event SET data=data||?", (" " * 100000,))
        connection.execute("UPDATE event_sequence SET seq=99")
        before = reader._terminal_state(db)
        result = reader.read_terminal_page(db, SESSION, scratch_dir=db.parent)
        assert result["rows"] == [event(0), event(1)]
        assert reader._terminal_state(db) == before
        connection.rollback()


@pytest.mark.parametrize("phase", ["copy", "read"])
def test_terminal_copy_cleans_io_failures(db, monkeypatch, phase):
    before = reader._terminal_state(db)

    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(reader, "open" if phase == "copy" else "read_page",
                        fail, raising=False)
    with pytest.raises(OSError, match="No space left"):
        reader.read_terminal_page(db, SESSION, scratch_dir=db.parent)
    assert reader._terminal_state(db) == before
    assert not list(db.parent.glob("native-history-*"))


@pytest.mark.parametrize("fault", ["same_size", "sidecar_removed"])
def test_terminal_copy_rejects_mutation_after_descriptor_open(db, monkeypatch, fault):
    if fault == "sidecar_removed":
        Path(str(db) + "-shm").write_bytes(b"index")
    original = reader.os.fstat
    calls = 0

    def fstat(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            if fault == "same_size":
                content = db.read_bytes()
                db.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
                # Avoid relying on the fixture filesystem's timestamp resolution.
                info = db.stat()
                os.utime(db, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
            else:
                Path(str(db) + "-shm").unlink()
        return original(fd)

    monkeypatch.setattr(reader.os, "fstat", fstat)
    with pytest.raises(reader.HistoryReadError, match="Terminal database identity"):
        reader.read_terminal_page(db, SESSION, scratch_dir=db.parent)
    assert not list(db.parent.glob("native-history-*"))


def test_terminal_copy_rejects_replacement_before_inventory_recheck(db, monkeypatch):
    original = reader._terminal_state
    calls = 0

    def state(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            # Windows prohibits replacing an open file; this seam is after close.
            replacement = db.with_suffix(".replacement")
            replacement.write_bytes(db.read_bytes())
            os.replace(replacement, db)
        return original(path)

    monkeypatch.setattr(reader, "_terminal_state", state)
    with pytest.raises(reader.HistoryReadError, match="Terminal database identity"):
        reader.read_terminal_page(db, SESSION, scratch_dir=db.parent)
    assert not list(db.parent.glob("native-history-*"))


def test_linux_terminal_descriptor_keeps_ctime_check(db, monkeypatch):
    with db.open("rb") as source:
        expected = reader._terminal_identity(os.fstat(source.fileno()))
        monkeypatch.setattr(reader, "sys", SimpleNamespace(platform="linux"))
        assert reader._terminal_descriptor_matches(source.fileno(), expected)
        changed = (*expected[:-1], expected[-1] + 1)
        assert not reader._terminal_descriptor_matches(source.fileno(), changed)


def test_terminal_hot_journal_fails_without_recovering_original(db):
    subprocess.run([
        sys.executable, "-I", "-S", "-c",
        "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA cache_size=1'); "
        "c.execute('BEGIN IMMEDIATE'); "
        "c.execute(\"UPDATE event SET data=data||?\", (' '*1000000,)); "
        "os._exit(0)", str(db),
    ], check=True)
    journal = Path(str(db) + "-journal")
    assert journal.exists() and any(journal.read_bytes()[:8])
    before = reader._terminal_state(db)
    with pytest.raises(reader.HistoryReadError, match="Native SQLite read failed"):
        reader.read_terminal_page(db, SESSION, scratch_dir=db.parent)
    assert reader._terminal_state(db) == before
    assert not list(db.parent.glob("native-history-*"))
