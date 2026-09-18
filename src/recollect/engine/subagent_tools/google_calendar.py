"""Google Calendar worker tools, backed by the connected account (issue #28).

The worker never holds credentials. Each call asks the local connection
service — its URL and key arrive in this process's environment — for a
short-lived access token, uses that token for exactly one Google request,
and discards it. No token is ever returned, printed, or logged, and no
Google answer crosses into the transcript without passing through here.

The environment variable names deliberately duplicate ``recollect.
connections``: the sandbox image carries no FastAPI, so this module must
not import that one. Both sides are pointed at each other by comment.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta

import httpx

CONNECTOR_ID = "google_calendar"
API = "https://www.googleapis.com/calendar/v3"
#: Matches the constants in recollect.connections (kept literal here because
#: the sandbox image cannot import that module).
URL_VARIABLE = "RECOLLECT_CONNECTIONS_URL"
KEY_VARIABLE = "RECOLLECT_CONNECTIONS_TOKEN"

_TIMEOUT = 20.0

#: A Google calendar id is an address or the literal "primary". Anything else
#: — a slash, a question mark, a dot-dot — must never reach the URL: httpx
#: decodes escapes and resolves dot segments, so an injected id could address
#: a calendar the caller never named.
_CALENDAR_ID = re.compile(r"[A-Za-z0-9@._~-]+")


def _calendar(tool: str, requested: str | None, access: dict):
    calendar = requested or access.get("calendar_id") or "primary"
    if not _CALENDAR_ID.fullmatch(calendar):
        return None, _document(tool, error=(
            f"{calendar!r} is not a usable calendar id; expected \"primary\" "
            "or an address like \"user@example.com\"."))
    return calendar, None


def _document(tool: str, **fields) -> str:
    return json.dumps({"tool": tool, **fields}, ensure_ascii=False)


async def _access(client, tool: str):
    """One short-lived access from the connection service, or an error document."""
    base, key = os.environ.get(URL_VARIABLE), os.environ.get(KEY_VARIABLE)
    if not base or not key:
        return None, _document(tool, error=(
            "No connection service is available to this worker; the service "
            "must be connected first."))
    try:
        response = await client.get(
            f"{base.rstrip('/')}/connectors/{CONNECTOR_ID}",
            headers={"Authorization": f"Bearer {key}"}, timeout=_TIMEOUT)
    except httpx.HTTPError as error:
        return None, _document(tool, error=(
            f"The connection service is unreachable ({type(error).__name__}). "
            "Report blocked; do not retry in a loop."))
    if response.status_code != 200:
        return None, _document(tool, error=(
            f"Google Calendar is not connected (HTTP {response.status_code}). "
            "Ask the user to connect it, then retry once."))
    return response.json(), None


def _refused(tool: str, response) -> str | None:
    if response.status_code in (401, 403):
        return _document(tool, error=(
            f"Google refused the request (HTTP {response.status_code}); the "
            "connection may have been revoked. Ask to reconnect."))
    if response.status_code == 404:
        return _document(tool, error=(
            "That calendar was not found on the connected account."))
    if response.status_code >= 300:
        # Status only: a third-party body has no business reaching a prompt.
        return _document(tool, error=(
            f"Google Calendar returned HTTP {response.status_code}."))
    return None


def _when(value: str) -> dict | None:
    """Google's time spec from an ISO date (all-day) or datetime string."""
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return {"date": value} if len(value) == 10 else {"dateTime": value}


async def _list_events(days_ahead: int = 7, max_results: int = 10,
                       calendar_id: str | None = None, *,
                       transport=None) -> str:
    """List upcoming events on the connected Google Calendar."""
    tool = "calendar_list_events"
    days = max(0, min(int(days_ahead), 365))
    now = datetime.now(UTC)
    async with httpx.AsyncClient(transport=transport,
                                 trust_env=False) as client:
        access, error = await _access(client, tool)
        if error is not None:
            return error
        calendar, rejected = _calendar(tool, calendar_id, access)
        if rejected is not None:
            return rejected
        try:
            response = await client.get(
                f"{API}/calendars/{calendar}/events",
                headers={"Authorization": f"Bearer {access['access_token']}"},
                params={"timeMin": now.isoformat(),
                        "timeMax": (now + timedelta(days=days)).isoformat(),
                        "singleEvents": "true", "orderBy": "startTime",
                        "maxResults": max(1, min(int(max_results), 100))},
                timeout=_TIMEOUT)
        except (httpx.HTTPError, ValueError) as failure:
            return _document(tool, error=(
                f"Google Calendar could not be reached "
                f"({type(failure).__name__})."))
        if (refused := _refused(tool, response)) is not None:
            return refused
        events = [{"id": event.get("id"), "summary": event.get("summary"),
                   "start": (event.get("start") or {}).get("dateTime")
                   or (event.get("start") or {}).get("date"),
                   "end": (event.get("end") or {}).get("dateTime")
                   or (event.get("end") or {}).get("date")}
                  for event in response.json().get("items", [])]
    return _document(tool, calendar_id=calendar, count=len(events),
                     events=events)


async def _create_event(summary: str, start: str, end: str,
                        calendar_id: str | None = None,
                        location: str = "", description: str = "", *,
                        transport=None) -> str:
    """Create one event on the connected Google Calendar."""
    tool = "calendar_create_event"
    if not summary.strip():
        return _document(tool, error="the event needs a summary")
    start_spec, end_spec = _when(start), _when(end)
    if start_spec is None or end_spec is None:
        return _document(tool, error=(
            "start and end must be ISO dates (2026-09-18) or datetimes "
            f"(2026-09-18T10:00:00+05:30); got start={start!r}, end={end!r}"))
    if ("dateTime" in start_spec) != ("dateTime" in end_spec):
        return _document(tool, error=(
            "start and end must both be all-day dates or both be datetimes"))
    async with httpx.AsyncClient(transport=transport,
                                 trust_env=False) as client:
        access, error = await _access(client, tool)
        if error is not None:
            return error
        calendar, rejected = _calendar(tool, calendar_id, access)
        if rejected is not None:
            return rejected
        body = {"summary": summary.strip(), "start": start_spec,
                "end": end_spec,
                **({"location": location} if location.strip() else {}),
                **({"description": description} if description.strip() else {})}
        try:
            response = await client.post(
                f"{API}/calendars/{calendar}/events",
                headers={"Authorization": f"Bearer {access['access_token']}"},
                json=body, timeout=_TIMEOUT)
        except httpx.HTTPError as failure:
            return _document(tool, error=(
                f"Google Calendar could not be reached "
                f"({type(failure).__name__})."))
        if (refused := _refused(tool, response)) is not None:
            return refused
        event = response.json()
    return _document(tool, event_id=event.get("id"),
                     summary=event.get("summary"),
                     start=event.get("start"), end=event.get("end"),
                     calendar_id=calendar, link=event.get("htmlLink"))


def register(mcp) -> None:
    """Expose the two calendar tools on a FastMCP server (host-side only)."""

    @mcp.tool()
    async def calendar_list_events(
            days_ahead: int = 7, max_results: int = 10,
            calendar_id: str | None = None) -> str:
        """Upcoming events on the user's connected Google Calendar: id, summary,
        start, end. Returns JSON; an error field means the connection is
        unavailable — report blocked, never invent events."""
        return await _list_events(days_ahead=days_ahead,
                                  max_results=max_results,
                                  calendar_id=calendar_id)

    @mcp.tool()
    async def calendar_create_event(
            summary: str, start: str, end: str,
            calendar_id: str | None = None, location: str = "",
            description: str = "") -> str:
        """Create one event on the user's connected Google Calendar. start and
        end are ISO dates (all-day) or ISO datetimes with offset. Returns the
        created event's id and link; an error field means nothing was created —
        report it, never claim success."""
        return await _create_event(
            summary, start, end, calendar_id=calendar_id,
            location=location, description=description)
