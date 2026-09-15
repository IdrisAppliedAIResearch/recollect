"""Validate a native auto-continuation from committed events, not summary prose."""

import json

from .journal import IntegrityError, encode


def verify_projection(rows, projection):
    """Rebuild every committed message/part and require exact projection equality.

    This covers the whole session, including compacted/pruned tool parts, not
    just the returned reply. Removal events fail: native history is append-only.
    """
    messages, parts = {}, {}
    for row in rows:
        kind = row["type"].removesuffix(".1")
        data = json.loads(row["data"])
        if kind == "message.updated":
            messages[data["info"]["id"]] = data["info"]
        elif kind == "message.part.updated":
            parts[data["part"]["id"]] = data["part"]
        elif kind in {"message.removed", "message.part.removed", "session.deleted"}:
            raise IntegrityError("Native history removal is not supported")
    expected = {
        identity: {"info": info, "parts": sorted(
            (p for p in parts.values() if p["messageID"] == identity),
            key=lambda p: p["id"])}
        for identity, info in messages.items()
    }
    if any(p["messageID"] not in messages for p in parts.values()):
        raise IntegrityError("Committed native part lacks its message")
    observed = {}
    for message in projection:
        identity = message["info"]["id"]
        if identity in observed:
            raise IntegrityError("Duplicate native projection message")
        observed[identity] = {"info": message["info"], "parts": sorted(
            message["parts"], key=lambda p: p["id"])}
    if encode(dict(sorted(observed.items()))) != encode(dict(sorted(expected.items()))):
        raise IntegrityError("Native projection differs from committed events")
    return len(observed)


def verify_reply(rows, after, submitted, result, authority, text, agent, model):
    messages, parts, created, updated = {}, {}, {}, {}
    for row in rows:
        kind = row["type"].removesuffix(".1")
        data = json.loads(row["data"])
        if kind == "message.updated":
            info = data["info"]
            identity = info["id"]
            previous = messages.get(identity)
            if previous is not None and any(previous.get(k) != info.get(k) for k in (
                "id", "sessionID", "role", "parentID", "agent", "model",
                "modelID", "providerID", "summary", "system",
            )):
                raise IntegrityError("Native message identity changed")
            if info.get("role") == "user" and (
                info.get("system") not in (None, authority.decode("utf-8"))
            ):
                raise IntegrityError("Native user turn changed frozen authority")
            if identity == submitted and (
                info.get("role") != "user" or info.get("agent") != agent
                or info.get("model") != model
                or info.get("system") != authority.decode("utf-8")
            ):
                raise IntegrityError("Native submitted identity changed authority")
            created.setdefault(identity, row["seq"])
            updated[identity] = row["seq"]
            messages[identity] = info
        elif kind == "message.part.updated":
            part = data["part"]
            previous = parts.get(part["id"])
            if previous is not None and any(previous[k] != part[k] for k in (
                "id", "sessionID", "messageID", "type",
            )):
                raise IntegrityError("Native message part identity changed")
            if part["messageID"] == submitted and (
                part.get("type") != "text" or part.get("text") != text
                or part.get("synthetic", False) is not False
                or part.get("ignored", False) is not False
            ):
                raise IntegrityError("Native original task content changed")
            parts[part["id"]] = part
        elif kind in {"message.removed", "message.part.removed", "session.deleted"}:
            raise IntegrityError("Native invocation removed history")

    def message_parts(identity):
        return [p for p in parts.values() if p["messageID"] == identity]

    original = messages.get(submitted, {})
    original_parts = message_parts(submitted)
    if (original.get("role") != "user"
            or created.get(submitted, -1) <= after
            or original.get("agent") != agent or original.get("model") != model
            or original.get("system") != authority.decode("utf-8")
            or len(original_parts) != 1 or original_parts[0].get("type") != "text"
            or original_parts[0].get("text") != text
            or original_parts[0].get("synthetic", False) is not False):
        raise IntegrityError("Submitted native task missing from durable history")
    users = sorted((i for i in messages if messages[i].get("role") == "user"
                    and created[i] > after),
                   key=created.__getitem__)
    if not users or users[0] != submitted:
        raise IntegrityError("Foreign user turn in native invocation")
    parent, compacting = submitted, None
    for identity in users[1:]:
        info, values = messages[identity], message_parts(identity)
        if info.get("agent") != original.get("agent") or (
            info.get("model") != original.get("model")
        ):
            raise IntegrityError("Native continuation changed agent/model")
        if (len(values) == 1 and values[0].get("type") == "compaction"
                and values[0].get("auto") is True and compacting is None):
            compacting = identity
            continue
        summaries = [m for m in messages.values()
                     if m.get("parentID") == compacting and m.get("summary") is True
                     and m.get("role") == "assistant"
                     and m.get("finish") == "stop" and not m.get("error")
                     and m.get("time", {}).get("completed")
                     and created[compacting] < created[m["id"]]
                     and updated[m["id"]] < created[identity]]
        if (compacting is None or len(summaries) != 1 or not values
                or any(p.get("type") != "text" or p.get("synthetic") is not True
                       for p in values)):
            raise IntegrityError("Unproven native automatic continuation")
        parent, compacting = identity, None
    final = result["info"]
    if (compacting is not None or final.get("parentID") != parent
            or created.get(final["id"], -1) <= created[parent]
            or final.get("role") != "assistant" or final.get("agent") != agent
            or final.get("modelID") != model["modelID"]
            or final.get("providerID") != model["providerID"]):
        raise IntegrityError("Native reply has a foreign continuation parent")
    if messages.get(final["id"]) != final or encode(sorted(
        message_parts(final["id"]), key=lambda p: p["id"],
    )) != encode(sorted(result["parts"], key=lambda p: p["id"])):
        raise IntegrityError("Native reply differs from committed projection")
