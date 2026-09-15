"""Whole-session native projection agreement with committed events."""

import json

import pytest

from recollect.selfmod.journal import IntegrityError
from recollect.selfmod.native_continuation import verify_projection


def row(seq, kind, data):
    return {"seq": seq, "type": kind + ".1", "data": json.dumps(data)}


def message(identity, role="assistant", **extra):
    return {"id": identity, "sessionID": "ses_a", "role": role, **extra}


def part(identity, message_id, **extra):
    return {"id": identity, "sessionID": "ses_a", "messageID": message_id,
            "type": "tool", **extra}


def history():
    output = "x" * 1000
    return [
        row(0, "session.created", {"info": {"id": "ses_a"}}),
        row(1, "message.updated", {"info": message("msg_1", "user")}),
        row(2, "message.part.updated", {"part": part("prt_1", "msg_1", type="text",
                                                     text="task")}),
        row(3, "message.updated", {"info": message("msg_2", finish=None)}),
        row(4, "message.part.updated", {"part": part("prt_2", "msg_2", state={
            "status": "completed", "output": output, "time": {"start": 1}})}),
        row(5, "message.updated", {"info": message("msg_2", finish="stop")}),
        # Pruning marks the old output compacted; the event log keeps row 4.
        row(6, "message.part.updated", {"part": part("prt_2", "msg_2", state={
            "status": "completed", "output": output,
            "time": {"start": 1, "compacted": 9}})}),
    ]


def projection():
    rows = history()
    return [
        {"info": json.loads(rows[5]["data"])["info"],
         "parts": [json.loads(rows[6]["data"])["part"]]},
        {"info": message("msg_1", "user"),
         "parts": [json.loads(rows[2]["data"])["part"]]},
    ]


def test_full_projection_including_pruned_parts_matches_events():
    assert verify_projection(history(), projection()) == 2


@pytest.mark.parametrize("fault", [
    "stale_part", "missing_message", "extra_message", "duplicate", "info_drift",
])
def test_projection_divergence_fails(fault):
    value = projection()
    if fault == "stale_part":
        value[0]["parts"][0]["state"]["time"].pop("compacted")
    elif fault == "missing_message":
        value.pop()
    elif fault == "extra_message":
        value.append({"info": message("msg_9"), "parts": []})
    elif fault == "duplicate":
        value.append(value[0])
    else:
        value[1]["info"]["role"] = "assistant"
    with pytest.raises(IntegrityError):
        verify_projection(history(), value)


@pytest.mark.parametrize("kind", ["message.removed", "message.part.removed",
                                  "session.deleted"])
def test_removal_events_are_not_supported(kind):
    rows = [*history(), row(7, kind, {"messageID": "msg_2", "partID": "prt_2"})]
    with pytest.raises(IntegrityError, match="removal"):
        verify_projection(rows, projection())


def test_committed_part_without_message_fails():
    rows = [*history(), row(7, "message.part.updated",
                            {"part": part("prt_9", "msg_missing")})]
    with pytest.raises(IntegrityError, match="lacks its message"):
        verify_projection(rows, projection())
