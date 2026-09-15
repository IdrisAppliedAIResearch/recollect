"""SQLite fixtures only: these do not qualify the live binary or compaction."""

import asyncio
import base64
import io
import json
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from recollect.selfmod import native_history_reader as reader
from recollect.selfmod.journal import IntegrityError, Journal
from recollect.selfmod.native_history import NativeHistory, iter_event_rows

SESSION = "ses_fixture"


def event(seq, *, kind=None, data=None, session=SESSION):
    kind = kind or ("session.created.1" if seq == 0 else "message.updated.1")
    if data is None:
        data = ({"info": {"id": session}} if seq == 0 else
                {"info": {"id": f"msg_{seq}", "sessionID": session}})
    return dict(id=f"evt_{session}_{seq}", aggregate_id=session, seq=seq, type=kind,
                data=data if isinstance(data, str) else json.dumps(data))


def insert(connection, row):
    connection.execute("INSERT INTO event VALUES (?,?,?,?,?)", tuple(row.values()))


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "native.sqlite"
    # SQLite's transaction context does not close its Windows file handle.
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE event (id text,aggregate_id text,seq int,"
                           "type text,data text)")
        connection.execute("CREATE TABLE event_sequence "
                           "(aggregate_id text,seq int,owner_id text)")
        connection.execute("INSERT INTO event_sequence VALUES (?,0,'owner_fixture')",
                           (SESSION,))
        insert(connection, event(0))
    return path


def append(path, rows):
    with sqlite3.connect(path) as connection:
        for row in rows:
            insert(connection, row)
        connection.execute("UPDATE event_sequence SET seq=? WHERE aggregate_id=?",
                           (rows[-1]["seq"], SESSION))


def page(path, **kwargs):
    return reader.read_page(path, SESSION, **kwargs)


@pytest.fixture
def archive(tmp_path):
    with Journal.create(tmp_path / "archive") as journal:
        yield journal


def history(db, archive, hook=None):
    calls = []

    async def read(request):
        calls.append(request)
        if hook:
            await hook(request, len(calls))
        return reader.canonical(reader.read_page(db, **request))

    return NativeHistory(SESSION, archive, reader=read), calls


def test_exact_rows_and_boundary_digest(db):
    raw = ' { "info" : {"sessionID":"ses_fixture", "id":"msg_1"}, "x": 1.0 } '
    append(db, [event(1, data=raw)])
    result = page(db)
    assert result["rows"] == [event(0), event(1, data=raw)]
    assert result["rows"][1]["data"] == raw
    assert result["watermark"] == result["head"] == result["after"] == 1
    assert result["complete"] is True
    assert result["prior_row_sha256"] is None
    boundary = page(db, after=1, through=1,
                    last_event_sha256=result["last_event_sha256"])
    assert boundary["rows"] == []
    assert boundary["prior_row_sha256"] == result["last_event_sha256"]


@pytest.mark.parametrize("sql", [
    "DELETE FROM event WHERE seq=0",
    "UPDATE event SET seq=1 WHERE seq=0",
    "INSERT INTO event SELECT * FROM event",
    "UPDATE event_sequence SET seq=1",
    "DELETE FROM event_sequence",
    "INSERT INTO event_sequence SELECT * FROM event_sequence",
    "UPDATE event_sequence SET owner_id=x'ff'",
    "UPDATE event SET aggregate_id='ses_foreign'",
    "UPDATE event SET type='message.part.delta'",
    "UPDATE event SET type='session.created.2'",
    "UPDATE event SET data='{}'",
    "UPDATE event SET data=x'ff'",
    "ALTER TABLE event ADD COLUMN surprise TEXT",
])
def test_corrupt_native_database_rejected(db, sql):
    with sqlite3.connect(db) as connection:
        connection.execute(sql)
    with pytest.raises(ValueError):
        page(db)


@pytest.mark.parametrize("raw", [
    "{", "[]", '{"info":{"id":"ses_foreign"}}',
    '{"info":{"id":"ses_fixture"},"x":NaN}',
    '{"info":{"id":"ses_fixture"},"x":Infinity}',
    '{"info":{"id":"ses_fixture"},"x":1e999}',
    '{"info":{"id":"ses_fixture"},"x":1,"x":2}',
    '{"info":{"id":"ses_fixture"},"sessionID":"ses_other"}',
])
def test_malformed_nonfinite_or_foreign_data_preserves_failure_prefix(db, raw):
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE event SET data=?", (raw,))
    with pytest.raises(reader.HistoryReadError) as caught:
        page(db)
    assert b'"data"' in caught.value.raw_prefix


@pytest.mark.parametrize("kwargs", [
    {"after": True}, {"after": -2}, {"after": 1}, {"through": True},
    {"through": -1}, {"through": 1}, {"last_event_sha256": "x" * 64},
    {"last_event_sha256": "a" * 64}, {"offset": 1},
    {"offset": 0, "row_sha256": "a" * 64},
])
def test_invalid_bounds(db, kwargs):
    with pytest.raises(ValueError):
        page(db, **kwargs)


def test_rollback_and_prior_mutation(db):
    append(db, [event(1)])
    first = page(db)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE event SET id='replaced' WHERE seq=1")
    with pytest.raises(ValueError, match="Prior row"):
        page(db, after=1, last_event_sha256=first["last_event_sha256"])
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM event WHERE seq=1")
        connection.execute("UPDATE event_sequence SET seq=0")
    with pytest.raises(ValueError, match="rollback"):
        page(db, after=1, through=1)


def test_other_sessions_do_not_cross_owned_aggregate(db):
    with sqlite3.connect(db) as connection:
        insert(connection, event(0, session="ses_other"))
        connection.execute("INSERT INTO event_sequence VALUES ('ses_other',0,'other')")
    assert page(db)["rows"] == [event(0)]


@pytest.mark.parametrize("initial", [None, "", "original"])
async def test_nullable_owner_is_frozen_including_none_to_string(db, archive, initial):
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE event_sequence SET owner_id=?", (initial,))
    owner, _ = history(db, archive)
    assert await owner.capture() == 0
    assert owner.final_metadata["owner_id"] == initial
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE event_sequence SET owner_id='new_owner'")
    with pytest.raises(IntegrityError, match="ownership"):
        await owner.capture()


def test_committed_wal_and_uncommitted_projection_are_transactional(db):
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        insert(connection, event(1))
        connection.execute("UPDATE event_sequence SET seq=1")
        connection.commit()
        assert Path(str(db) + "-wal").stat().st_size > 0
        assert page(db)["after"] == 1
        insert(connection, event(2))
        connection.execute("UPDATE event_sequence SET seq=2")
        assert page(db)["after"] == 1
        connection.commit()
        assert page(db)["after"] == 2
    finally:
        connection.close()


def test_read_transaction_stays_consistent_during_concurrent_commit(db, monkeypatch):
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        original = reader._schema

        def concurrent_commit(read_connection):
            original(read_connection)
            insert(connection, event(1))
            connection.execute("UPDATE event_sequence SET seq=1")
            connection.commit()

        monkeypatch.setattr(reader, "_schema", concurrent_commit)
        result = page(db)
        assert result["after"] == result["head"] == 0
    finally:
        connection.close()


async def test_incremental_capture_pins_highwater_and_rechecks_boundary(db, archive,
                                                                      monkeypatch):
    monkeypatch.setattr(reader, "MAX_PAGE_BYTES", 1100)
    append(db, [event(i) for i in range(1, 13)])

    async def concurrent_append(request, count):
        if count == 2:
            append(db, [event(13)])

    owner, calls = history(db, archive, concurrent_append)
    assert await owner.capture() == 12
    assert len(calls) > 2
    assert all(c["through"] == 12 for c in calls[1:])
    assert calls[-1]["after"] == 12
    assert owner.durable_sequence == 12
    assert owner.final_metadata["observed_head"] == 13
    assert await owner.finalize() == 13
    assert owner.final_metadata["final"] is True
    assert owner.final_metadata["history_completeness"] == "committed_native_events"
    assert not owner.final_metadata["execution_receipt"]
    rows = list(iter_event_rows(archive.verify(), session_id=SESSION))
    assert [row["seq"] for row in rows] == list(range(14))
    with pytest.raises(IntegrityError, match="finalized"):
        await owner.capture()


async def test_large_row_and_long_history_are_paged_not_flattened(db, archive):
    raw = json.dumps({"part": {
        "id": "prt_1", "sessionID": SESSION, "messageID": "msg_1", "type": "tool",
        "state": {"output": "original tool output\n" * 80000,
                  "time": {"compacted": 42}},
    }})
    rows = [event(1, kind="message.part.updated.1", data=raw),
            event(2, kind="message.part.updated.1", data=raw)]
    append(db, rows)
    owner, calls = history(db, archive)
    assert await owner.capture() == 2
    records = archive.verify()
    assert len(calls) > 4
    assert any("offset" in call for call in calls)
    assert all(len(file.content) <= reader.MAX_PAGE_BYTES
               for record in records for file in record.files.files)
    assert list(iter_event_rows(records, session_id=SESSION)) == [event(0), *rows]
    assert owner.durable_head["sha256"] == reader.digest(reader.canonical(rows[-1]))


def test_row_limit_fails_closed(db, monkeypatch):
    monkeypatch.setattr(reader, "MAX_ROW_BYTES", 256)
    append(db, [event(1, data={"info": {"sessionID": SESSION}, "output": "x" * 257})])
    with pytest.raises(ValueError, match="16 MiB"):
        page(db)


@pytest.mark.parametrize("mutation", ["owner", "boundary", "head"])
async def test_between_page_mutations_poison_and_archive_prefix(db, archive, mutation):
    async def mutate(request, count):
        if count == 2:
            with sqlite3.connect(db) as connection:
                if mutation == "owner":
                    connection.execute("UPDATE event_sequence SET owner_id='changed'")
                elif mutation == "boundary":
                    connection.execute("UPDATE event SET id='changed' WHERE seq=0")
                else:
                    connection.execute("DELETE FROM event_sequence")

    owner, _ = history(db, archive, mutate)
    with pytest.raises(ValueError):
        await owner.capture()
    assert owner.poisoned
    assert not owner.final_metadata["complete"]
    assert archive.verify()[-1].value["kind"] == "native_history_failure"
    with pytest.raises(IntegrityError, match="failed"):
        await owner.capture()


async def test_final_capture_refuses_writers_advancing(db, archive):
    async def mutate(request, count):
        if count == 2:
            append(db, [event(1)])

    owner, _ = history(db, archive, mutate)
    with pytest.raises(IntegrityError, match="writers advanced"):
        await owner.finalize()
    assert not owner.final_metadata["final"]


@pytest.mark.parametrize("mutation", ["foreign", "gap", "digest", "nan", "duplicate",
                                     "no_progress", "too_big", "noncanonical"])
async def test_untrusted_page_rejected_after_raw_capture(db, archive, mutation):
    result = page(db)
    if mutation == "foreign":
        result["rows"][0]["aggregate_id"] = "ses_other"
    elif mutation == "gap":
        result["rows"][0]["seq"] = 1
    elif mutation == "digest":
        result["last_event_sha256"] = "a" * 64
    elif mutation == "no_progress":
        result.update(rows=[], after=-1, complete=False, last_event_sha256=None)
    raw = reader.canonical(result)
    if mutation == "nan":
        raw = b'{"bad":NaN}\n'
    elif mutation == "duplicate":
        raw = b'{"bad":1,"bad":2}\n'
    elif mutation == "too_big":
        raw = b"x" * (reader.MAX_PAGE_BYTES + 1)
    elif mutation == "noncanonical":
        raw += b" "

    async def read(request):
        return raw

    owner = NativeHistory(SESSION, archive, reader=read)
    with pytest.raises(ValueError):
        await owner.capture()
    assert owner.durable_sequence == -1
    assert archive.verify()[-1].files.files[0].content == raw[:reader.MAX_PAGE_BYTES]


async def test_reader_failure_archives_available_prefix(db, archive):
    async def read(request):
        raise reader.HistoryReadError("transport failed", raw_prefix=b"partial native")

    owner = NativeHistory(SESSION, archive, reader=read)
    with pytest.raises(ValueError, match="transport failed"):
        await owner.capture()
    assert archive.verify()[-1].files.files[0].content == b"partial native"


@pytest.mark.parametrize("stage", ["native_history_page", "native_history_event",
                                   "native_history_captured"])
async def test_repeated_cancel_settles_durable_writes_before_releasing_owner(
    db, archive, stage,
):
    entered, release = threading.Event(), threading.Event()

    def fault(name):
        if name == "journal.before_commit:" + stage:
            entered.set()
            assert release.wait(10)

    archive.fault = fault
    owner, _ = history(db, archive)
    task = asyncio.create_task(owner.capture())
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    with pytest.raises(IntegrityError, match="busy"):
        await owner.capture()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.poisoned
    records = archive.verify()
    assert any(r.value["kind"] == stage for r in records)
    assert records[-1].value["data"]["failure"] == "CancelledError"
    assert not owner.final_metadata["complete"]


async def test_repeated_cancel_during_failure_archive_settles(db, archive):
    entered, release = threading.Event(), threading.Event()
    ready = asyncio.Event()

    def fault(name):
        if name == "journal.before_commit:native_history_failure":
            entered.set()
            assert release.wait(10)

    async def read(request):
        ready.set()
        await asyncio.Event().wait()

    archive.fault = fault
    owner = NativeHistory(SESSION, archive, reader=read)
    task = asyncio.create_task(owner.capture())
    await ready.wait()
    task.cancel()
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert archive.verify()[-1].value["kind"] == "native_history_failure"


async def test_journal_write_failure_is_not_swallowed_or_cursor_advanced(db, archive):
    def fail(name):
        if name == "journal.before_commit:native_history_page":
            raise OSError("disk full")

    archive.fault = fail
    owner, _ = history(db, archive)
    with pytest.raises(IntegrityError, match="poisoned") as caught:
        await owner.capture()
    assert isinstance(caught.value.__context__, OSError)
    assert owner.poisoned and owner.durable_sequence == -1


def test_cli_root_only_and_fixed_path(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(SystemExit, match="root"):
        reader.main()
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    output = io.BytesIO()
    monkeypatch.setattr(reader.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(reader.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        reader.canonical({"session_id": SESSION, "after": -1, "path": "/other"})
    )))
    with pytest.raises(SystemExit):
        reader.main()
    assert "Unknown" in json.loads(output.getvalue())["error"]
    captured = []

    def fake(path, **kwargs):
        captured.append(path)
        return {"ok": True}

    monkeypatch.setattr(reader, "read_page", fake)
    monkeypatch.setattr(reader.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        reader.canonical({"session_id": SESSION, "after": -1})
    )))
    reader.main()
    assert captured == [Path("/state/data/opencode/opencode.db")]


def test_reader_rejects_directory_and_reparse_paths(db, monkeypatch):
    with pytest.raises(ValueError, match="regular"):
        reader.read_page(db.parent, SESSION)
    original = Path.lstat

    def reparse(path):
        result = original(path)
        if path == db:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=1024)
        return result

    monkeypatch.setattr(reader.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024,
                        raising=False)
    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(ValueError, match="regular"):
        page(db)


def test_reader_rejects_symlink_and_sidecar(db, monkeypatch):
    original = Path.lstat

    def linked(path):
        result = original(path)
        if path == db:
            return SimpleNamespace(st_mode=reader.stat.S_IFLNK, st_file_attributes=0)
        return result

    monkeypatch.setattr(Path, "lstat", linked)
    with pytest.raises(ValueError, match="regular"):
        page(db)
    monkeypatch.setattr(Path, "lstat", original)
    Path(str(db) + "-wal").mkdir()
    with pytest.raises(ValueError, match="regular"):
        page(db)


def test_fragment_continuation_detects_mutation(db):
    append(db, [event(1, data={"info": {"sessionID": SESSION},
                               "output": "x" * reader.MAX_PAGE_BYTES})])
    first = page(db)
    fragmented = page(db, after=0, through=1,
                      last_event_sha256=first["last_event_sha256"])
    fragment = fragmented["fragment"]
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE event SET id='mutated' WHERE seq=1")
    with pytest.raises(ValueError, match="Fragment row identity"):
        page(db, after=0, through=1,
             offset=len(base64.b64decode(fragment["data"])),
             row_sha256=fragment["row_sha256"])


@pytest.mark.parametrize("mutation", ["digest", "offset", "size", "empty",
                                     "encoding", "sequence", "abandoned"])
async def test_fragment_faults_poison_without_committing_partial_row(
    db, archive, mutation,
):
    append(db, [event(1, data={"info": {"sessionID": SESSION},
                               "output": "x" * (2 * reader.MAX_PAGE_BYTES)})])

    async def read(request):
        result = reader.read_page(db, **request)
        if request.get("offset"):
            fragment = result["fragment"]
            if mutation == "digest":
                fragment["row_sha256"] = "a" * 64
            elif mutation == "offset":
                fragment["offset"] += 1
            elif mutation == "size":
                fragment["total_bytes"] += 1
            elif mutation == "empty":
                fragment["data"] = ""
            elif mutation == "encoding":
                fragment["data"] = "!?"
            elif mutation == "sequence":
                fragment["seq"] += 1
            else:
                result["fragment"] = None
        return reader.canonical(result)

    owner = NativeHistory(SESSION, archive, reader=read)
    with pytest.raises(ValueError):
        await owner.capture()
    assert owner.durable_sequence == 0 and owner.poisoned
    events = [record for record in archive.verify()
              if record.value["kind"] == "native_history_event"]
    assert [record.value["data"]["seq"] for record in events] == [0]


def test_cli_failure_is_bounded_and_retains_prefix(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(reader.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(reader.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        b"x" * (reader.MAX_PAGE_BYTES + 1)
    )))
    with pytest.raises(SystemExit):
        reader.main()
    raw = output.getvalue()
    assert len(raw) <= reader.MAX_PAGE_BYTES
    result = json.loads(raw)
    assert result["raw_prefix_truncated"]
    assert base64.b64decode(result["raw_prefix"]).startswith(b"xxx")


@pytest.mark.parametrize("mutation", ["id", "data", "delete_replace"])
async def test_finalization_revalidates_old_interior_rows(db, archive, mutation):
    original = [event(0), event(1), event(2)]
    append(db, original[1:])
    owner, calls = history(db, archive)
    assert await owner.capture() == 2
    before = page(db)
    with sqlite3.connect(db) as connection:
        if mutation == "id":
            connection.execute("UPDATE event SET id='changed' WHERE seq=1")
        elif mutation == "data":
            connection.execute("UPDATE event SET data=? WHERE seq=1", (
                json.dumps({"info": {"id": "msg_1", "sessionID": SESSION},
                            "output": "replaced old output"}),
            ))
        else:
            connection.execute("DELETE FROM event WHERE seq=1")
            replacement = event(1)
            replacement["id"] = "replacement"
            insert(connection, replacement)
    changed = page(db)
    assert changed["head"] == before["head"] == 2
    assert changed["last_event_sha256"] == before["last_event_sha256"]
    assert len(changed["rows"]) == len(before["rows"])
    # Incremental capture reports its actual scope without certifying old rows.
    assert await owner.capture() == 2
    assert owner.final_metadata["verification_scope"] == "incremental_boundary"
    assert owner.final_metadata["prefix_revalidated_through"] is None
    with pytest.raises(IntegrityError, match="prefix identity mismatch at sequence 1"):
        await owner.finalize()
    assert owner.poisoned and not owner.final_metadata["final"]
    records = archive.verify()
    verification = [r for r in records
                    if r.value["kind"] == "native_history_verification_page"]
    assert verification[0].value["data"]["request"] == {
        "session_id": SESSION, "after": -1, "through": 2,
        "last_event_sha256": None,
    }
    actual = json.loads(verification[-1].files.files[0].content)["rows"][1]
    assert actual == changed["rows"][1]
    assert records[-1].files.files[0].content == verification[-1].files.files[0].content
    assert records[-1].value["data"]["verification_phase"]
    assert list(iter_event_rows(records, session_id=SESSION)) == original
    assert calls[-1]["after"] == -1 and calls[-1]["through"] == 2


async def test_finalization_rejects_deleted_old_row(db, archive):
    append(db, [event(1), event(2)])
    owner, _ = history(db, archive)
    await owner.capture()
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM event WHERE seq=1")
    with pytest.raises(ValueError, match="gap"):
        await owner.finalize()
    assert owner.poisoned
    assert not any(r.value["kind"] == "native_history_final" for r in archive.verify())


async def test_finalization_revalidates_fragmented_old_row(db, archive):
    original_data = json.dumps({"info": {"sessionID": SESSION},
                                "output": "x" * (2 * reader.MAX_PAGE_BYTES)})
    original = [event(0), event(1, data=original_data), event(2)]
    append(db, original[1:])
    owner, _ = history(db, archive)
    await owner.capture()
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE event SET data=? WHERE seq=1", (
            original_data.replace('"output": "x', '"output": "y'),
        ))
    with pytest.raises(IntegrityError, match="prefix identity mismatch at sequence 1"):
        await owner.finalize()
    records = archive.verify()
    pages = [json.loads(r.files.files[0].content) for r in records
             if r.value["kind"] == "native_history_verification_page"]
    fragments = [p["fragment"] for p in pages if p["fragment"]]
    assert len(fragments) >= 3
    actual = b"".join(base64.b64decode(f["data"]) for f in fragments)
    assert b'\\"output\\": \\"y' in actual
    assert list(iter_event_rows(records, session_id=SESSION)) == original
    assert owner.durable_sequence == 2


async def test_finalization_revalidates_unchanged_fragmented_row(db, archive):
    original = [event(0), event(1, data={"info": {"sessionID": SESSION},
                                        "output": "x" * reader.MAX_PAGE_BYTES}),
                event(2)]
    append(db, original[1:])
    owner, _ = history(db, archive)
    await owner.capture()
    assert await owner.finalize() == 2
    records = archive.verify()
    assert list(iter_event_rows(records, session_id=SESSION)) == original
    assert sum(r.value["kind"] == "native_history_event" for r in records) == 3
    fragments = [json.loads(r.files.files[0].content)["fragment"] for r in records
                 if r.value["kind"] == "native_history_verification_page"]
    assert sum(fragment is not None for fragment in fragments) >= 2
    assert owner.final_metadata["prefix_revalidated_through"] == 2


async def test_finalization_success_archives_bounded_verification_pages(
    db, archive, monkeypatch,
):
    monkeypatch.setattr(reader, "MAX_PAGE_BYTES", 1100)
    original = [event(i) for i in range(13)]
    append(db, original[1:10])
    owner, calls = history(db, archive)
    await owner.capture()
    append(db, original[10:])
    assert await owner.finalize() == 12
    records = archive.verify()
    verification = [r for r in records
                    if r.value["kind"] == "native_history_verification_page"]
    assert len(verification) > 2
    assert all(len(r.files.files[0].content) <= 1100 for r in verification)
    assert verification[0].value["data"]["request"]["after"] == -1
    assert calls[-1]["after"] == calls[-1]["through"] == 12
    assert [r.value["data"]["seq"] for r in records
            if r.value["kind"] == "native_history_event"] == list(range(13))
    assert list(iter_event_rows(records, session_id=SESSION)) == original
    assert owner.final_metadata["verification_scope"] == "full_prefix"
    assert owner.final_metadata["prefix_revalidated_through"] == 12
    assert records[-1].value["data"]["prefix_revalidated_through"] == 12


@pytest.mark.parametrize("when", ["first_page", "later_page", "boundary"])
async def test_concurrent_append_during_prefix_revalidation_fails(
    db, archive, monkeypatch, when,
):
    monkeypatch.setattr(reader, "MAX_PAGE_BYTES", 1100)
    append(db, [event(i) for i in range(1, 10)])
    verifying = False
    appended = False

    async def read(request):
        nonlocal verifying, appended
        if request["after"] == -1 and request.get("through") == 9:
            verifying = True
        trigger = (request["after"] == -1 if when == "first_page" else
                   request["after"] == 9 if when == "boundary" else
                   0 <= request["after"] < 9)
        if verifying and trigger and not appended:
            append(db, [event(10)])
            appended = True
        return reader.canonical(reader.read_page(db, **request))

    owner = NativeHistory(SESSION, archive, reader=read)
    await owner.capture()
    with pytest.raises(IntegrityError, match="writers advanced"):
        await owner.finalize()
    assert appended and owner.poisoned
    assert not owner.final_metadata["final"]
    records = archive.verify()
    assert records[-1].value["data"]["verification_phase"]
    assert json.loads(records[-1].files.files[0].content)["head"] == 10


async def test_repeat_cancel_during_verification_page_cannot_finalize(db, archive):
    entered, release = threading.Event(), threading.Event()

    def fault(name):
        if name == "journal.before_commit:native_history_verification_page":
            entered.set()
            assert release.wait(10)

    owner, _ = history(db, archive)
    await owner.capture()
    archive.fault = fault
    task = asyncio.create_task(owner.finalize())
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    records = archive.verify()
    assert any(r.value["kind"] == "native_history_verification_page" for r in records)
    assert records[-1].value["data"]["verification_phase"]
    assert not any(r.value["kind"] == "native_history_final" for r in records)
    assert owner.poisoned and not owner.final_metadata["final"]
