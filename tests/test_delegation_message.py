"""The worker's first message and later updates into its live session."""

from recollect.engine.sandbox.runner import OpenCodeRunner

DATA = "<request>\nr\n</request>\n\n<brief>\nb\n</brief>\n\n<context>\nc\n</context>"


def test_instructions_follow_the_data_as_numbered_steps():
    message = OpenCodeRunner._delegation_message(DATA, "focused", 3, "start:abc")
    assert message.startswith(
        DATA + "\n\n<instructions>\n1. Load the recollect-reporting")
    assert message.endswith("</instructions>")
    assert "revision 3 and related message ID start:abc" in message
    assert "Keep it quick" in message and "Go deep" not in message
    steps = message.split("<instructions>\n", 1)[1].split("\n</instructions>")[0]
    assert [line.split(".", 1)[0] for line in steps.splitlines()] == [
        "1", "2", "3", "4", "5", "6"]


def test_deep_effort_changes_only_the_pace_step():
    focused = OpenCodeRunner._delegation_message(DATA, "focused", 1, "m")
    deep = OpenCodeRunner._delegation_message(DATA, "deep", 1, "m")
    assert "Go deep" in deep and "Keep it quick" not in deep
    assert len(focused.splitlines()) == len(deep.splitlines())


def test_only_steers_ask_for_a_fresh_acknowledgment():
    steer = OpenCodeRunner._update_message("Add warranty", 2, "msg-2", acknowledge=True)
    assert steer == (
        '<update revision="2" message_id="msg-2">\nAdd warranty\n</update>\n'
        "Report accepted with this revision and message ID."
    )
    nudge = OpenCodeRunner._update_message("Continue.", 2, "msg-2")
    assert nudge == '<update revision="2" message_id="msg-2">\nContinue.\n</update>'
