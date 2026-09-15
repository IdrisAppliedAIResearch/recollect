"""Frozen in-process Google Calendar events API fixture for candidate evaluation.

This is the independent evaluator's provider, never reachable from the live
origin: a broker whose origin is not Google's uses it through an httpx mock
transport. It keeps every request and event, answers unknown shapes with API-like
errors instead of assertions, and injects the registered failure scenarios.
"""

import json
from urllib.parse import unquote

import httpx

EVENTS_PREFIX = "/calendar/v3/calendars/"


class FixtureCalendar:
    def __init__(self, calendar_id="evaluation-calendar@fixture.invalid",
                 page_size=50):
        self.calendar_id, self.page_size = calendar_id, page_size
        self.events, self.requests = {}, []
        self.faults = []  # callables(request, calendar) -> Response | Exception | None

    def transport(self):
        return httpx.MockTransport(self.handle)

    def seed(self, event):
        """An unrelated pre-existing event that evaluation must leave unchanged."""
        self.events[event["id"]] = {**event, "status": "confirmed"}

    def handle(self, request):
        self.requests.append(request)
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return _error(401)
        for fault in list(self.faults):
            outcome = fault(request, self)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                return outcome
        path = request.url.path
        calendar, marker, rest = path.removeprefix(EVENTS_PREFIX).partition("/events")
        if (not path.startswith(EVENTS_PREFIX) or not marker
                or unquote(calendar) != self.calendar_id):
            return _error(404)
        event_id = rest.lstrip("/")
        if request.method == "GET" and not event_id:
            return self._list(request)
        if request.method == "GET":
            event = self.events.get(event_id)
            return httpx.Response(200, json=event) if event else _error(404)
        if request.method == "POST" and not event_id:
            try:
                body = json.loads(request.content)
            except ValueError:
                return _error(400)
            return self.insert(body)
        if request.method == "DELETE" and event_id:
            if event_id not in self.events:
                return httpx.Response(410)
            self.events[event_id]["status"] = "cancelled"
            return httpx.Response(204)
        return _error(405)

    def insert(self, body):
        if (type(body) is not dict or type(body.get("id")) is not str
                or not isinstance(body.get("start"), dict)
                or not isinstance(body.get("end"), dict)):
            return _error(400)
        if body["id"] in self.events:
            return _error(409)
        event = {**body, "status": "confirmed",
                 "htmlLink": "https://calendar.fixture.invalid/event/" + body["id"]}
        self.events[body["id"]] = event
        return httpx.Response(200, json=event)

    def _list(self, request):
        query = request.url.params.get("q", "")
        items = [e for e in self.events.values()
                 if query in (e.get("summary") or "")
                 and e.get("status") != "cancelled"]
        start = int(request.url.params.get("pageToken") or 0)
        payload = {"items": items[start:start + self.page_size]}
        if start + self.page_size < len(items):
            payload["nextPageToken"] = str(start + self.page_size)
        return httpx.Response(200, json=payload)


def _error(code):
    return httpx.Response(code, json={"error": {"code": code}})


def _is_insert(request):
    return request.method == "POST" and request.url.path.endswith("/events")


def fail_inserts(status):
    """Every create attempt is answered with ``status`` (403, 400 or 500)."""

    def fault(request, calendar):
        return _error(status) if _is_insert(request) else None

    return fault


def lose_first_insert_response():
    """The first create succeeds at the provider, then its response is lost."""
    state = {"lost": False}

    def fault(request, calendar):
        if not _is_insert(request) or state["lost"]:
            return None
        state["lost"] = True
        calendar.insert(json.loads(request.content))
        return httpx.RemoteProtocolError("fixture: response lost after creation")

    return fault
