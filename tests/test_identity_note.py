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


def test_the_host_clock_zone_rides_along():
    # A live run booked the user's "1:30 pm" in UTC — five hours early —
    # because the worker could not know the host's zone. It travels in the
    # identity line, with an instruction to send only explicit offsets.
    note = identity_note("sess-9", "task-3")
    assert "host_tz=UTC" in note
    assert "explicit offsets" in note
