"""Pinned slot observation and settlement against a fake llama-server."""

import asyncio

import pytest

from recollect.selfmod.journal import IntegrityError
from recollect.selfmod.model_settlement import SlotSettlement, loopback_root, slot_state
from tests.selfmod_fake_llama import FakeLlama

URL = "http://127.0.0.1:8001/v1"


async def test_settle_waits_for_pinned_slot_to_become_idle_without_deadline():
    fake = FakeLlama()
    fake.state[2].update(is_processing=True, id_task=7, next_token=[{"n_decoded": 3}])
    settlement = SlotSettlement(URL, 2, transport=fake.transport(),
                                poll_interval=0.01)

    async def finish_later():
        await asyncio.sleep(0.1)
        fake.idle(2)

    later = asyncio.create_task(finish_later())
    result = await settlement.settle()
    await later
    assert result["confirmed"] and result["polls"] > 1
    first = result["samples"][0]
    assert first["is_processing"] and first["id_task"] == 7
    assert result["samples"][-1]["is_processing"] is False
    await settlement.aclose()


async def test_busy_pinned_slot_is_not_owned_capacity():
    fake = FakeLlama()
    settlement = SlotSettlement(URL, 1, transport=fake.transport())
    assert (await settlement.require_idle())["is_processing"] is False
    fake.state[1]["is_processing"] = True
    with pytest.raises(IntegrityError, match="already processing"):
        await settlement.require_idle()
    await settlement.aclose()


def test_slot_state_links_task_and_decoded_tokens_across_server_shapes():
    listed = [{"id": 0, "is_processing": True, "id_task": 4,
               "next_token": [{"n_decoded": 9}]}]
    mapped = [{"id": 0, "is_processing": True, "id_task": 4,
               "next_token": {"n_decoded": 9}}]
    for value in (listed, mapped):
        assert slot_state(value, 0) == {"slot": 0, "is_processing": True,
                                        "id_task": 4, "n_decoded": 9}
    assert slot_state([{"id": 0, "is_processing": False}], 0)["n_decoded"] is None
    for value in ({}, [], [{"id": 0}], [{"id": 0, "is_processing": False}] * 2):
        with pytest.raises(IntegrityError):
            slot_state(value, 0)


@pytest.mark.parametrize("url", ["https://127.0.0.1:8001/v1", "http://example.com:8001",
                                 "http://127.0.0.1/v1", "http://127.0.0.1:8001/admin",
                                 "http://u@127.0.0.1:8001/v1"])
def test_only_literal_loopback_model_endpoints(url):
    with pytest.raises(ValueError):
        loopback_root(url)
    assert loopback_root(URL) == "http://127.0.0.1:8001"


def test_slot_and_poll_interval_are_explicit():
    with pytest.raises(ValueError):
        SlotSettlement(URL, -1)
    with pytest.raises(ValueError):
        SlotSettlement(URL, 0, poll_interval=0)


async def test_queued_request_blocks_settlement_until_deferred_drains():
    fake = FakeLlama()
    fake.deferred = 1
    settlement = SlotSettlement(URL, 2, transport=fake.transport(),
                                poll_interval=0.01)

    async def drain_later():
        await asyncio.sleep(0.1)
        fake.deferred = 0

    later = asyncio.create_task(drain_later())
    result = await settlement.settle()
    await later
    assert result["polls"] > 1
    assert result["samples"][0]["requests_deferred"] == 1
    assert result["samples"][-1]["requests_deferred"] == 0
    await settlement.aclose()
