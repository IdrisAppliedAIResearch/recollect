"""The main chat prompt: tagged sections, with task rules inside their sections."""

import re
from dataclasses import fields
from datetime import date

import pytest

from recollect import system_prompt
from recollect.config import RecollectConfig

DAY = date(2026, 9, 8)
TAGS = ("identity", "instructions", "memory", "tools")


def sections(prompt):
    found = re.findall(r"<(\w+)>\n(.*?)\n</\1>", prompt, flags=re.S)
    assert [tag for tag, _ in found] == list(TAGS)
    assert "\n\n".join(f"<{t}>\n{b}\n</{t}>" for t, b in found) == prompt
    return dict(found)


def test_every_mode_has_exactly_the_four_sections_in_order():
    for kwargs in ({}, {"input_mode": "voice"}, {"task_mode": True},
                   {"task_mode": True, "follow_up": "operation_returned"}):
        sections(system_prompt.build(DAY, **kwargs))


def test_task_rules_live_inside_memory_and_tools_only_in_task_mode():
    plain = sections(system_prompt.build(DAY))
    task = sections(system_prompt.build(DAY, task_mode=True))
    assert system_prompt.TASK_MEMORY not in plain["memory"]
    assert task["memory"] == system_prompt.MEMORY + "\n\n" + system_prompt.TASK_MEMORY
    assert plain["tools"] == system_prompt.TOOLS
    assert task["tools"] == system_prompt.TASK_TOOLS
    assert system_prompt.TASK_INSTRUCTIONS in task["instructions"]
    assert system_prompt.TASK_INSTRUCTIONS not in plain["instructions"]


def test_voice_and_date_are_instructions():
    voice = sections(system_prompt.build(DAY, input_mode="voice"))
    assert system_prompt.VOICE_INSTRUCTIONS in voice["instructions"]
    assert "Current date (UTC): 2026-09-08." in voice["instructions"]
    assert system_prompt.VOICE_INSTRUCTIONS not in system_prompt.build(DAY)


@pytest.mark.parametrize("follow_up", ["work_not_started", "operation_returned"])
def test_follow_up_notes_close_the_tools_section(follow_up):
    tools = sections(system_prompt.build(DAY, task_mode=True, follow_up=follow_up))
    assert tools["tools"].endswith(system_prompt.FOLLOW_UPS[follow_up])
    with pytest.raises(ValueError):
        system_prompt.build(DAY, follow_up=follow_up)


def test_prompt_is_not_a_configuration_override():
    assert "system_prompt" not in {f.name for f in fields(RecollectConfig)}
