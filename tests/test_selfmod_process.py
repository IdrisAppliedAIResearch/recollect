"""Real benign child processes exercise pipes; never runs candidate code."""

import sys
import threading
import time

import pytest

from recollect.selfmod.clock import current_stamp
from recollect.selfmod.executor import Deadline
from recollect.selfmod.journal import IntegrityError
from recollect.selfmod.process import PipeCommand


def command(code, *, seconds=3, cap=8192, cancelled=None):
    now = current_stamp()
    return PipeCommand(
        [sys.executable, "-I", "-S", "-u", "-c", code],
        Deadline(now.monotonic_ns + int(seconds * 1e9), now.boot_id),
        stdout_limit=cap,
        stderr_limit=cap,
        cancelled=cancelled,
    )


def test_split_ready_and_report_keep_stdin_open_after_release():
    child = command(
        "import sys,time,os\nos.write(1,b'ready\\n')\n"
        "line=sys.stdin.buffer.readline()\nos.write(1,b'report:'+line)\n"
        "time.sleep(.05)\n"
    )
    try:
        assert child.line(2048) == b"ready\n"
        child.send(b"release\n")
        assert not child.process.stdin.closed
        assert child.finish() == (b"report:release\n", b"", 0)
        with pytest.raises(IntegrityError, match="one bounded"):
            child.send(b"duplicate\n")
    finally:
        child.close()


@pytest.mark.parametrize("stream", [1, 2])
def test_output_flood_is_bounded_and_child_reaped(stream):
    child = command(f"import os\nwhile True: os.write({stream},b'x'*8192)", cap=128)
    try:
        with pytest.raises(IntegrityError, match="output limit"):
            child.finish()
    finally:
        child.close()
    assert child.process.returncode is not None
    assert all(len(data) <= 128 for data in child.evidence())


def test_stalled_child_is_killed_by_watchdog_without_caller_polling():
    child = command("import time; time.sleep(10)", seconds=0.15)
    try:
        child.process.wait(timeout=2)
        with pytest.raises(TimeoutError):
            child.finish()
    finally:
        child.close()


def test_cancel_wakes_blocked_read_and_stops_process():
    event = threading.Event()
    child = command(
        "import time,os; os.write(1,b'ready\\n'); time.sleep(10)", cancelled=event
    )
    try:
        assert child.line(2048) == b"ready\n"
        event.set()
        with pytest.raises(InterruptedError):
            child.finish()
    finally:
        child.close()
    assert child.process.returncode is not None


@pytest.mark.parametrize("code", ["print('x'*2048)", "print('unterminated',end='')"])
def test_invalid_readiness_is_rejected(code):
    child = command(code)
    try:
        with pytest.raises(IntegrityError):
            child.line(2048)
    finally:
        child.close()


def test_nonzero_exit_preserves_both_streams():
    child = command(
        "import sys,os; os.write(1,b'out\\n'); os.write(2,b'err\\n'); sys.exit(7)"
    )
    try:
        assert child.finish() == (b"out\n", b"err\n", 7)
    finally:
        child.close()


def test_pipe_descendant_is_not_mistaken_for_complete_parent():
    # The benign descendant exits on its own; no unrelated process is terminated.
    child = command(
        "import subprocess,sys\nsubprocess.Popen([sys.executable,'-I','-S','-c',"
        "'import time; time.sleep(.8)'])\n",
        seconds=0.2,
    )
    try:
        with pytest.raises(TimeoutError):
            child.finish()
        with pytest.raises(IntegrityError, match="teardown"):
            child.close()
    finally:
        # Wait only for this fixed-lived fixture's inherited pipes, then reap.
        time.sleep(0.9)
        child.close()


def test_expired_command_does_not_spawn(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("spawned after expiry")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    now = current_stamp()
    with pytest.raises(TimeoutError):
        PipeCommand(
            [sys.executable], Deadline(now.monotonic_ns, now.boot_id), stdout_limit=1
        )
