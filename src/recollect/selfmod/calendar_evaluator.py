"""Frozen independent read-only Calendar verifier, replay and attribution checks.

The verifier holds only a read principal. A completed search means every page
was read; exhausted transient reads or authorization errors produce "unknown",
never zero or absent events. It records field-level mismatches and keeps B's
claim, observed provider state and the calendar-action result separate.
Cleanup can delete only an event this verifier itself observed as passing.
"""

import asyncio
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .journal import IntegrityError
from .provider_broker import TRANSIENT_STATUS

READ_BACKOFF_S = (1, 2)
UNKNOWN_STATUS = frozenset({401, 403})


@dataclass(frozen=True)
class ExpectedEvent:
    experiment_id: str
    event_date: Date
    time_zone: str
    start_hour: int = 15
    duration_minutes: int = 30

    def __post_init__(self):
        if not self.experiment_id or type(self.event_date) is not Date:
            raise ValueError("Freeze the experiment marker and event date")
        ZoneInfo(self.time_zone)

    @property
    def title(self):
        return f"Self-modification review {self.experiment_id}"

    @property
    def start(self):
        return datetime(self.event_date.year, self.event_date.month,
                        self.event_date.day, self.start_hour,
                        tzinfo=ZoneInfo(self.time_zone))

    @property
    def end(self):
        return self.start + timedelta(minutes=self.duration_minutes)

    def window(self):
        return ((self.start - timedelta(days=1)).isoformat(),
                (self.end + timedelta(days=1)).isoformat())


def _instant(value):
    if type(value) is not dict or type(value.get("dateTime")) is not str:
        return None
    try:
        return datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
    except ValueError:
        return None


def mismatches(expected, event):
    found = []
    if event.get("summary") != expected.title:
        found.append("title")
    start, end = _instant(event.get("start")), _instant(event.get("end"))
    if start is None or start != expected.start:
        found.append("start_instant")
    if end is None or end != expected.end:
        found.append("end_instant")
    if start is not None and end is not None and end - start != timedelta(
            minutes=expected.duration_minutes):
        found.append("duration")
    if (event.get("start") or {}).get("timeZone") not in (None, expected.time_zone):
        found.append("time_zone")
    if event.get("attendees"):
        found.append("attendees")
    if event.get("status") == "cancelled":
        found.append("status")
    return found


class CalendarVerifier:
    def __init__(self, broker, capability, expected, *, sleep=asyncio.sleep):
        if capability.principal != "verifier":
            raise IntegrityError("Verifier requires a read-only principal")
        self._broker, self._capability = broker, capability
        self.expected, self._sleep = expected, sleep
        self.passed = set()

    async def _read(self, path, params=None):
        observations = []
        for attempt in range(len(READ_BACKOFF_S) + 1):
            try:
                status, payload = await self._broker.request(
                    self._capability, "GET", path, params=params)
            except IntegrityError:
                raise
            except Exception as error:
                observations.append({"attempt": attempt + 1,
                                     "failure": type(error).__name__})
            else:
                observations.append({"attempt": attempt + 1, "status": status})
                if status in UNKNOWN_STATUS:
                    return None, None, observations
                if status not in TRANSIENT_STATUS:
                    return status, payload, observations
            if attempt < len(READ_BACKOFF_S):
                await self._sleep(READ_BACKOFF_S[attempt])
        return None, None, observations

    async def search(self):
        """Complete marker search across every page, or unknown."""
        start, end = self.expected.window()
        matches, token, reads = [], None, []
        while True:
            params = {"q": self.expected.experiment_id, "timeMin": start,
                      "timeMax": end, "singleEvents": "true"}
            if token:
                params["pageToken"] = token
            status, payload, observations = await self._read(
                self._broker.policy.events_path, params)
            reads.append(observations)
            if status != 200 or type(payload) is not dict:
                return {"complete": False, "count": None, "matches": None,
                        "reads": reads}
            matches.extend(item for item in payload.get("items", [])
                           if self.expected.experiment_id in (item.get("summary") or "")
                           and item.get("status") != "cancelled")
            token = payload.get("nextPageToken")
            if not token:
                return {"complete": True, "count": len(matches), "matches": matches,
                        "reads": reads}

    async def baseline_empty(self):
        result = await self.search()
        return result["complete"] and result["count"] == 0, result

    async def verify(self, event_id, *, claimed=None):
        read_status, event, read_obs = await self._read(
            self._broker.policy.events_path + "/" + event_id)
        search = await self.search()
        result = {"claimed": claimed, "event_id": event_id, "read_status": read_status,
                  "read_observations": read_obs, "search": search}
        if read_status is None or not search["complete"]:
            return {**result, "observed": "unknown", "action_result": "unknown"}
        if read_status != 200 or type(event) is not dict:
            return {**result, "observed": "absent", "action_result": "failed"}
        fields = mismatches(self.expected, event)
        count = search["count"]
        observed = ("duplicate" if count > 1 else "absent" if count == 0
                    else "wrong" if fields or search["matches"][0].get("id") != event_id
                    else "exactly_one_correct")
        passed = observed == "exactly_one_correct"
        if passed:
            self.passed.add(event_id)
        return {**result, "event": event, "mismatches": fields,
                "matching_count": count, "observed": observed,
                "html_link": event.get("htmlLink"),
                "action_result": "pass" if passed else "failed"}

    async def verify_replay(self, first, replay_event_id):
        """The replay must report the same provider event and leave exactly one."""
        again = await self.verify(first["event_id"])
        same = replay_event_id is not None and replay_event_id == first["event_id"]
        passed = (first["action_result"] == "pass" and again["action_result"] == "pass"
                  and same)
        return {"first_event_id": first["event_id"], "replay_event_id": replay_event_id,
                "after_replay": again, "same_identity": same,
                "result": "pass" if passed else "unknown"
                if again["observed"] == "unknown" else "failed"}


async def cleanup(broker, capability, verifier, event_id):
    """Delete only an event this verifier observed passing; record the receipt."""
    if capability.principal != "cleanup" or event_id not in verifier.passed:
        raise IntegrityError("Cleanup requires a cleanup principal and verified event")
    broker._allow_cleanup(event_id)
    status, _ = await broker.request(
        capability, "DELETE", broker.policy.events_path + "/" + event_id)
    return {"event_id": event_id, "status": status,
            "deleted": status in {200, 204, 410}}


def attribute(operations, *, verified_event_id, action_id, task_id, serving_digest,
              routing_epoch, invocation_modules, candidate_modules, sealed_after_ns):
    """Link generated invocation -> provider write -> response -> independent read.

    ``operations`` are provider_operation journal data; ``invocation_modules``
    maps controller-instrumented invocation IDs to executed modules, which must
    belong to B's accepted candidate (``candidate_modules``). Any missing,
    foreign or ambiguous link fails attribution.
    """
    writes = [o for o in operations if o["kind"] == "mutation"
              and o["action_id"] == action_id]
    created = [o for o in writes if o["status"] == 200]
    reasons = []
    if len(created) != 1:
        reasons.append("expected_exactly_one_successful_create")
    for write in writes:
        module = invocation_modules.get(write["invocation_id"])
        if (write["principal"] != "worker" or write["task_id"] != task_id
                or write["serving_digest"] != serving_digest
                or write["routing_epoch"] != routing_epoch
                or module is None or module not in candidate_modules
                or write["dispatched_ns"] <= sealed_after_ns):
            reasons.append("unattributed_foreign_or_pre_activation_write")
            break
    created_id = None
    if created:
        created_id = (created[0]["response_fields"] or {}).get("id")
    if created and created_id != verified_event_id:
        reasons.append("created_event_differs_from_verified_event")
    return {"attributed": not reasons, "reasons": reasons, "writes": len(writes),
            "modules": sorted({invocation_modules.get(w["invocation_id"])
                               for w in writes
                               if w["invocation_id"] in invocation_modules})}
