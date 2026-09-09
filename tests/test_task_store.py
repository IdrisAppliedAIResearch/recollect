"""Durability, conversation isolation, and safe immutable task artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from recollect import task_store
from recollect.config import RecollectConfig
from recollect.session import SessionManager
from recollect.task_store import TaskStorageFull, TaskStore


@pytest.fixture
def tasks(tmp_path, fake_embedder):
    config = RecollectConfig(
        embedding_model_path=tmp_path / "unused.gguf", data_dir=tmp_path / "data",
    )
    sessions = SessionManager(config, fake_embedder)
    first = sessions.create_session("First").session_id
    second = sessions.create_session("Second").session_id
    return TaskStore(config), first, second


def _start(store, session_id, request="request-1", **kwargs):
    return store.start(session_id, request, "Compare options", "Research options",
                       "focused", **kwargs)


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def test_start_and_instruction_retries_survive_reopen(tasks):
    store, first, _ = tasks
    created = _start(store, first)
    reopened = TaskStore(store.config)
    assert reopened.request(first, "request-1") == created
    assert _start(reopened, first) == created
    assert reopened.request(first, "missing") is None
    changed = reopened.steer(first, created["task_id"], "instruction-2", "Under $100")
    retry = store.steer(first, created["task_id"], "instruction-2", "Under $100")
    assert changed == retry
    assert retry["revision"] == 2
    assert retry["accepted_revision"] == 0
    assert retry["objective"] == "Compare options"
    accepted = store.accept_revision(first, created["task_id"], 2)
    assert accepted["accepted_revision"] == 2
    assert store.accept_revision(first, created["task_id"], 2) == accepted
    messages = reopened.messages(first, created["task_id"])
    assert [message["kind"] for message in messages] == ["start", "steer", "accepted"]
    assert [message["seq"] for message in messages] == [1, 2, 3]


def test_concurrent_start_deduplicates_before_dispatch(tasks):
    store, first, _ = tasks
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _start(store, first), range(16)))
    assert len({result["task_id"] for result in results}) == 1
    assert len(store.list(first)) == 1
    assert len(store.messages(first, results[0]["task_id"])) == 1


def test_failed_start_transaction_does_not_leave_intent(tasks, monkeypatch):
    store, first, _ = tasks
    original = store._message

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_message", fail)
    with pytest.raises(OSError, match="disk full"):
        _start(store, first)
    assert store.request(first, "request-1") is None
    monkeypatch.setattr(store, "_message", original)
    assert len(store.messages(first, _start(store, first)["task_id"])) == 1


def test_failed_steering_rolls_back_revision(tasks, monkeypatch):
    store, first, _ = tasks
    task = _start(store, first)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_message", fail)
    with pytest.raises(OSError, match="disk full"):
        store.steer(first, task["task_id"], "change", "Use the second option")
    assert store.get(first, task["task_id"])["revision"] == 1


def test_ids_cannot_rebind_payload_or_task(tasks):
    store, first, _ = tasks
    task = _start(store, first)
    another = _start(store, first, "request-2")
    with pytest.raises(ValueError, match="already used"):
        store.start(first, "request-1", "Different", "Research options", "focused")
    store.steer(first, task["task_id"], "change", "Use A")
    with pytest.raises(ValueError, match="already used"):
        store.steer(first, task["task_id"], "change", "Use B")
    with pytest.raises(ValueError, match="already used"):
        store.steer(first, another["task_id"], "change", "Use A")
    assert store.get(first, another["task_id"])["revision"] == 1


def test_mailboxes_followups_and_notifications_are_owned_by_conversation(tasks):
    store, first, second = tasks
    task = _start(store, first)
    identifier = task["task_id"]
    store.notify(first, identifier, "notice", "Found A and B")
    for operation in (
        lambda: store.get(second, identifier),
        lambda: store.message(second, identifier, "bad", "main", "cancel", {}),
        lambda: store.messages(second, identifier),
        lambda: _start(store, second, parent_task_id=identifier),
        lambda: store.delete(second, identifier),
    ):
        with pytest.raises(KeyError):
            operation()
    assert store.notifications(second) == []
    assert store.get_notification(second, "notice") is None
    assert store.list(second) == []
    followup = _start(store, first, "followup", parent_task_id=identifier)
    assert followup["parent_task_id"] == identifier


def test_notifications_and_cursor_preserve_display_referents(tasks):
    store, first, _ = tasks
    task_id = _start(store, first)["task_id"]
    notice = store.notify(first, task_id, "notice", "A or B?", kind="question",
                          source_refs=["https://example.org/source"])
    assert notice["notification_id"] == "notice"
    assert store.get_notification(first, "notice") == notice
    store.steer(first, task_id, "reply", "The second one", reply_to="notice")
    assert store.notify(
        first, task_id, "notice", "A or B?", kind="question",
        source_refs=["https://example.org/source"],
    ) == notice
    later = store.notify(first, task_id, "later", "Now investigating B")
    replay = TaskStore(store.config).snapshot(first)
    assert replay["notifications"][0]["text"] == "A or B?"
    assert replay["notifications"][0]["source_refs"] == ["https://example.org/source"]
    assert replay["cursor"] == later["seq"]
    assert store.notifications(first, after=notice["seq"]) == [later]
    assert store.messages(first, task_id, direction="main")[-1]["payload"] == {
        "text": "The second one", "reply_to": "notice",
    }
    assert store.all_messages(first, after=notice["seq"], limit=1)[0]["kind"] == "steer"
    assert store.get_notification(first, "reply") is None
    json.dumps(replay)


def test_revision_and_finished_task_constraints(tasks):
    store, first, _ = tasks
    task_id = _start(store, first)["task_id"]
    with pytest.raises(ValueError, match="unknown task question"):
        store.steer(first, task_id, "wrong", "Yes", reply_to="missing")
    assert store.get(first, task_id)["revision"] == 1
    store.accept_revision(first, task_id, 1)
    with pytest.raises(ValueError, match="stale or unknown"):
        store.accept_revision(first, task_id, 0)
    with pytest.raises(ValueError, match="unknown instruction"):
        store.update(first, task_id, result_revision=2)
    store.update(first, task_id, state="completed", result="A", result_revision=1)
    with pytest.raises(ValueError, match="follow-up"):
        store.steer(first, task_id, "late", "Actually use B")


def test_recovery_never_labels_orphaned_execution_running(tasks):
    store, first, _ = tasks
    running = _start(store, first)
    queued = _start(store, first, "queued")
    store.update(first, running["task_id"], state="running", findings=["Found A"],
                 backend_session_id="native-session", progress="Comparing A")
    recovered = TaskStore(store.config).recover(first)
    assert len(recovered) == 1
    assert recovered[0]["state"] == "interrupted"
    assert recovered[0]["partial"] is True
    assert recovered[0]["findings"] == ["Found A"]
    assert recovered[0]["backend_session_id"] is None
    assert store.get(first, queued["task_id"])["state"] == "queued"
    assert store.recover(first) == []


def test_schema_version_and_session_identity_fail_closed(tasks):
    store, first, second = tasks
    _start(store, first)
    path = store.config.session_dir(first) / "tasks.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="unsupported schema"):
        store.list(first)
    metadata = store.config.session_file(second, "session.json")
    metadata.write_text(json.dumps({"session_id": first}), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        store.list(second)
    assert not (metadata.parent / "tasks.sqlite").exists()
    with pytest.raises(ValueError):
        store.list("../outside")


def test_capacity_failures_preserve_data(tasks, monkeypatch):
    store, first, _ = tasks
    task = _start(store, first)
    monkeypatch.setattr(task_store, "MAX_TASKS", 1)
    with pytest.raises(TaskStorageFull, match="full"):
        _start(store, first, "second")
    monkeypatch.setattr(task_store, "MAX_MESSAGES", 1)
    with pytest.raises(TaskStorageFull, match="full"):
        store.steer(first, task["task_id"], "change", "Under $100")
    assert store.get(first, task["task_id"])["revision"] == 1
    with pytest.raises(TaskStorageFull, match="storage limit"):
        store.update(first, task["task_id"], progress="x" * 270_000)
    assert store.get(first, task["task_id"])["progress"] == ""


@pytest.mark.parametrize("extension", ["txt", "md", "csv", "json"])
def test_export_download_revise_and_restore(tasks, workspace, tmp_path, extension):
    store, first, _ = tasks
    task_id = _start(store, first)["task_id"]
    name = f"result.{extension}"
    content = '{"finding": "A"}\n'
    (workspace / name).write_text(content, encoding="utf-8")
    initial = store.export_workspace(first, task_id, workspace)[0]
    assert initial["filename"] == name
    assert initial["version"] == 1
    assert "path" not in initial
    assert initial["sha256"] == hashlib.sha256(
        (workspace / name).read_bytes()
    ).hexdigest()
    assert store.export_workspace(first, task_id, workspace) == [initial]
    downloaded = store.artifact(first, task_id, initial["artifact_id"])
    assert Path(downloaded["path"]).read_text(encoding="utf-8") == content
    store.steer(first, task_id, "change", "Use B")
    store.accept_revision(first, task_id, 2)
    (workspace / name).write_text("B", encoding="utf-8")
    updated = store.export_workspace(first, task_id, workspace)[0]
    assert updated["artifact_id"] != initial["artifact_id"]
    assert updated["version"] == updated["revision"] == 2
    destination = tmp_path / "fresh-container-workspace"
    destination.mkdir()
    assert TaskStore(store.config).restore_workspace(first, task_id, destination) == (
        [updated]
    )
    assert (destination / name).read_text(encoding="utf-8") == "B"
    assert Path(downloaded["path"]).read_text(encoding="utf-8") == content
    assert len(store.get(first, task_id)["artifacts"]) == 2


def test_artifact_ids_and_workspace_are_conversation_scoped(tasks, workspace):
    store, first, second = tasks
    task_id = _start(store, first)["task_id"]
    other = _start(store, second)["task_id"]
    (workspace / "note.txt").write_text("private", encoding="utf-8")
    artifact = store.export_workspace(first, task_id, workspace)[0]
    with pytest.raises(KeyError):
        store.artifact(second, other, artifact["artifact_id"])
    with pytest.raises(KeyError):
        store.restore_workspace(second, task_id, workspace)
    with pytest.raises(ValueError, match="separate"):
        store.export_workspace(first, task_id, store.config.session_dir(first))
    with pytest.raises(ValueError):
        store.artifact(first, task_id, "../session.json")


def test_nested_artifacts_restore_and_binary_or_unsafe_files_are_rejected(
    tasks, workspace, tmp_path,
):
    store, first, _ = tasks
    task_id = _start(store, first)["task_id"]
    (workspace / "reports").mkdir()
    (workspace / "reports" / "research.md").write_text("# Findings", encoding="utf-8")
    assert store.export_workspace(first, task_id, workspace)[0]["name"] == (
        "reports/research.md"
    )
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    store.restore_workspace(first, task_id, fresh)
    assert (fresh / "reports" / "research.md").read_text() == "# Findings"
    (workspace / "broken.txt").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        store.export_workspace(first, task_id, workspace)


@pytest.mark.parametrize("name", [
    "../outside.txt", "/absolute.txt", "C:/root.txt", "a\\b.txt", "NUL.txt",
    "a/../../b.txt", "a//b.txt", "a/./b.txt", "x.txt ", "a:b.txt",
])
def test_unsafe_archive_names_cannot_be_restored(name):
    with pytest.raises(ValueError, match="Unsafe"):
        task_store._relative_name(name)


def test_hardlinks_rejected_for_export_and_restore(tasks, workspace, tmp_path):
    store, first, _ = tasks
    task_id = _start(store, first)["task_id"]
    source = workspace / "note.txt"
    source.write_text("original", encoding="utf-8")
    artifact = store.export_workspace(first, task_id, workspace)[0]
    outside = tmp_path / "outside.txt"
    os.link(source, outside)
    with pytest.raises(ValueError, match="without links"):
        store.export_workspace(first, task_id, workspace)
    with pytest.raises(ValueError, match="without links"):
        store.restore_workspace(first, task_id, workspace)
    assert outside.read_text() == "original"
    assert store.artifact(first, task_id, artifact["artifact_id"])


def test_reparse_directory_and_symlink_are_detected_without_privileges():
    assert task_store._linked(SimpleNamespace(st_mode=stat.S_IFREG,
                                             st_file_attributes=0x400))
    assert task_store._linked(SimpleNamespace(st_mode=stat.S_IFLNK))
    assert not task_store._linked(SimpleNamespace(st_mode=stat.S_IFREG))


def test_file_changed_during_read_is_not_published(tasks, workspace, monkeypatch):
    store, first, _ = tasks
    task_id = _start(store, first)["task_id"]
    source = workspace / "note.txt"
    source.write_text("before", encoding="utf-8")
    real_fstat = os.fstat
    calls = 0

    def changing(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            source.write_text("changed contents", encoding="utf-8")
        return real_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", changing)
    with pytest.raises(ValueError, match="changed while"):
        store.export_workspace(first, task_id, workspace)
    assert store.get(first, task_id)["artifacts"] == []


def test_failed_revision_keeps_previous_download_and_removes_staging(
    tasks, workspace, monkeypatch,
):
    store, first, _ = tasks
    task_id = _start(store, first)["task_id"]
    (workspace / "first.txt").write_text("old", encoding="utf-8")
    old = store.export_workspace(first, task_id, workspace)[0]
    (workspace / "first.txt").write_text("new", encoding="utf-8")
    (workspace / "second.txt").write_text("second", encoding="utf-8")
    real_fsync = os.fsync
    calls = 0

    def failing(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", failing)
    with pytest.raises(OSError, match="disk full"):
        store.export_workspace(first, task_id, workspace)
    assert store.get(first, task_id)["artifacts"] == [old]
    path = Path(store.artifact(first, task_id, old["artifact_id"])["path"])
    assert path.read_text() == "old"
    assert list(path.parent.iterdir()) == [path]


def test_retained_version_and_file_quotas_never_delete_old_artifacts(
    tasks, workspace, monkeypatch,
):
    store, first, _ = tasks
    task_id = _start(store, first)["task_id"]
    source = workspace / "note.txt"
    source.write_text("1234", encoding="utf-8")
    old = store.export_workspace(first, task_id, workspace)[0]
    monkeypatch.setattr(task_store, "MAX_TASK_BYTES", 6)
    source.write_text("5678", encoding="utf-8")
    with pytest.raises(TaskStorageFull, match="Previous versions"):
        store.export_workspace(first, task_id, workspace)
    assert store.get(first, task_id)["artifacts"] == [old]
    monkeypatch.setattr(task_store, "MAX_FILE_BYTES", 3)
    with pytest.raises(TaskStorageFull, match="per-file"):
        store.export_workspace(first, task_id, workspace)


def test_saved_artifact_tampering_is_detected(tasks, workspace):
    store, first, _ = tasks
    task_id = _start(store, first)["task_id"]
    (workspace / "note.txt").write_text("original", encoding="utf-8")
    saved = store.export_workspace(first, task_id, workspace)[0]
    path = Path(store.artifact(first, task_id, saved["artifact_id"])["path"])
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="recorded content"):
        store.artifact(first, task_id, saved["artifact_id"])


def test_reset_requires_stopped_workers_and_invalidates_old_messages(tasks, workspace):
    store, first, second = tasks
    task_id = _start(store, first)["task_id"]
    second_task = _start(store, second)
    (workspace / "note.txt").write_text("saved", encoding="utf-8")
    saved = store.export_workspace(first, task_id, workspace)[0]
    saved_path = Path(store.artifact(first, task_id, saved["artifact_id"])["path"])
    notice = store.notify(first, task_id, "notice", "Saved a file")
    store.update(first, task_id, state="running")
    with pytest.raises(ValueError, match="Stop task workers"):
        store.reset(first)
    store.update(first, task_id, state="canceled")
    store.reset(first)
    assert store.list(first) == []
    assert store.notifications(first) == []
    assert not saved_path.exists()
    assert store.get(second, second_task["task_id"]) == second_task
    with pytest.raises(KeyError):
        store.message(first, task_id, "stale", "subagent", "finding", {})
    new = _start(store, first)
    assert new["task_id"] != task_id
    assert store.messages(first, new["task_id"])[0]["seq"] > notice["seq"]


def test_hundreds_of_tool_messages_never_touch_episodic_store(tasks, fake_embedder):
    store, first, _ = tasks
    manager = SessionManager(store.config, fake_embedder)
    episodic = manager.open_store(first)
    episodic.append("user", "Remember this")
    episodic.append("assistant", "Remembered")
    episodic.close()
    path = store.config.store_path(first)
    before = path.read_bytes()
    calls = fake_embedder.calls
    task_id = _start(store, first)["task_id"]
    for index in range(250):
        store.message(first, task_id, f"tool-{index}", "subagent", "tool",
                      {"summary": f"Tool output {index}"})
        store.update(first, task_id, progress=f"Tool {index}")
    store.notify(first, task_id, "notice", "Research is underway")
    assert path.read_bytes() == before
    assert fake_embedder.calls == calls
    assert not store.config.session_file(first, "turns.jsonl").exists()
    assert store.snapshot(first)["tasks"][0]["progress"] == "Tool 249"
