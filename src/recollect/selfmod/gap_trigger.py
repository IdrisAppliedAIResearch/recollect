"""Structured capability-gap parsing from A's durable task reports.

The harness never manufactures a gap from keywords in the request. It accepts
only a structured block that A itself sent through its generic reporting tool,
as a durable ``subagent`` ``blocked`` task message. Binding comes from that
durable record (task, revision, copied related message ID), never from
identities the model writes inside its own report. Absence or malformed
structure returns ``None``.
"""

import json
import re

from .journal import IntegrityError

FENCE = re.compile(r"```capability_gap\s*\n(\{.*?\})\s*\n```", re.DOTALL)
FIELDS = {"type", "missing_capability", "attempted", "modification_request"}


def parse_gap_report(message):
    """Return A's structured report bound to its durable record, or None."""
    if type(message) is not dict or message.get("direction") != "subagent":
        raise IntegrityError("Gap reports must come from A's durable subagent report")
    payload = message.get("payload")
    if message.get("kind") != "blocked" or type(payload) is not dict:
        return None
    text = payload.get("text")
    if type(text) is not str:
        return None
    blocks = FENCE.findall(text)
    if len(blocks) != 1:
        return None
    try:
        report = json.loads(blocks[0])
    except ValueError:
        return None
    if (type(report) is not dict or set(report) != FIELDS
            or report["type"] != "capability_gap"
            or any(type(report[k]) is not str or not report[k].strip()
                   for k in ("missing_capability", "modification_request"))
            or type(report["attempted"]) is not list
            or any(type(item) is not str for item in report["attempted"])):
        return None
    # The coordinator persists A's copied related message ID as ``reply_to``.
    return {**report, "task_id": message.get("task_id"),
            "message_id": message.get("message_id"),
            "revision": message.get("revision"),
            "related_message_id": payload.get("reply_to")}
