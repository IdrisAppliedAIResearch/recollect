"""Worker tools for the generic timer: book, list, cancel.

The booking travels over the connection relay to the host scheduler; the
payload ``{session_id, task_id, text}`` is the *deliverer's* contract,
not this module's — and an optional ``channel`` names where the host may
also send it at delivery time. Sending never happens here: a tool
answers a call, and a due job may fire with no worker alive.

The ids must be copied from the delegation instruction's
``[recollect identity]`` line: the tool process env is shared across
chats, and self-asserted ids route notices — they authorize nothing.

The environment variable names deliberately duplicate ``recollect.
connections``: the sandbox image carries no FastAPI, so this module must
not import that one.
"""

from __future__ import annotations

import json
import os
import re

import httpx

#: Mirrors of the constants in recollect.connections (literal here because
#: the sandbox image cannot import that module).
URL_VARIABLE = "RECOLLECT_CONNECTIONS_URL"
KEY_VARIABLE = "RECOLLECT_CONNECTIONS_TOKEN"

_TIMEOUT = 20.0
_TEXT_MAX = 1000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_JOB_ID = re.compile(r"[0-9a-f]{8,64}")


def _document(tool: str, **fields) -> str:
    return json.dumps({"tool": tool, **fields}, ensure_ascii=False)


async def _relay(tool: str, method: str, path: str, body=None, *,
                 transport=None) -> str:
    """One authenticated relay call, answered as a result document."""
    base, key = os.environ.get(URL_VARIABLE), os.environ.get(KEY_VARIABLE)
    if not base or not key:
        return _document(tool, error=(
            "No connection service is available to this worker; scheduling "
            "is offline. Report it; do not pretend the job was booked."))
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False) \
                as client:
            response = await client.request(
                method, f"{base.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {key}"},
                json=body, timeout=_TIMEOUT)
    except httpx.HTTPError as error:
        return _document(tool, error=(
            f"The connection service is unreachable "
            f"({type(error).__name__}). Report blocked; do not retry in a "
            "loop."))
    if response.status_code in (400, 404, 409):
        # The relay's own reason (a real FastAPI detail string) teaches the
        # model what to fix; a bare error page gets the status only.
        try:
            value = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            value = None
        if isinstance(value, dict) and isinstance(value.get("detail"), str):
            return _document(tool, error=value["detail"])
    if response.status_code >= 300:
        return _document(tool, error=(
            f"The connection service refused the request (HTTP "
            f"{response.status_code})."))
    try:
        value = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        # A 2xx that is not JSON (a proxy-rewritten page) still gets the
        # result-document contract: an answer, never an exception.
        return _document(tool, error=(
            f"The connection service answered without a result (HTTP "
            f"{response.status_code})."))
    return _document(tool, result=value)


def _identity(tool: str, session_id: str, task_id: str):
    if not _ID.fullmatch(session_id or "") or not _ID.fullmatch(task_id or ""):
        return _document(tool, error=(
            "session_id and task_id are required: copy them from the "
            "[recollect identity] line of your instruction. If it has no "
            "task_id, this conversation cannot book notices — report "
            "blocked instead of inventing ids."))
    return None


async def _book(when: str, text: str, session_id: str = "", task_id: str = "",
                channel: str = "", *, transport=None) -> str:
    tool = "schedule_at"
    if not text.strip() or len(text) > _TEXT_MAX:
        return _document(tool, error=(
            f"text is required, up to {_TEXT_MAX} characters"))
    if (bad := _identity(tool, session_id, task_id)) is not None:
        return bad
    if channel and not _ID.fullmatch(channel):
        return _document(tool, error=(
            f"{channel!r} is not a usable channel name"))
    payload = {"session_id": session_id, "task_id": task_id,
               "text": text.strip()}
    if channel:
        payload["channel"] = channel
    return await _relay(tool, "POST", "/schedules",
                        {"due_at": when, "payload": payload},
                        transport=transport)


async def _list(*, transport=None) -> str:
    return await _relay("list_scheduled", "GET", "/schedules",
                        transport=transport)


async def _cancel(job_id: str, *, transport=None) -> str:
    tool = "cancel_scheduled"
    if not _JOB_ID.fullmatch(job_id or ""):
        return _document(tool, error=(
            "job_id must be the hex id schedule_at returned"))
    return await _relay(tool, "DELETE", f"/schedules/{job_id}",
                        transport=transport)


def register(mcp) -> None:
    """Expose the three timer tools on a FastMCP server (sandbox-side)."""

    @mcp.tool()
    async def schedule_at(when: str, text: str, session_id: str = "",
                          task_id: str = "", channel: str = "") -> str:
        """Book one job for a timezone-aware ISO moment (2026-09-18T08:30:00,
        never a naive time). At that moment the host posts text as a
        reminder notice into the conversation named by session_id/task_id —
        copy both from the [recollect identity] line of your instruction;
        inventing or omitting them fails the booking. A moment already past
        is delivered immediately. Optional channel sends the text off-device
        too, if the user configured one (list them first; the Notice posts
        either way). Returns the booked job with its id, or an error to
        report — never claim a booking without an id."""
        return await _book(when, text, session_id, task_id, channel)

    @mcp.tool()
    async def list_scheduled() -> str:
        """Every job currently waiting: id, due_at, payload. Returns JSON;
        an error field means the connection service is unreachable."""
        return await _list()

    @mcp.tool()
    async def cancel_scheduled(job_id: str) -> str:
        """Cancel a waiting job by the id schedule_at returned. A job that
        already fired or canceled comes back as an error — report it, do
        not retry."""
        return await _cancel(job_id)
