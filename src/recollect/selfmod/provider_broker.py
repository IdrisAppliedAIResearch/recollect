"""Credentialed provider transport with scope, lifecycle gates and attribution audit.

The broker is a trusted host transport, not a calendar capability: it forwards
only exact Google Calendar event routes that a principal's frozen policy allows,
attaches that principal's own host-held credential, and journals a redacted,
attribution-bound record of every operation. A and serving B receive the same
worker access; the target action's gate is controlled by the harness lifecycle.
The verifier uses a separate read-only credential, cleanup is limited to events
the verifier passed, and fixture principals can never use the live origin.

Amendment 01 section 4 is enforced from journaled state, so a restart cannot
reset it: at most three mutation dispatches per action phase with one frozen
deduplication identity; a non-transient failure ends that phase; a transient or
uncertain outcome requires a successful read before the next dispatch. Amendment
02 removes local elapsed-time cutoffs, so provider requests have no timeout.
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
CREDENTIAL_ROLE = {"worker": "worker", "cleanup": "worker", "verifier": "verifier",
                   "fixture": "fixture"}
# Frozen transient classification: only these are transport observations.
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ProviderPolicy:
    calendar_id: str
    origin: str = LIVE_ORIGIN

    def __post_init__(self):
        if (type(self.calendar_id) is not str or not self.calendar_id
                or len(self.calendar_id) > 256
                or any(c.isspace() for c in self.calendar_id)):
            raise ValueError("Freeze the private test calendar identifier")
        if not re.fullmatch(r"https://[a-z0-9.-]+(:[0-9]+)?", self.origin):
            raise ValueError("Provider origin must be an explicit HTTPS origin")

    @property
    def live(self):
        return self.origin == LIVE_ORIGIN

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

    def __init__(self, policy, journal, *, credentials, transport,
                 clock=time.monotonic_ns):
        if type(policy) is not ProviderPolicy or type(credentials) is not dict or any(
                role not in {"worker", "verifier", "fixture"} or not callable(source)
                for role, source in credentials.items()):
            raise ValueError("Freeze a provider policy and per-role credential sources")
        if policy.live and "fixture" in credentials:
            raise ValueError("A live broker never holds fixture credentials")
        if not isinstance(transport, httpx.AsyncBaseTransport):
            raise ValueError("Supply an explicit provider transport")
        self.policy, self._journal, self._credentials = policy, journal, credentials
        self.live, self._clock = policy.live, clock
        self._client = httpx.AsyncClient(
            base_url=policy.origin, transport=transport, trust_env=False,
            follow_redirects=False, timeout=None,
        )
        self._capabilities = {}
        self._gates = set()
        self._attempts = {}
        self._phase_state = {}
        self._dedup = {}
        self._verified_events = set()
        self._lock = asyncio.Lock()
        for record in journal.verify():
            kind, data = record.value["kind"], record.value["data"]
            if kind == "provider_issued" and data.get("action_id"):
                self._dedup.setdefault(data["action_id"], data.get("dedup_id"))
            elif kind == "provider_operation":
                self._observe(data)

    def issue(self, principal, **binding):
        if principal not in PRINCIPALS:
            raise ValueError("Unknown provider principal")
        if principal == "fixture" and self.live:
            raise IntegrityError("Fixture principals cannot use a live broker")
        if CREDENTIAL_ROLE[principal] not in self._credentials:
            raise IntegrityError("No credential for this principal's role")
        if principal == "worker":
            if not valid_dedup_id(binding.get("dedup_id")):
                raise ValueError("Worker actions require a stable dedup identity")
            known = self._dedup.get(binding.get("action_id"))
            if known is not None and known != binding["dedup_id"]:
                raise IntegrityError("An action keeps one frozen dedup identity")
            self._dedup[binding.get("action_id")] = binding["dedup_id"]
        capability = Capability(principal, secrets.token_urlsafe(32), **binding)
        self._capabilities[capability.token] = capability
        self._record("issued", {"principal": principal, **binding})
        return capability

    def open_gate(self, action_id, reason):
        self._gates.add(action_id)
        self._record("gate_opened", {"action_id": action_id, "reason": reason})

    def close_gate(self, action_id, reason):
        self._gates.discard(action_id)
        self._record("gate_closed", {"action_id": action_id, "reason": reason})

    def _allow_cleanup(self, event_id):
        """Called only by the verifier-bound cleanup operation."""
        self._verified_events.add(event_id)
        self._record("cleanup_allowed", {"event_id": event_id})

    def _record(self, kind, data, files=()):
        return self._journal.append("provider_" + kind, data, Snapshot(tuple(files)))

    def _observe(self, data):
        """Advance per-phase reconciliation state from one journaled operation."""
        if data["kind"] == "mutation":
            key = (data["action_id"], data["phase"])
            self._attempts[key] = max(self._attempts.get(key, 0), data["attempt"])
            if data["status"] in {200, 409}:
                self._phase_state[key] = "done"
            elif data["failure"] is not None or data["status"] in TRANSIENT_STATUS:
                self._phase_state[key] = "needs_read"
            else:
                self._phase_state[key] = "terminal"
        elif data["kind"] == "read" and data["status"] == 200:
            for key, state in self._phase_state.items():
                if state == "needs_read":
                    self._phase_state[key] = "reconciled"

    def _authorize(self, capability, method, path, params, body, phase):
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
            key = (capability.action_id, phase)
            state = self._phase_state.get(key)
            if state in {"done", "terminal"}:
                raise IntegrityError("This mutation phase already has a final outcome")
            if state == "needs_read":
                raise IntegrityError("Reconcile with a completed read first")
            if self._attempts.get(key, 0) >= MAX_MUTATION_ATTEMPTS:
                raise IntegrityError("Mutation dispatch attempts exhausted")
            return "mutation"
        if principal == "cleanup" and method == "DELETE" and item:
            if item[1] not in self._verified_events:
                raise IntegrityError("Cleanup is limited to verified experiment events")
            return "cleanup"
        raise IntegrityError("Provider operation is outside the principal's policy")

    async def request(self, capability, method, path, *, params=None, body=None,
                      phase="original", invocation_id=None):
        """Forward one authorized operation.

        ``invocation_id`` is the host-observed tool invocation correlated with
        this request by a trusted relay; it overrides the capability's binding
        in the journal and is never taken from worker-supplied data.
        """
        params = dict(params or {})
        if phase not in {"original", "replay"}:
            raise ValueError("Mutation phase is original or replay")
        raw = encode(body) if body is not None else b""
        if len(raw) > MAX_BODY_BYTES:
            raise IntegrityError("Provider request body exceeds bound")
        async with self._lock:
            kind = self._authorize(capability, method, path, params, body, phase)
            attempt = None
            if kind == "mutation":
                key = (capability.action_id, phase)
                attempt = self._attempts.get(key, 0) + 1
                self._attempts[key] = attempt
                self._phase_state[key] = "dispatching"
        context = {
            "principal": capability.principal, "task_id": capability.task_id,
            "action_id": capability.action_id, "dedup_id": capability.dedup_id,
            "routing_epoch": capability.routing_epoch,
            "serving_digest": capability.serving_digest,
            "invocation_id": (invocation_id if invocation_id is not None
                              else capability.invocation_id),
            "method": method,
            "path_sha256": sha256(path.encode()), "params": params,
            "kind": kind or "read", "phase": phase if kind == "mutation" else None,
            "attempt": attempt, "request_body_sha256": sha256(raw),
            "request_fields": event_fields(body), "dispatched_ns": self._clock(),
        }
        status = payload = failure = None
        response_raw = b""
        credential = self._credentials[CREDENTIAL_ROLE[capability.principal]]
        try:
            response = await self._client.request(
                method, path, params=params or None, content=raw or None,
                headers={"Authorization": "Bearer " + credential(),
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
            data = {
                **context, "status": status, "failure": failure,
                "transient": failure is not None or status in TRANSIENT_STATUS,
                "response_body_sha256": sha256(response_raw),
                "response_fields": fields, "listed_fields": listed,
                "next_page_token_present": isinstance(payload, dict)
                and bool(payload.get("nextPageToken")),
                "completed_ns": self._clock(),
            }
            self._record("operation", data, (File("response.json", response_raw),)
                         if response_raw else ())
            self._observe(data)

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
