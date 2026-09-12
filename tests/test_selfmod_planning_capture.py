"""Guard the source-verified planning record against accidental truncation/drift."""

import hashlib
import json
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
STEM = "SELF_MODIFICATION_PLANNING_CONVERSATION"


def test_planning_capture_hashes_and_message_inventory():
    for line in (DOCS / f"{STEM}.sha256").read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((DOCS / name).read_bytes()).hexdigest() == digest

    capture = json.loads((DOCS / f"{STEM}.json").read_text(encoding="utf-8"))
    messages = capture["messages"]
    assert len(messages) == 15
    assert [m["role"] for m in messages] == [
        "user", "assistant", "assistant", "assistant", "user", "assistant",
        "user", "assistant", "user", "assistant", "user", "assistant",
        "user", "assistant", "user",
    ]
    assert [m["source_line"] for m in messages] == sorted(
        {m["source_line"] for m in messages}
    )
    assert sum(m["phase"] == "final_answer" for m in messages) == 6
    assert sum(m["phase"] == "commentary" for m in messages) == 2
    for sequence, message in enumerate(messages, 1):
        assert message["sequence"] == sequence
        body = message["text"].encode("utf-8")
        assert len(body) == message["utf8_bytes"]
        assert hashlib.sha256(body).hexdigest() == message["sha256"]
    assert messages[0]["text"] == (
        "Great. Before we begin implementing the harness, "
        "lets agree on its architecture"
    )
    assert messages[-1]["text"] == (
        "This is all good planning, but we were very detailed. I want you to "
        "store our turns for this section of planning verbatim then begin "
        "implementing the harness and your checks will be that this "
        "conversation was correctly captured."
    )


def test_readable_capture_preserves_every_message():
    capture = json.loads((DOCS / f"{STEM}.json").read_text(encoding="utf-8"))
    expected = (
        "# Self-modification harness: verbatim planning conversation\n\n"
        f"Source: local thread `{capture['source_thread_id']}`.\n\n"
        f"{capture['scope']}\n\n"
        "The JSON companion preserves exact message text and per-message "
        "SHA-256 values. Headings and separators below are editorial framing, "
        "not part of the messages. This capture is not a replacement or "
        "amendment of the registered experiment protocols.\n\n"
    )
    sections = []
    for message in capture["messages"]:
        phase = f" ({message['phase']})" if message["phase"] else ""
        sections.append(
            f"## {message['sequence']}. {message['role']}{phase}\n\n"
            f"{message['text']}\n"
        )
    expected += "\n---\n\n".join(sections)
    assert (DOCS / f"{STEM}.md").read_bytes() == expected.encode("utf-8")
