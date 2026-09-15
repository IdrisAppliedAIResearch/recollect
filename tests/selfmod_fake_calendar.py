"""Deterministic Google Calendar events API stand-in for provider fixtures."""

import json
from urllib.parse import unquote

import httpx


class FakeCalendar:
    def __init__(self, calendar_id="test-calendar@example.invalid", page_size=2):
        self.calendar_id, self.page_size = calendar_id, page_size
        self.events, self.requests = {}, []
        self.faults = []  # callables(request) -> Response | Exception | None

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request):
        self.requests.append(request)
        assert request.headers["authorization"].startswith("Bearer ")
        for fault in list(self.faults):
            outcome = fault(request)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                return outcome
        prefix = "/calendar/v3/calendars/"
        path = request.url.path
        assert path.startswith(prefix)
        calendar, _, rest = path[len(prefix):].partition("/events")
        assert unquote(calendar) == self.calendar_id
        event_id = rest.lstrip("/")
        if request.method == "GET" and not event_id:
            return self._list(request)
        if request.method == "GET":
            event = self.events.get(event_id)
            return (httpx.Response(200, json=event) if event
                    else httpx.Response(404, json={"error": {"code": 404}}))
        if request.method == "POST":
            return self.insert(json.loads(request.content))
        if request.method == "DELETE":
            if event_id not in self.events:
                return httpx.Response(410)
            self.events[event_id]["status"] = "cancelled"
            return httpx.Response(204)
        return httpx.Response(405)

    def insert(self, body):
        event_id = body["id"]
        if event_id in self.events:
            return httpx.Response(409, json={"error": {"code": 409,
                                                       "message": "duplicate"}})
        event = {**body, "status": "confirmed",
                 "htmlLink": "https://calendar.example.invalid/" + event_id}
        self.events[event_id] = event
        return httpx.Response(200, json=event)

    def _list(self, request):
        query = request.url.params.get("q", "")
        items = [e for e in self.events.values()
                 if query in e.get("summary", "") and e.get("status") != "cancelled"]
        start = int(request.url.params.get("pageToken") or 0)
        page = items[start:start + self.page_size]
        payload = {"items": page}
        if start + self.page_size < len(items):
            payload["nextPageToken"] = str(start + self.page_size)
        return httpx.Response(200, json=payload)
