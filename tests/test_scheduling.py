"""The generic "do this at time T" primitive: late-fire once, fail loudly.

No real wall-clock waits beyond one tiny loop test; the rest drives
``deliver_due`` with an explicit ``now``.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from recollect.connections import ConnectionService
from recollect.scheduling import MAX_PENDING, SCHEMA, Scheduler, ScheduleStore
from recollect.selfmod.service import notice_delivery


def later(seconds):
    return datetime.now(UTC) + timedelta(seconds=seconds)


def nothing(job):
    pass


def scheduler_for(tmp_path, deliver=nothing):
    return Scheduler(ScheduleStore(tmp_path / "schedules.json"), deliver)


def write_one_job(tmp_path, **overrides):
    job = {"job_id": "j1", "due_at": later(60).isoformat(), "payload": {},
           "status": "pending", "created_at": later(0).isoformat()}
    job.update(overrides)
    (tmp_path / "schedules.json").write_text(
        json.dumps({"schema": SCHEMA, "jobs": [job]}), encoding="utf-8")


def test_a_saved_job_with_unbookable_times_is_malformed(tmp_path):
    path = tmp_path / "schedules.json"
    write_one_job(tmp_path, due_at="2026-09-19T13:30:00")  # naive
    with pytest.raises(ValueError, match="Malformed"):
        ScheduleStore(path).load()
    write_one_job(tmp_path, created_at=None)  # the file may not lie about it
    with pytest.raises(ValueError, match="Malformed"):
        ScheduleStore(path).load()
    write_one_job(tmp_path, fired_at=123)  # a sort key must be a string
    with pytest.raises(ValueError, match="Malformed"):
        ScheduleStore(path).load()


def test_a_saved_naive_time_starts_empty_instead_of_killing_the_loop(
        tmp_path):
    write_one_job(tmp_path, due_at="2026-09-19T13:30:00")
    assert scheduler_for(tmp_path).pending() == []


def test_a_saved_schedule_survives_the_process(tmp_path):
    scheduler = scheduler_for(tmp_path)
    job = scheduler.schedule(later(3600), {"text": "later"})
    reloaded = ScheduleStore(tmp_path / "schedules.json").load()
    assert [entry["job_id"] for entry in reloaded] == [job["job_id"]]
    assert reloaded[0]["payload"] == {"text": "later"}
    assert reloaded[0]["status"] == "pending"


def test_an_unreadable_schedule_file_is_a_value_error(tmp_path):
    path = tmp_path / "schedules.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="could not be read"):
        ScheduleStore(path).load()
    path.write_text('{"schema": 1, "jobs": [{"job_id": "x"}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="Malformed"):
        ScheduleStore(path).load()


def test_a_corrupt_schedule_starts_empty_instead_of_taking_the_server_down(
        tmp_path):
    path = tmp_path / "schedules.json"
    path.write_text("garbage", encoding="utf-8")
    scheduler = Scheduler(ScheduleStore(path), nothing)
    assert scheduler.pending() == []


def test_only_aware_times_and_bounded_json_payloads_are_booked(tmp_path):
    scheduler = scheduler_for(tmp_path)
    naive = datetime(2026, 9, 19, 13, 30)
    with pytest.raises(ValueError, match="timezone-aware"):
        scheduler.schedule(naive, {"text": "someday"})
    with pytest.raises(ValueError, match="payload"):
        scheduler.schedule(later(60), "not an object")
    with pytest.raises(ValueError, match="payload"):
        scheduler.schedule(later(60), {"text": set()})
    with pytest.raises(ValueError, match="payload"):
        scheduler.schedule(later(60), {"text": "x" * 5000})
    assert scheduler.pending() == []


def test_the_pending_queue_is_bounded(tmp_path):
    scheduler = scheduler_for(tmp_path)
    for index in range(MAX_PENDING):
        scheduler.schedule(later(3600 + index), {"n": index})
    with pytest.raises(ValueError, match="wait at once"):
        scheduler.schedule(later(7200), {"n": "one too many"})


async def test_due_jobs_fire_oldest_first_and_exactly_once(tmp_path):
    fired = []

    async def deliver(job):
        fired.append((job["job_id"], job["payload"]["n"]))

    scheduler = scheduler_for(tmp_path, deliver)
    second = scheduler.schedule(later(200), {"n": "second"})
    first = scheduler.schedule(later(100), {"n": "first"})
    horizon = later(500)
    assert await scheduler.deliver_due(now=later(0)) == 0
    assert await scheduler.deliver_due(now=horizon) == 2
    assert [entry[1] for entry in fired] == ["first", "second"]
    # Fired jobs are drained: the same tick never delivers them twice.
    assert await scheduler.deliver_due(now=horizon) == 0
    stored = {job["job_id"]: job for job in
              ScheduleStore(tmp_path / "schedules.json").load()}
    assert stored[first["job_id"]]["status"] == "fired"
    assert stored[second["job_id"]]["fired_at"]


async def test_a_job_due_while_the_server_was_down_fires_late(tmp_path):
    fired = []

    async def deliver(job):
        fired.append(job["payload"]["n"])

    scheduler = scheduler_for(tmp_path, deliver)
    scheduler.schedule(later(-3600), {"n": "missed while offline"})
    # The process that booked it died; a fresh one honours the file.
    revived = scheduler_for(tmp_path, deliver)
    assert await revived.deliver_due() == 1
    assert fired == ["missed while offline"]


async def test_a_failing_delivery_fails_loudly_without_stopping_the_rest(
        tmp_path):
    fired = []

    async def deliver(job):
        fired.append(job["payload"]["n"])
        if job["payload"]["n"] == "bad":
            raise RuntimeError("the channel was down")

    scheduler = scheduler_for(tmp_path, deliver)
    scheduler.schedule(later(100), {"n": "bad"})
    scheduler.schedule(later(200), {"n": "good"})
    assert await scheduler.deliver_due(now=later(500)) == 2
    # The loop survived, the bad job records its reason, and is not retried.
    assert fired == ["bad", "good"]
    stored = {job["payload"]["n"]: job for job in
              ScheduleStore(tmp_path / "schedules.json").load()}
    assert stored["bad"]["status"] == "failed"
    assert "the channel was down" in stored["bad"]["error"]
    assert stored["good"]["status"] == "fired"
    assert await scheduler.deliver_due(now=later(500)) == 0


async def test_a_delivery_sees_its_job_already_claimed(tmp_path):
    """A crash mid-delivery loses that job; it cannot re-fire afterwards."""
    seen = []

    async def deliver(job):
        seen.append(ScheduleStore(tmp_path / "schedules.json").load()[0])
        raise SystemExit  # the process dies right here

    scheduler = scheduler_for(tmp_path, deliver)
    scheduler.schedule(later(-1), {"n": 1})
    with pytest.raises(SystemExit):
        await scheduler.deliver_due()
    # Re-opening the file after the "crash" shows the job fired, not pending.
    assert seen[0]["status"] == "fired"
    survivor = scheduler_for(tmp_path, nothing)
    assert await survivor.deliver_due() == 0


def test_a_pending_job_can_be_canceled_once(tmp_path):
    scheduler = scheduler_for(tmp_path)
    job = scheduler.schedule(later(3600), {"n": 1})
    canceled = scheduler.cancel(job["job_id"])
    assert canceled["status"] == "canceled"
    assert scheduler.pending() == []
    with pytest.raises(ValueError, match="already canceled"):
        scheduler.cancel(job["job_id"])
    with pytest.raises(KeyError):
        scheduler.cancel("nope")


async def test_running_wakes_when_a_moment_arrives(tmp_path):
    done = asyncio.Event()
    fired = []

    async def deliver(job):
        fired.append(job["job_id"])
        done.set()

    scheduler = scheduler_for(tmp_path, deliver)
    job = scheduler.schedule(later(0.05), {"n": 1})
    await scheduler.start(idle_seconds=0.02)
    try:
        await asyncio.wait_for(done.wait(), 5)
    finally:
        await scheduler.close()
    assert fired == [job["job_id"]]


async def test_the_schedule_relay_answers_only_holders_of_the_key(tmp_path):
    service = ConnectionService(None, None, scheduler_for(tmp_path))
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app),
            base_url="http://service") as client:
        assert (await client.post("/schedules", json={
            "due_at": later(60).isoformat(), "payload": {"n": 1}})).status_code \
            == 401
        assert (await client.get("/schedules")).status_code == 401
        assert (await client.delete("/schedules/any")).status_code == 401
    await service.close()


async def test_a_keyed_caller_books_lists_and_cancels_a_job(tmp_path):
    service = ConnectionService(None, None, scheduler_for(tmp_path))
    headers = {"Authorization": f"Bearer {service.key}"}
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app),
            base_url="http://service") as client:
        bad = await client.post("/schedules", headers=headers, json={
            "due_at": "2026-09-19T13:30:00", "payload": {"n": 1}})
        assert bad.status_code == 400 and "timezone-aware" in bad.json()["detail"]
        broken = await client.post("/schedules", headers=headers,
                                   content=b"{oops")
        assert broken.status_code == 400
        booked = await client.post("/schedules", headers=headers, json={
            "due_at": later(3600).isoformat(), "payload": {"n": 1}})
        assert booked.status_code == 200
        job_id = booked.json()["job_id"]
        listed = await client.get("/schedules", headers=headers)
        assert [entry["job_id"] for entry in listed.json()] == [job_id]
        canceled = await client.delete(f"/schedules/{job_id}", headers=headers)
        assert canceled.json()["status"] == "canceled"
        assert (await client.delete(f"/schedules/{job_id}",
                                    headers=headers)).status_code == 409
        assert (await client.delete("/schedules/nope",
                                    headers=headers)).status_code == 404
    await service.close()


async def test_a_service_without_a_scheduler_schedules_nothing():
    service = ConnectionService()
    headers = {"Authorization": f"Bearer {service.key}"}
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service.app),
            base_url="http://service") as client:
        assert (await client.get("/schedules", headers=headers)).json() == []
        assert (await client.post("/schedules", headers=headers, json={
            "due_at": later(60).isoformat(), "payload": {}})).status_code == 404
        assert (await client.delete("/schedules/x",
                                    headers=headers)).status_code == 404
    await service.close()


async def test_closing_the_service_stops_the_scheduler(tmp_path):
    fired = []

    async def deliver(job):
        fired.append(job)

    scheduler = scheduler_for(tmp_path, deliver)
    scheduler.schedule(later(0.15), {"n": 1})
    await scheduler.start(idle_seconds=0.02)
    service = ConnectionService(None, None, scheduler)
    await service.close()
    await asyncio.sleep(0.4)
    assert fired == []


async def test_a_due_notice_is_posted_into_the_task_that_booked_it():
    posted = []

    def notify(session_id, task_id, message_id, text, kind="progress"):
        posted.append((session_id, task_id, message_id, text, kind))

    deliver = notice_delivery(notify)
    await deliver({"job_id": "abc", "payload": {
        "session_id": "s1", "task_id": "t1", "text": "  Friday prayer  "}})
    assert posted == [("s1", "t1", "scheduled-abc", "Friday prayer", "reminder")]


async def test_a_notice_payload_is_checked_before_anything_is_posted():
    posted = []

    def notify(*args, **kwargs):
        posted.append(args)

    deliver = notice_delivery(notify)
    with pytest.raises(ValueError, match="task_id, text"):
        await deliver({"job_id": "abc", "payload": {"session_id": "s1"}})
    with pytest.raises(ValueError, match="session_id"):
        await deliver({"job_id": "abc", "payload": {
            "session_id": "   ", "task_id": "t1", "text": "hi"}})
    assert posted == []
