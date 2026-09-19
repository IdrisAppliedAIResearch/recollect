"""Identity travels in the instruction, where the worker can copy it."""

from recollect.engine.sandbox.runner import identity_note


def test_both_ids_appear_verbatim_under_one_banner():
    note = identity_note("sess-9", "task-3")
    assert note.startswith("[recollect identity]")
    assert "session_id=sess-9 task_id=task-3" in note
    assert "never invent" in note


def test_a_chat_foreground_has_session_only():
    note = identity_note("sess-9")
    assert "session_id=sess-9" in note
    assert "task_id=" not in note


def test_no_session_no_note():
    assert identity_note("") == ""
