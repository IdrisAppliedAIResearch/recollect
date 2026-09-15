import asyncio
import sqlite3
import weakref
from contextlib import closing
from dataclasses import replace

import pytest

from recollect.selfmod import journal as journal_module
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.evidence_segments import (
    copy_archive,
    import_segments,
    iter_segments,
)
from recollect.selfmod.journal import (
    MAX_FILES,
    Anchor,
    IntegrityError,
    Journal,
    Record,
    encode,
    inventory,
    iter_archive,
    read_archive,
    sha256,
)


def records(count, *, file_count=1, content=b"exact\x00bytes\n"):
    previous = None
    for seq in range(1, count + 1):
        files = Snapshot(tuple(
            File(path, content)
            for path in sorted(f"part-{i}.txt" for i in range(file_count))
        ))
        body = encode(dict(
            version=1, sequence=seq, previous=previous, kind="native_event",
            data={"index": seq, "text": "original \u00e9vidence"},
            files=inventory(files),
        ))
        previous = sha256(body)
        yield Record(Anchor(seq, previous), body, files)


def seed(root, count, **kwargs):
    # Build a large valid source in one transaction. Production appends retain
    # their full verification cost; that cost is not what the iterator test times.
    with Journal.create(root):
        pass
    head = None
    with closing(sqlite3.connect(root / "journal.sqlite")) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for record in records(count, **kwargs):
            head = record.anchor
            for file in record.files.files:
                connection.execute("INSERT INTO files VALUES (?,?,?)", (
                    head.sequence, file.path, file.content,
                ))
            connection.execute("INSERT INTO records VALUES (?,?,?)", (
                head.sequence, record.body, head.sha256,
            ))
        connection.commit()
    return head


def tamper(root, operations):
    # Restore the exact triggers after corruption, so chain/file checks must
    # catch it independently of schema equality.
    with closing(sqlite3.connect(root / "journal.sqlite")) as connection:
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        for sql, args in operations:
            connection.execute(sql, args)
        for _, sql in triggers:
            connection.execute(sql)
        connection.commit()


def test_many_records_iteration_is_bounded_and_ordered(tmp_path, monkeypatch):
    root = tmp_path / "source"
    count = MAX_FILES + 7
    head = seed(root, count)
    live = weakref.WeakValueDictionary()
    validate = journal_module.validate_record

    def observe(record, previous):
        validate(record, previous)
        live[record.anchor.sequence] = record
        assert len(live) <= 3, "Archive reader retained earlier records"

    monkeypatch.setattr(journal_module, "validate_record", observe)
    with closing(iter_archive(root, head)) as stream:
        for index, record in enumerate(stream, 1):
            assert record.anchor.sequence == index
            assert record.value["data"]["index"] == index
            assert record.files.files == (File("part-0.txt", b"exact\x00bytes\n"),)
    assert index == count


def test_copy_exceeds_aggregate_file_limit_without_tuple_readers(
    tmp_path, monkeypatch,
):
    root, target = tmp_path / "source", tmp_path / "sidecar"
    count, file_count = 65, 64
    assert count * file_count > MAX_FILES
    head = seed(root, count, file_count=file_count)

    def forbidden(*args, **kwargs):
        raise AssertionError("Copy must not materialize all archive records")

    # Creation checks only an empty archive using the old tuple API.
    original = journal_module.read_archive

    def empty_only(root, expected):
        assert expected is None
        return original(root, expected)

    monkeypatch.setattr(journal_module, "read_archive", empty_only)
    monkeypatch.setattr(journal_module, "inspect_archive", forbidden)
    monkeypatch.setattr(Journal, "verify", forbidden)
    assert copy_archive(root, target, head) == head
    with closing(iter_segments(target, head)) as stream:
        for index, record in enumerate(stream, 1):
            assert record.anchor.sequence == index
            assert len(record.files.files) == file_count
    assert index == count
    with (closing(iter_archive(root, head)) as source,
          closing(iter_segments(target, head)) as copied):
        for first, second in zip(source, copied, strict=True):
            assert first == second


def test_record_at_file_count_bound_is_not_wrapped(tmp_path):
    source = tuple(records(1, file_count=MAX_FILES))
    target = tmp_path / "sidecar"
    head = source[-1].anchor
    assert import_segments(iter(source), target, head) == head
    assert tuple(iter_segments(target, head)) == source


def test_byte_limits_are_per_record_not_aggregate(tmp_path, monkeypatch):
    # Scale the existing limits down to exercise the same boundaries without
    # writing hundreds of MiB solely for a unit test.
    monkeypatch.setattr(journal_module, "MAX_FILE_BYTES", 2048)
    monkeypatch.setattr(journal_module, "MAX_RECORD_BYTES", 4096)
    source = tuple(records(5, file_count=2, content=b"x" * 2048))
    target = tmp_path / "sidecar"
    head = source[-1].anchor
    assert import_segments(iter(source), target, head) == head
    assert tuple(iter_segments(target, head)) == source


def test_existing_tuple_apis_and_owner_iterator_match(tmp_path):
    with Journal.create(tmp_path / "source") as journal:
        first = journal.append("first", {}, Snapshot((File("empty", b""),)))
        second = journal.append("second", {})
        assert journal.verify() == (first, second)
        assert tuple(journal.iter_verify()) == (first, second)
        assert read_archive(journal.root, second.anchor) == (first, second)
        assert journal_module.inspect_archive(journal.root) == (first, second)


@pytest.mark.parametrize("count", [0, 3])
def test_empty_anchor_means_exact_empty_archive(tmp_path, count):
    root, target = tmp_path / "source", tmp_path / "sidecar"
    seed(root, count)
    if count:
        with pytest.raises(IntegrityError, match="head mismatch"):
            copy_archive(root, target, None)
        assert not (target / "complete.json").exists()
    else:
        assert copy_archive(root, target, None) is None
        assert tuple(iter_segments(target, None)) == ()


@pytest.mark.parametrize("change", ["drop", "reorder", "repeat", "truncate", "extra"])
def test_import_rejects_changed_sequence_without_completion(tmp_path, change):
    source = tuple(records(4))
    changed = {
        "drop": (source[0], *source[2:]),
        "reorder": (source[1], source[0], *source[2:]),
        "repeat": (source[0], *source),
        "truncate": source[:-1],
        "extra": (*source, next(iter(records(1)))),
    }[change]
    target = tmp_path / "sidecar"
    with pytest.raises(IntegrityError):
        import_segments(iter(changed), target, source[-1].anchor)
    assert not (target / "complete.json").exists()
    with pytest.raises(IntegrityError, match="incomplete"):
        tuple(iter_segments(target, source[-1].anchor))


@pytest.mark.parametrize("operation", ["drop", "reorder", "truncate", "bad_file",
                                       "missing_file", "orphan", "bad_body"])
def test_independent_archive_rejects_corruption(tmp_path, operation):
    root, target = tmp_path / "source", tmp_path / "sidecar"
    head = seed(root, 4)
    changes = {
        "drop": [("DELETE FROM files WHERE seq=2", ()),
                 ("DELETE FROM records WHERE seq=2", ())],
        "reorder": [("UPDATE records SET seq=5 WHERE seq=2", ()),
                    ("UPDATE records SET seq=2 WHERE seq=3", ()),
                    ("UPDATE records SET seq=3 WHERE seq=5", ())],
        "truncate": [("DELETE FROM files WHERE seq=4", ()),
                     ("DELETE FROM records WHERE seq=4", ())],
        "bad_file": [("UPDATE files SET content=? WHERE seq=3", (b"changed",))],
        "missing_file": [("DELETE FROM files WHERE seq=3", ())],
        "orphan": [("INSERT INTO files VALUES (99,'orphan',?)", (b"",))],
        "bad_body": [("UPDATE records SET body=? WHERE seq=3", (b"{}\n",))],
    }
    tamper(root, changes[operation])
    with pytest.raises(IntegrityError):
        tuple(iter_archive(root, head))
    with pytest.raises(IntegrityError):
        copy_archive(root, target, head)
    assert not (target / "complete.json").exists()


@pytest.mark.parametrize("sql", ["DROP TRIGGER records_update",
                                 "PRAGMA user_version=2",
                                 "PRAGMA journal_mode=WAL"])
def test_stream_checks_full_schema_and_settings(tmp_path, sql):
    root = tmp_path / "source"
    head = seed(root, 1)
    with closing(sqlite3.connect(root / "journal.sqlite")) as connection:
        connection.execute(sql)
        connection.commit()
    with pytest.raises(IntegrityError, match="schema/settings"):
        tuple(iter_archive(root, head))


@pytest.mark.parametrize("field,value", [
    ("version", True), ("sequence", True), ("previous", "0" * 64),
    ("kind", 1), ("data", []), ("extra", None), ("files", []),
])
def test_import_checks_full_body_even_with_matching_hash(tmp_path, field, value):
    record = next(records(1))
    body = encode({**record.value, field: value})
    invalid = replace(record, body=body, anchor=Anchor(1, sha256(body)))
    target = tmp_path / "sidecar"
    with pytest.raises(IntegrityError):
        import_segments(iter((invalid,)), target, invalid.anchor)
    assert not (target / "complete.json").exists()


@pytest.mark.parametrize("change", ["hash", "file", "file_count", "file_bytes",
                                    "record_bytes", "body_bytes", "noncanonical"])
def test_import_rejects_bad_files_hashes_and_individual_bounds(
    tmp_path, monkeypatch, change,
):
    record = next(records(1, file_count=2, content=b"ab"))
    if change == "hash":
        record = replace(record, anchor=Anchor(1, "0" * 64))
    elif change == "file":
        record = replace(record, files=Snapshot((File("bad.txt", b"changed"),)))
    elif change == "file_count":
        monkeypatch.setattr(journal_module, "MAX_FILES", 1)
    elif change == "file_bytes":
        record = next(records(1, content=b"x" * 4096))
        monkeypatch.setattr(journal_module, "MAX_FILE_BYTES", 2048)
    elif change == "record_bytes":
        monkeypatch.setattr(journal_module, "MAX_RECORD_BYTES", 3)
    elif change == "body_bytes":
        monkeypatch.setattr(journal_module, "MAX_FILE_BYTES", len(record.body) - 1)
    else:
        body = record.body + b"\n"
        record = replace(record, body=body, anchor=Anchor(1, sha256(body)))
    target = tmp_path / "sidecar"
    with pytest.raises(IntegrityError):
        import_segments(iter((record,)), target, record.anchor)
    assert not (target / "complete.json").exists()


@pytest.mark.parametrize("after", [1, 3])
def test_cancelled_input_never_publishes_completion_even_after_last_record(
    tmp_path, after,
):
    source = tuple(records(3))
    target = tmp_path / "sidecar"

    def interrupted():
        yield from source[:after]
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        import_segments(interrupted(), target, source[-1].anchor)
    assert not (target / "complete.json").exists()
    assert read_archive(target, source[after - 1].anchor) == source[:after]
    with pytest.raises(IntegrityError, match="incomplete"):
        tuple(iter_segments(target, source[-1].anchor))
    with pytest.raises(FileExistsError):
        import_segments(iter(source), target, source[-1].anchor)


def test_failure_during_append_readback_does_not_publish(tmp_path, monkeypatch):
    source = tuple(records(3))
    original = Journal.append

    def fail_after_write(journal, *args):
        result = original(journal, *args)
        if result.anchor.sequence == 2:
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(Journal, "append", fail_after_write)
    target = tmp_path / "sidecar"
    with pytest.raises(asyncio.CancelledError):
        import_segments(iter(source), target, source[-1].anchor)
    assert not (target / "complete.json").exists()
    assert read_archive(target, source[1].anchor) == source[:2]


def test_completion_marker_does_not_substitute_for_independent_verification(tmp_path):
    source = tuple(records(3))
    target = tmp_path / "sidecar"
    head = source[-1].anchor
    import_segments(iter(source), target, head)
    tamper(target, [("DELETE FROM files WHERE seq=3", ()),
                    ("DELETE FROM records WHERE seq=3", ())])
    with pytest.raises(IntegrityError, match="head mismatch"):
        tuple(iter_segments(target, head))
    with pytest.raises(IntegrityError, match="anchor mismatch"):
        tuple(iter_segments(target, source[1].anchor))


def test_incomplete_completion_marker_is_rejected(tmp_path):
    source = tuple(records(1))
    target = tmp_path / "sidecar"
    import_segments(iter(source), target, source[0].anchor)
    marker = target / "complete.json"
    marker.write_bytes(marker.read_bytes()[:-1])
    with pytest.raises(IntegrityError, match="incomplete"):
        tuple(iter_segments(target, source[0].anchor))


def test_closing_iterator_releases_snapshot_and_owner_can_continue(tmp_path):
    with Journal.create(tmp_path / "source") as journal:
        first = journal.append("first", {})
        stream = journal.iter_verify()
        assert next(stream) == first
        stream.close()
        assert not journal.poisoned
        second = journal.append("second", {})
        assert journal.verify() == (first, second)
        # A correct prefix is not a successful exact-end verification.
        with pytest.raises(IntegrityError, match="head mismatch"):
            tuple(iter_archive(journal.root, first.anchor))


def test_owner_iterator_still_poisons_corrupted_owner(tmp_path):
    with Journal.create(tmp_path / "source") as journal:
        journal.append("first", {})
        tamper(journal.root, [("DELETE FROM records", ())])
        with pytest.raises(IntegrityError, match="head mismatch"):
            tuple(journal.iter_verify())
        assert journal.poisoned


@pytest.mark.parametrize("change", ["body_bytes", "file_bytes", "file_count",
                                    "record_bytes", "file_type", "path", "alias"])
def test_reader_rejects_bad_files_and_limits_before_loading_payloads(
    tmp_path, monkeypatch, change,
):
    root = tmp_path / "source"
    head = seed(root, 1, file_count=2, content=b"x" * 2048)
    if change == "body_bytes":
        monkeypatch.setattr(journal_module, "MAX_FILE_BYTES", 1)
    elif change == "file_bytes":
        monkeypatch.setattr(journal_module, "MAX_FILE_BYTES", 1024)
    elif change == "file_count":
        monkeypatch.setattr(journal_module, "MAX_FILES", 1)
    elif change == "record_bytes":
        monkeypatch.setattr(journal_module, "MAX_RECORD_BYTES", 2048)
    elif change == "file_type":
        tamper(root, [("UPDATE files SET content='text'", ())])
    elif change == "path":
        tamper(root, [("UPDATE files SET path='../escape' WHERE path='part-0.txt'",
                       ())])
    else:
        tamper(root, [("UPDATE files SET path='PART-0.txt' WHERE path='part-1.txt'",
                       ())])

    loaded = []
    connect = sqlite3.connect

    def observed_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(loaded.append)
        return connection

    monkeypatch.setattr(journal_module.sqlite3, "connect", observed_connect)
    with pytest.raises(IntegrityError):
        tuple(iter_archive(root, head))
    content_reads = sum(sql.startswith("SELECT content FROM files") for sql in loaded)
    if change in {"body_bytes", "file_bytes", "file_type"}:
        assert content_reads == 0
    elif change in {"file_count", "record_bytes"}:
        assert content_reads == 1
    if change == "body_bytes":
        assert not any(sql.startswith("SELECT body FROM records") for sql in loaded)


def test_head_hash_mismatch_and_missing_last_record_are_not_trusted_prefixes(tmp_path):
    root = tmp_path / "source"
    head = seed(root, 3)
    with pytest.raises(IntegrityError, match="head mismatch"):
        tuple(iter_archive(root, Anchor(head.sequence, "0" * 64)))
    tamper(root, [("DELETE FROM files WHERE seq=3", ()),
                  ("DELETE FROM records WHERE seq=3", ())])
    with closing(iter_archive(root, head)) as stream:
        assert next(stream).anchor.sequence == 1
        assert next(stream).anchor.sequence == 2
        with pytest.raises(IntegrityError, match="head mismatch"):
            next(stream)


def test_closed_or_replaced_owner_cannot_verify(tmp_path):
    root = tmp_path / "source"
    with Journal.create(root) as journal:
        journal.append("first", {})
        original = root / "journal.sqlite"
        saved = root / "old.sqlite"
        original.rename(saved)
        original.write_bytes(saved.read_bytes())
        with pytest.raises(IntegrityError, match="identity"):
            tuple(journal.iter_verify())
    with pytest.raises(IntegrityError, match="closed or poisoned"):
        tuple(journal.iter_verify())
