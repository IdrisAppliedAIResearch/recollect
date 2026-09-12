import sqlite3
import subprocess
import sys

import pytest

from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.journal import (
    Anchor,
    IntegrityError,
    Journal,
    inspect_archive,
    read_archive,
)
from tests.selfmod_checkpoint_helpers import EVIDENCE, Fault


def test_archive_readback_exact_bytes_and_pinned_head(tmp_path):
    root = tmp_path / "attempt"
    with Journal.create(root) as journal:
        first = journal.append("first", {"text": "exact\ntext"}, EVIDENCE)
        second = journal.append("second", {}, Snapshot((File("empty", b""),)))
        assert read_archive(root, journal.head) == (first, second)
        assert second.value["previous"] == first.anchor.sha256
        with pytest.raises(IntegrityError, match="head mismatch"):
            read_archive(root, first.anchor)
        with pytest.raises(IntegrityError):
            read_archive(root, Anchor(2, "0" * 64))
    with pytest.raises(FileExistsError):
        Journal.create(root)


@pytest.mark.parametrize(
    "operation",
    [
        "UPDATE records SET body=x'00'",
        "DELETE FROM records",
        "UPDATE files SET content=x'00'",
        "DELETE FROM files",
        "INSERT OR REPLACE INTO records SELECT * FROM records",
        "INSERT OR REPLACE INTO files SELECT * FROM files",
        "INSERT INTO files VALUES (1,'late.txt',x'01')",
    ],
)
def test_archive_rejects_mutation_replace_and_late_evidence(tmp_path, operation):
    with Journal.create(tmp_path / "attempt") as journal:
        journal.append("first", {}, EVIDENCE)
        connection = sqlite3.connect(journal.root / "journal.sqlite")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(operation)
                connection.commit()
            connection.rollback()
        finally:
            connection.close()
        assert len(journal.verify()) == 1


@pytest.mark.parametrize(
    "point,committed",
    [
        ("journal.before_commit:item", False),
        ("journal.after_commit:item", True),
        ("journal.before_readback:item", True),
        ("journal.after_readback:item", True),
    ],
)
def test_uncertain_storage_poison_and_recovery_preserves_actual_record(
    tmp_path,
    point,
    committed,
):
    fault = Fault()
    root = tmp_path / "attempt"
    journal = Journal.create(root, fault=fault)
    journal.append("initial", {})
    fault.at = point
    with pytest.raises(OSError):
        journal.append("item", {}, EVIDENCE)
    with pytest.raises(IntegrityError, match="poisoned"):
        journal.append("retry", {})
    journal.close()
    with Journal.recover(root) as recovered:
        assert recovered.recovery_only
        assert len(recovered.verify()) == 1 + committed
        assert list(root.glob("recovery-*/journal.sqlite"))


def test_second_process_cannot_take_controller_ownership(tmp_path):
    root = tmp_path / "attempt"
    with Journal.create(root) as journal:
        journal.append("initial", {})
        program = (
            "from pathlib import Path; from recollect.selfmod.journal import Journal; "
            "import sys; Journal.recover(Path(sys.argv[1]))"
        )
        child = subprocess.run(
            [sys.executable, "-c", program, str(root)], capture_output=True, timeout=20
        )
        assert child.returncode != 0
        assert len(journal.verify()) == 1


@pytest.mark.parametrize(
    "point,committed",
    [
        ("journal.before_commit:crash", False),
        ("journal.after_commit:crash", True),
    ],
)
def test_actual_process_exit_and_hot_journal_accounting(tmp_path, point, committed):
    root = tmp_path / "attempt"
    program = """
import os, sys
from pathlib import Path
from recollect.selfmod.journal import Journal
from recollect.selfmod.contracts import File, Snapshot
root = Path(sys.argv[1])
journal = Journal.create(root)
journal.append('initial', {})
journal.fault = lambda point: os._exit(73) if point == sys.argv[2] else None
journal.append('crash', {}, Snapshot((File('large.bin', b'x' * 2000000),)))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(root), point],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 73
    with Journal.recover(root) as recovered:
        assert len(recovered.verify()) == 1 + committed
        assert list(root.glob("recovery-*/journal.sqlite"))
        if not committed:
            assert list(root.glob("recovery-*/journal.sqlite-journal"))


def test_schema_tampering_is_detected_before_another_write(tmp_path):
    with Journal.create(tmp_path / "attempt") as journal:
        journal.append("initial", {})
        with sqlite3.connect(journal.root / "journal.sqlite") as connection:
            connection.execute("DROP TRIGGER records_update")
        with pytest.raises(IntegrityError, match="schema"):
            journal.verify()
        with pytest.raises(IntegrityError, match="poisoned"):
            journal.append("late", {})


def test_unanchored_inspection_is_not_a_truncation_check(tmp_path):
    with Journal.create(tmp_path / "attempt") as journal:
        journal.append("first", {})
        second = journal.append("second", {})
        db = sqlite3.connect(journal.root / "journal.sqlite")
        try:
            trigger = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='records_delete'"
            ).fetchone()[0]
            db.execute("DROP TRIGGER records_delete")
            db.execute("DELETE FROM records WHERE seq=2")
            db.execute(trigger)
            db.commit()
        finally:
            db.close()
        assert len(inspect_archive(journal.root)) == 1
        with pytest.raises(IntegrityError, match="head mismatch"):
            read_archive(journal.root, second.anchor)
