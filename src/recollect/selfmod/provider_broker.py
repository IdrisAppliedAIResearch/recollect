"""Credentialed provider transport with scope, lifecycle gates and attribution audit.

The broker is a trusted host transport, not a calendar capability: it forwards
only exact Google Calendar event routes that a principal's frozen policy allows,
attaches a host-held credential, and journals a redacted, attribution-bound
record of every operation. A and serving B receive the same worker access; the
target action's gate is controlled by the harness lifecycle. Verifier access is
read-only, cleanup is limited to verified event IDs, and fixture principals can
never reach a live transport. No credential value is ever recorded.
"""

import asyncio
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from .contracts import File, Snapshot
from .journal import IntegrityError, encode, sha256

LIVE_ORIGIN = "https://www.googleapis.com"
MAX_BODY_BYTES = 1024 * 1024
MAX_MUTATION_ATTEMPTS = 3
EVENT_FIELDS = ("id", "summary", "start", "end", "attendees", "htmlLink", "status")
LIST_QUERY = {"q", "timeMin", "timeMax", "singleEvents", "pageToken", "showDeleted"}
PRINCIPALS = {"worker", "verifier", "cleanup", "fixture"}
# Frozen transient classification: only these are transport observations.
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ProviderPolicy:
    calendar_id: str
    request_timeout_s: float
    origin: str = LIVE_ORIGIN

    def __post_init__(self):
        if (type(self.calendar_id) is not str or not self.calendar_id
                or len(self.calendar_id) > 256
                or any(c.isspace() for c in self.calendar_id)):
            raise ValueError("Freeze the private test calendar identifier")
        if type(self.request_timeout_s) not in {int, float} or not (
                0 < self.request_timeout_s <= 120):
            raise ValueError("Freeze a bounded per-request provider timeout")
        if not re.fullmatch(r"https://[a-z0-9.-]+(:[0-9]+)?", self.origin):
            raise ValueError("Provider origin must be an explicit HTTPS origin")

    @property
    def events_path(self):
        return f"/calendar/v3/calendars/{quote(self.calendar_id, safe='')}/events"


@dataclass(frozen=True, eq=False)
class Capability:
    principal: str
    token: str = field(repr=False)
    task_id: str | None = None
    action_id: str | None = None
    dedup_id: str | None = None
    routing_epoch: int | None = None
    serving_digest: str | None = None
    invocation_id: str | None = None


def valid_dedup_id(value):
    """Google Calendar client event IDs: base32hex characters, 5 to 1024 long."""
    return type(value) is str and bool(re.fullmatch(r"[a-v0-9]{5,1024}", value))


def event_fields(payload):
    if type(payload) is not dict:
        return None
    return {key: payload.get(key) for key in EVENT_FIELDS if key in payload}


class ProviderBroker:
    """Host-owned; workers reach it only through their minted capability."""

    def __init__(self, policy, journal, *, credential, transport, live=True,
                 clock=time.monotonic_ns):
        if type(policy) is not ProviderPolicy or not callable(credential):
            raise ValueError("Freeze a provider policy and trusted credential source")
        if not isinstance(transport, httpx.AsyncBaseTransport):
            raise ValueError("Supply an explicit provider transport")
        self.policy, self._journal, self._credential = policy, journal, credential
        self.live, self._clock = live, clock
        self._client = httpx.AsyncClient(
            base_url=policy.origin, transport=transport, trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(policy.request_timeout_s),
        )
        self._capabilities = {}
        self._gates = set()
        self._attempts = {}
        self._verified_events = set()
        self._lock = asyncio.Lock()

    def issue(self, principal, **binding):
        if principal not in PRINCIPALS:
            raise ValueError("Unknown provider principal")
        if principal == "fixture" and self.live:
            raise IntegrityError("Fixture principals cannot use a live broker")
        if principal == "worker" and not valid_dedup_id(binding.get("dedup_id")):
            raise ValueError("Worker actions require a stable provider dedup identity")
        capability = Capability(principal, secrets.token_urlsafe(32), **binding)
        self._capabilities[capability.token] = capability
        self._record("issued", {"principal": principal, **{
            k: v for k, v in binding.items() if k != "token"}})
        return capability

    def open_gate(self, action_id, reason):
        self._gates.add(action_id)
        self._record("gate_opened", {"action_id": action_id, "reason": reason})

    def close_gate(self, action_id, reason):
        self._gates.discard(action_id)
        self._record("gate_closed", {"action_id": action_id, "reason": reason})

    def allow_cleanup(self, event_id):
        """Only independently verified experiment events may be deleted."""
        self._verified_events.add(event_id)
        self._record("cleanup_allowed", {"event_id": event_id})

    def _record(self, kind, data, files=()):
        return self._journal.append("provider_" + kind, data, Snapshot(tuple(files)))

    def _authorize(self, capability, method, path, params, body):
        stored = self._capabilities.get(getattr(capability, "token", None))
        if stored is not capability:
            raise IntegrityError("Unknown or forged provider capability")
        events = self.policy.events_path
        item = re.fullmatch(re.escape(events) + r"/([A-Za-z0-9_-]{1,1024})", path)
        principal = capability.principal
        if method == "GET" and path == events:
            if set(params) - LIST_QUERY:
                raise IntegrityError("Provider list query field is not allowed")
            return None
        if method == "GET" and item:
            if params:
                raise IntegrityError("Provider read accepts no query")
            return None
        if principal == "worker" and method == "POST" and path == events:
            if params:
                raise IntegrityError("Provider insert accepts no query")
            if capability.action_id not in self._gates:
                raise IntegrityError("Target action gate is closed")
            if type(body) is not dict or body.get("id") != capability.dedup_id:
                raise IntegrityError("Insert must carry the frozen dedup identity")
            return "mutation"
        if principal == "cleanup" and method == "DELETE" and item:
            if item[1] not in self._verified_events:
                raise IntegrityError("Cleanup is limited to verified experiment events")
            return "cleanup"
        raise IntegrityError("Provider operation is outside the principal's policy")

    async def request(self, capability, method, path, *, params=None, body=None,
                      phase="original"):
        params = dict(params or {})
        if phase not in {"original", "replay"}:
            raise ValueError("Mutation phase is original or replay")
        raw = encode(body) if body is not None else b""
        if len(raw) > MAX_BODY_BYTES:
            raise IntegrityError("Provider request body exceeds bound")
        async with self._lock:
            kind = self._authorize(capability, method, path, params, body)
            if kind == "mutation":
                key = (capability.action_id, phase)
                if self._attempts.get(key, 0) >= MAX_MUTATION_ATTEMPTS:
                    raise IntegrityError("Mutation dispatch attempts exhausted")
                self._attempts[key] = self._attempts.get(key, 0) + 1
            attempt = self._attempts.get((capability.action_id, phase))
        context = {
            "principal": capability.principal, "task_id": capability.task_id,
            "action_id": capability.action_id, "dedup_id": capability.dedup_id,
            "routing_epoch": capability.routing_epoch,
            "serving_digest": capability.serving_digest,
            "invocation_id": capability.invocation_id, "method": method,
            "path_sha256": sha256(path.encode()), "params": params,
            "kind": kind or "read", "phase": phase if kind == "mutation" else None,
            "attempt": attempt if kind == "mutation" else None,
            "request_body_sha256": sha256(raw), "request_fields": event_fields(body),
            "dispatched_ns": self._clock(),
        }
        status = payload = failure = None
        response_raw = b""
        try:
            response = await self._client.request(
                method, path, params=params or None, content=raw or None,
                headers={"Authorization": "Bearer " + self._credential(),
                         "Content-Type": "application/json",
                         "Accept-Encoding": "identity"},
            )
            status, response_raw = response.status_code, response.content
            if len(response_raw) > MAX_BODY_BYTES:
                raise IntegrityError("Provider response exceeds bound")
            if response_raw:
                payload = json.loads(response_raw)
            return status, payload
        except BaseException as error:
            failure = type(error).__name__
            raise
        finally:
            fields = event_fields(payload) if isinstance(payload, dict) else None
            listed = ([event_fields(i) for i in payload.get("items", [])]
                      if isinstance(payload, dict) and "items" in payload else None)
            self._record("operation", {
                **context, "status": status, "failure": failure,
                "transient": failure is not None or status in TRANSIENT_STATUS,
                "response_body_sha256": sha256(response_raw),
                "response_fields": fields, "listed_fields": listed,
                "next_page_token_present": isinstance(payload, dict)
                and bool(payload.get("nextPageToken")),
                "completed_ns": self._clock(),
            }, (File("response.json", response_raw),) if response_raw else ())

    async def aclose(self):
        await self._client.aclose()


class ProviderTransport(httpx.AsyncBaseTransport):
    """httpx adapter for trusted in-process harness principals only."""

    def __init__(self, broker, capability):
        self._broker, self._capability = broker, capability

    async def handle_async_request(self, request):
        body = json.loads(await request.aread() or b"null")
        status, payload = await self._broker.request(
            self._capability, request.method, request.url.path,
            params=dict(request.url.params), body=body,
        )
        return httpx.Response(status, json=payload) if payload is not None else (
            httpx.Response(status))
