"""Pinned modifier lane: host slot routing and upstream settlement evidence."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from recollect.selfmod.journal import IntegrityError, Journal
from recollect.selfmod.model_settlement import SlotSettlement
from recollect.selfmod.native_broker import TOKEN_CAP_FIELDS, NativeModelBroker
from tests.selfmod_fake_llama import FakeLlama
from tests.test_selfmod_native_broker import RAW, SETTINGS, data, forward

PINNED = replace(SETTINGS, slot=2)


@pytest.fixture
async def pinned(tmp_path):
    made = []

    def make(fake, *, settlement_transport=None):
        journal = Journal.create(tmp_path / f"journal-{len(made)}")
        settlement = SlotSettlement(
            PINNED.base_url, 2,
            transport=settlement_transport or fake.transport(), poll_interval=0.01,
        )
        broker = NativeModelBroker(PINNED, journal, transport=fake.transport(),
                                   settlement=settlement)
        made.append((broker, journal))
        return broker, journal

    yield make
    for broker, journal in made:
        await broker.close()
        journal.close()


async def test_host_pins_slot_after_validation_and_confirms_idle(pinned):
    fake = FakeLlama(tokens=3, delay=0.01)
    broker, journal = pinned(fake)
    capped = json.dumps({**json.loads(RAW), "max_tokens": 64}).encode()
    await forward(broker, capped)
    sent = fake.requests[-1]
    assert sent["id_slot"] == 2 and not TOKEN_CAP_FIELDS & sent.keys()
    end = data(journal, "end")[-1]
    assert end["upstream_quiescence"] == "pinned_slot_idle_confirmed"
    assert end["settlement"]["confirmed"] and broker.upstream_settled


async def test_native_cannot_choose_its_own_slot(pinned):
    fake = FakeLlama()
    broker, _ = pinned(fake)
    raw = json.dumps({**json.loads(RAW), "id_slot": 0}).encode()
    with pytest.raises(IntegrityError, match="routing/control"):
        await forward(broker, raw)
    assert not fake.requests


async def test_occupied_pinned_slot_refuses_dispatch(pinned):
    fake = FakeLlama()
    fake.state[2]["is_processing"] = True
    broker, journal = pinned(fake)
    with pytest.raises(IntegrityError, match="already processing"):
        await forward(broker)
    assert not fake.requests
    assert data(journal, "end")[-1]["dispatch_attempted"] is False


async def test_cancelled_generation_is_settled_only_when_slot_goes_idle(pinned):
    fake = FakeLlama(tokens=2, delay=0.01, stop_delay=0.1)
    fake.hold = asyncio.Event()
    broker, journal = pinned(fake)
    task = asyncio.create_task(forward(broker))
    for _ in range(200):
        if fake.requests and fake.state[2]["is_processing"]:
            break
        await asyncio.sleep(0.01)
    await broker.close()
    with pytest.raises((asyncio.CancelledError, IntegrityError)):
        await task
    end = data(journal, "end")[-1]
    assert end["http_body_complete"] is False
    assert end["upstream_quiescence"] == "pinned_slot_idle_confirmed"
    assert end["settlement"]["polls"] > 1
    assert fake.state[2]["is_processing"] is False
    assert broker.upstream_settled


async def test_failed_settlement_observation_leaves_upstream_unsettled(pinned):
    fake = FakeLlama(tokens=2, delay=0.01)
    calls = []

    async def fail_after_dispatch(request):
        # The idle check succeeds; the post-request settlement read fails.
        calls.append(request)
        if len(calls) > 1:
            raise httpx.ConnectError("server gone")
        return await fake.handle(request)

    broker, journal = pinned(
        fake, settlement_transport=httpx.MockTransport(fail_after_dispatch))
    await forward(broker)
    end = data(journal, "end")[-1]
    assert end["upstream_quiescence"] == "unknown"
    assert end["settlement"] == {"error": "ConnectError"}
    assert not broker.upstream_settled


def test_settlement_must_match_frozen_slot(tmp_path):
    with Journal.create(tmp_path / "journal") as journal:
        with pytest.raises(ValueError, match="slot"):
            NativeModelBroker(PINNED, journal)
        with pytest.raises(ValueError, match="slot"):
            NativeModelBroker(SETTINGS, journal,
                              settlement=SlotSettlement(SETTINGS.base_url, 2))
        with pytest.raises(ValueError, match="slot"):
            NativeModelBroker(PINNED, journal,
                              settlement=SlotSettlement(SETTINGS.base_url, 1))
    with pytest.raises(ValueError):
        replace(SETTINGS, slot=-1)
