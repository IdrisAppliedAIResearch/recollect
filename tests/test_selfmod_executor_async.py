import asyncio
import threading

import pytest

from recollect.selfmod.journal import IntegrityError
from tests.test_selfmod_executor import records
from tests.test_selfmod_executor import setup as setup


async def wait(event):
    assert await asyncio.to_thread(event.wait, 3), "fixture barrier timed out"


async def test_async_success(setup):
    runner, runtime, _, _ = setup
    runtime.cancel = lambda: None
    assert (await runner.run_async(runtime)).snapshot == runner.spec.baseline


@pytest.mark.parametrize("point", ["prepare", "start", "release", "collect"])
async def test_cancel_waits_for_cleanup_and_ignores_repeated_cancel(setup, point):
    runner, runtime, _, _ = setup
    entered, cancelled, stopping, stopped = (threading.Event() for _ in range(4))
    runtime.cancel = cancelled.set

    def hook(name):
        if name == point:
            entered.set()
            assert cancelled.wait(3)
            raise InterruptedError("cancelled")
        if name == "terminate":
            stopping.set()
            assert stopped.wait(3)

    runtime.hook = hook
    task = asyncio.create_task(runner.run_async(runtime))
    await wait(entered)
    task.cancel()
    await wait(stopping)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    with pytest.raises(IntegrityError, match="in progress"):
        runner.authorize_refresh()
    stopped.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.primary_failed
    assert (
        records(runner, "fixture_failure_accounted")[-1].value["data"]["error_type"]
        == "CallerCancelled"
    )


async def test_cancel_after_thread_success_discards_undelivered_receipt(setup):
    runner, runtime, _, _ = setup
    runtime.cancel = lambda: None
    loop = asyncio.get_running_loop()
    run = runner._run

    def completed(runtime):
        result = run(runtime)
        loop.call_soon_threadsafe(task.cancel)
        return result

    runner._run = completed
    task = asyncio.create_task(runner.run_async(runtime))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert records(runner, "snapshot_verified")
    assert runner.primary_failed
    assert (
        records(runner, "fixture_failure_accounted")[-1].value["data"]["error_type"]
        == "CallerCancelled"
    )


async def test_cancel_with_unconfirmed_cleanup_is_explicit_failure(setup):
    runner, runtime, _, _ = setup
    entered, cancelled = threading.Event(), threading.Event()
    runtime.cancel = cancelled.set
    runtime.clean_stop = False

    def hook(name):
        if name == "collect":
            entered.set()
            assert cancelled.wait(3)
            raise InterruptedError()

    runtime.hook = hook
    task = asyncio.create_task(runner.run_async(runtime))
    await wait(entered)
    task.cancel()
    with pytest.raises(IntegrityError, match="unconfirmed"):
        await task
    with pytest.raises(IntegrityError, match="confirmed stop"):
        runner.authorize_refresh()


async def test_cancel_before_dispatch_never_prepares_worker(setup):
    runner, runtime, _, _ = setup
    runtime.cancel = lambda: None
    runner._cancelled.set()
    with pytest.raises(InterruptedError):
        await runner.run_async(runtime)
    assert "prepare" not in runtime.calls
    assert runner.primary_failed


async def test_async_delivery_after_deadline_discards_provisional_receipt(setup):
    runner, runtime, clock, _ = setup
    runtime.cancel = lambda: None
    run = runner._run

    def completed(runtime):
        result = run(runtime)
        clock.ns = 21_000_000_000
        return result

    runner._run = completed
    with pytest.raises(IntegrityError, match="deadline"):
        await runner.run_async(runtime)
    assert records(runner, "snapshot_verified")
    assert runner.primary_failed
    assert (
        records(runner, "fixture_failure_accounted")[-1].value["data"]["error_type"]
        == "ReceiptDeliveryFailed"
    )


async def test_throwing_cancellation_hook_cannot_skip_failure_accounting(setup):
    runner, runtime, _, _ = setup
    loop = asyncio.get_running_loop()
    run = runner._run

    def broken_cancel():
        raise OSError("broken cancellation hook")

    def completed(runtime):
        result = run(runtime)
        loop.call_soon_threadsafe(task.cancel)
        return result

    runtime.cancel, runner._run = broken_cancel, completed
    task = asyncio.create_task(runner.run_async(runtime))
    with pytest.raises(IntegrityError, match="hook failed"):
        await task
    assert runner.primary_failed
    failure = records(runner, "fixture_failure_accounted")[-1].value["data"]
    assert "broken cancellation hook" in failure["error"]
    assert failure["termination_confirmed"] is True


async def test_baseexception_at_handoff_still_accounts_failed_delivery(setup):
    class ClockFailure(BaseException):
        pass

    runner, runtime, _, _ = setup
    runtime.cancel = lambda: None
    run = runner._run

    def broken():
        raise ClockFailure("handoff clock unavailable")

    def completed(runtime):
        result = run(runtime)
        runner._clock = broken
        return result

    runner._run = completed
    with pytest.raises(ClockFailure):
        await runner.run_async(runtime)
    assert runner.primary_failed
    failure = records(runner, "fixture_failure_accounted")[-1].value["data"]
    assert failure["error_type"] == "ReceiptDeliveryFailed"
