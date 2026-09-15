"""Linear journal append: tail binding and single-record readback, no prefix reread."""

import sqlite3
from contextlib import closing

import pytest

from recollect.selfmod import journal as journal_module
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.journal import (
    IntegrityError,
    Journal,
    encode,
    inventory,
    read_archive,
    sha256,
)
from tests.test_selfmod_evidence_segments import tamper

EVIDENCE = Snapshot((File("page.json", b'{"rows":[]}\n'),))


def statements(monkeypatch):
    executed = []
    connect = sqlite3.connect

    def observed(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(executed.append)
        return connection

    monkeypatch.setattr(journal_module.sqlite3, "connect", observed)
    return executed


def test_append_never_rereads_prefix_and_cost_is_independent_of_length(
    tmp_path, monkeypatch,
):
    with Journal.create(tmp_path / "attempt") as journal:
        def forbidden(root):
            raise AssertionError("append iterated the whole archive")

        validated = []
        validate = journal_module.validate_record

        def observe(record, previous):
            validated.append(record.anchor.sequence)
            validate(record, previous)

        monkeypatch.setattr(journal_module, "_iter_archive", forbidden)
        monkeypatch.setattr(journal_module, "validate_record", observe)
        executed = statements(monkeypatch)
        counts = []
        for index in range(600):
            before = len(executed)
            journal.append("native_history_event", {"seq": index}, EVIDENCE)
            counts.append(len(executed) - before)
        # One exact readback per append, and the same SQL work at 1 and 600.
        assert validated == list(range(1, 601))
        assert counts[1] == counts[-1]
        body_reads = [sql for sql in executed if sql.startswith("SELECT body FROM")]
        assert len(body_reads) <= 2 * 600
        monkeypatch.undo()
        assert len(journal.verify()) == 600


def external_next(root, head):
    body = encode(dict(version=1, sequence=head.sequence + 1, previous=head.sha256,
                       kind="foreign", data={}, files=inventory(Snapshot(()))))
    with closing(sqlite3.connect(root / "journal.sqlite")) as connection:
        connection.execute("INSERT INTO records VALUES (?,?,?)",
                           (head.sequence + 1, body, sha256(body)))
        connection.commit()


@pytest.mark.parametrize("fault", ["external_append", "orphan_files", "head_body",
                                   "truncated", "schema"])
def test_append_rejects_changed_tail_and_poisons(tmp_path, fault):
    with Journal.create(tmp_path / "attempt") as journal:
        journal.append("first", {}, EVIDENCE)
        head = journal.append("second", {}, EVIDENCE).anchor
        root = journal.root
        if fault == "external_append":
            external_next(root, head)
        elif fault == "orphan_files":
            with closing(sqlite3.connect(root / "journal.sqlite")) as connection:
                connection.execute("INSERT INTO files VALUES (3,'late.txt',x'01')")
                connection.commit()
        elif fault == "head_body":
            tamper(root, [("UPDATE records SET body=? WHERE seq=2",
                           (b'{"forged":true}\n',))])
        elif fault == "truncated":
            tamper(root, [("DELETE FROM files WHERE seq=2", ()),
                          ("DELETE FROM records WHERE seq=2", ())])
        else:
            with closing(sqlite3.connect(root / "journal.sqlite")) as connection:
                connection.execute("DROP TRIGGER records_update")
                connection.commit()
        with pytest.raises(IntegrityError):
            journal.append("third", {}, EVIDENCE)
        assert journal.poisoned
        with pytest.raises(IntegrityError, match="poisoned"):
            journal.append("retry", {})


def test_older_record_tampering_is_caught_by_full_verification(tmp_path):
    with Journal.create(tmp_path / "attempt") as journal:
        journal.append("first", {"original": True}, EVIDENCE)
        journal.append("second", {}, EVIDENCE)
        tamper(journal.root, [("UPDATE records SET body=? WHERE seq=1",
                               (b'{"original":false}\n',))])
        # Append deliberately binds only the head; the prefix check is verify().
        third = journal.append("third", {}, EVIDENCE)
        with pytest.raises(IntegrityError):
            journal.verify()
        assert journal.poisoned
        with pytest.raises(IntegrityError):
            read_archive(journal.root, third.anchor)


def test_readback_failure_after_commit_poisons_without_trusting_head(
    tmp_path, monkeypatch,
):
    with Journal.create(tmp_path / "attempt") as journal:
        first = journal.append("first", {}, EVIDENCE)
        original = journal_module._read_head

        def lost(root, head, previous):
            raise OSError("readback lost")

        monkeypatch.setattr(journal_module, "_read_head", lost)
        with pytest.raises(OSError):
            journal.append("second", {}, EVIDENCE)
        monkeypatch.setattr(journal_module, "_read_head", original)
        assert journal.poisoned and journal.head == first.anchor
    with Journal.recover(tmp_path / "attempt") as recovered:
        assert len(recovered.verify()) == 2


def test_returned_record_is_exact_independent_readback(tmp_path):
    with Journal.create(tmp_path / "attempt") as journal:
        records = [journal.append("item", {"index": i, "text": "é"}, EVIDENCE)
                   for i in range(5)]
        assert journal.verify() == tuple(records)
        assert read_archive(journal.root, journal.head) == tuple(records)
