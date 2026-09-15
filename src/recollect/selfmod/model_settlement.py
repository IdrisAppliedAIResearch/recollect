"""Read-only llama-server slot observation for lane settlement and concurrency.

A pinned lane owns exactly one server slot, so that slot becoming idle after an
HTTP body completes or is abandoned is the settlement proof used here. Closing
HTTP alone is never labeled GPU settlement. Polling has no deadline: an
indefinitely busy slot leaves upstream stop unknown and ownership retained.
"""

import asyncio
import ipaddress
import time
from urllib.parse import urlsplit

import httpx

from .journal import IntegrityError

MAX_RETAINED_SAMPLES = 64


def loopback_root(base_url):
    """Accept only a literal loopback http origin, optionally with /v1."""
    url = urlsplit(base_url)
    try:
        address = ipaddress.ip_address(url.hostname or "")
    except ValueError as exc:
        raise ValueError("Freeze a literal loopback model endpoint") from exc
    if (url.scheme != "http" or not address.is_loopback or url.port is None
            or url.username is not None or url.password is not None
            or url.query or url.fragment or url.path not in {"", "/", "/v1"}):
        raise ValueError("Freeze a literal loopback model endpoint")
    return f"http://{url.hostname}:{url.port}"


def slot_state(value, slot):
    """Extract one slot's request-linked state from a /slots response."""
    if type(value) is not list:
        raise IntegrityError("Malformed model server slot inventory")
    matches = [s for s in value if type(s) is dict and s.get("id") == slot]
    if len(matches) != 1 or type(matches[0].get("is_processing")) is not bool:
        raise IntegrityError("Pinned model slot is absent or ambiguous")
    item = matches[0]
    token = item.get("next_token")
    if type(token) is list:
        token = token[0] if token else None
    decoded = token.get("n_decoded") if type(token) is dict else None
    task = item.get("id_task")
    return {
        "slot": slot, "is_processing": item["is_processing"],
        "id_task": task if type(task) is int else None,
        "n_decoded": decoded if type(decoded) is int else None,
    }


class SlotObserver:
    """GET /slots only; this client can neither generate nor cancel requests."""

    def __init__(self, base_url, *, transport=None, poll_interval=0.05):
        if type(poll_interval) not in {int, float} or not 0 < poll_interval <= 1:
            raise ValueError("Poll interval is an observation cadence, not a deadline")
        self.root = loopback_root(base_url)
        self.poll_interval = poll_interval
        self._client = httpx.AsyncClient(
            base_url=self.root,
            transport=transport or httpx.AsyncHTTPTransport(retries=0,
                                                            trust_env=False),
            trust_env=False, follow_redirects=False, timeout=None,
        )

    async def slots(self):
        response = await self._client.get("/slots")
        response.raise_for_status()
        return time.monotonic_ns(), response.json()

    async def aclose(self):
        await self._client.aclose()


class SlotSettlement:
    """Settlement for one exclusive pinned lane slot."""

    def __init__(self, base_url, slot, *, transport=None, poll_interval=0.05):
        if type(slot) is not int or not 0 <= slot < 64:
            raise ValueError("Pin an explicit model server slot")
        self.slot = slot
        self._observer = SlotObserver(base_url, transport=transport,
                                      poll_interval=poll_interval)

    async def sample(self):
        observed, value = await self._observer.slots()
        return {"monotonic_ns": observed, **slot_state(value, self.slot)}

    async def require_idle(self):
        """A busy pinned slot means another owner is using this lane's capacity."""
        state = await self.sample()
        if state["is_processing"]:
            raise IntegrityError("Pinned model slot is already processing")
        return state

    async def settle(self):
        """Poll until the pinned slot is idle; there is deliberately no deadline."""
        retained, polls = [], 0
        while True:
            state = await self.sample()
            polls += 1
            if len(retained) < MAX_RETAINED_SAMPLES or not state["is_processing"]:
                retained.append(state)
            if not state["is_processing"]:
                return {"slot": self.slot, "polls": polls, "confirmed": True,
                        "samples": retained[-MAX_RETAINED_SAMPLES:]}
            await asyncio.sleep(self._observer.poll_interval)

    async def aclose(self):
        await self._observer.aclose()
