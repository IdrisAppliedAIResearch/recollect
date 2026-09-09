"""Verified file delivery and operational conversation memory regressions."""

from dataclasses import replace
from pathlib import Path

import pytest

from recollect.task_replies import task_question
from recollect.task_store import TaskStore
from tests import test_task_chat as chat_tests
from tests import test_task_store as store_tests
from tests.test_subagent import _episode_rows
from tests.test_task_chat import chat, seed_task, tool
from tests.test_task_store import _start

make_task_state = chat_tests.make_task_state
tasks = store_tests.tasks
workspace = store_tests.workspace


def test_downloads_collision_versions_and_restart_are_safe(tasks, workspace, tmp_path):
    store, session, _ = tasks
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "profile.md").write_text("User's original")
    store = TaskStore(replace(store.config, downloads_dir=downloads))
    task = _start(store, session)
    source = workspace / "profile.md"
    source.write_text("First version")
    first = store.export_workspace(session, task["task_id"], workspace)[0]
    assert Path(first["download_path"]).name == "profile (2).md"
    assert Path(first["download_path"]).read_text() == "First version"
    assert (downloads / "profile.md").read_text() == "User's original"
    reopened = TaskStore(store.config)
    assert reopened.export_workspace(session, task["task_id"], workspace) == [first]
    assert len(list(downloads.iterdir())) == 2
    source.write_text("Second version")
    second = reopened.export_workspace(session, task["task_id"], workspace)[0]
    assert second["version"] == 2
    assert Path(second["download_path"]).read_text() == "Second version"
    assert Path(first["download_path"]).read_text() == "First version"


def test_failed_download_keeps_verified_archive_and_can_retry(
    tasks, workspace, tmp_path
):
    store, session, _ = tasks
    downloads = tmp_path / "Downloads"
    downloads.write_text("Blocks directory creation")
    store = TaskStore(replace(store.config, downloads_dir=downloads))
    task = _start(store, session)
    (workspace / "report.md").write_text("Saved report")
    item = store.export_workspace(session, task["task_id"], workspace)[0]
    assert item["download_error"]
    assert "download_path" not in item
    saved = store.artifact(session, task["task_id"], item["artifact_id"])
    assert Path(saved["path"]).read_text() == "Saved report"
    downloads.unlink()
    retried = store.deliver_artifacts(session, task["task_id"], [item])[0]
    assert Path(retried["download_path"]).read_text() == "Saved report"
    assert "download_error" not in retried


@pytest.mark.parametrize(
    "message",
    [
        "What's the relative path for that file?",
        "Where can I find my document?",
        "How do I download that file?",
    ],
)
async def test_file_location_uses_records_without_model_or_memory(
    make_task_state, message
):
    state = make_task_state([])
    session = state.sessions.create_session().session_id
    task = seed_task(state, session)
    workspace = state.config.data_dir.parent / "file-work"
    workspace.mkdir()
    (workspace / "profile.md").write_text("The actual file")
    item = state.task_store.export_workspace(session, task["task_id"], workspace)[0]
    events = await chat(state, session, message)
    assert events["done"]["committed"] is False
    assert item["artifact_id"] in events["token"]["text"]
    assert "profile.md" in events["token"]["text"]
    assert state.generator.calls == []
    assert _episode_rows(state, session) == []


async def test_status_misclassification_cannot_save_known_progress_question(
    make_task_state,
):
    state = make_task_state(
        [tool("task_reply", text="Still working.", status_only=False)]
    )
    session = state.sessions.create_session().session_id
    seed_task(state, session)
    events = await chat(state, session, "How is the research going?")
    assert not events["done"]["committed"]
    assert _episode_rows(state, session) == []


async def test_mixed_delegation_retains_fact_without_work_acknowledgment(
    make_task_state,
):
    state = make_task_state(
        [
            tool(
                "run_subagent",
                task="Research enzymes",
                effort="focused",
                memory_reply="You teach biology.",
            ),
        ],
        ["I have started the enzyme research."],
    )
    session = state.sessions.create_session().session_id
    events = await chat(state, session, "I teach biology; research enzymes.")
    assert events["done"]["committed"]
    row = _episode_rows(state, session)[0]
    assert row["assistant_message"] == "You teach biology."
    assert "I teach biology" in row["user_message"]
    assert events["token"]["text"] == "I have started the enzyme research."
    assert events["done"]["generation"]["memory_response_text"] == "You teach biology."


def test_mixed_message_is_not_treated_as_a_pure_operational_question():
    assert task_question("How is it going? My budget is $500.") is None
    assert task_question("Where is my document? Explain its conclusion.") is None


@pytest.mark.parametrize("memory", [None, "", "   "])
async def test_false_status_flag_alone_cannot_ingest_task_clarification(
    make_task_state, memory,
):
    state = make_task_state(
        [
            tool(
                "task_reply",
                text="What scope should the document cover?",
                status_only=False,
                memory_reply=memory,
            )
        ]
    )
    session = state.sessions.create_session().session_id
    events = await chat(state, session, "Please create a document about enzymes.")
    assert not events["done"]["committed"]
    assert _episode_rows(state, session) == []


async def test_substantive_direct_answer_is_still_remembered(make_task_state):
    answer = "Enzymes catalyze biochemical reactions."
    state = make_task_state(
        [
            tool(
                "task_reply",
                text=answer,
                status_only=False,
                memory_reply=answer,
            )
        ]
    )
    session = state.sessions.create_session().session_id
    events = await chat(state, session, "What are enzymes?")
    assert events["done"]["committed"]
    assert _episode_rows(state, session)[0]["assistant_message"] == answer
