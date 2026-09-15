"""Observational work timing without weakening termination/evidence gates."""

import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from recollect.selfmod import fixture_supervisor as supervisor
from recollect.selfmod.containment import release_record
from recollect.selfmod.executor import Deadline
from recollect.selfmod.journal import IntegrityError, decode, sha256
from recollect.selfmod.process import PipeCommand
from tests.selfmod_containment_helpers import spec
from tests.selfmod_round_helpers import FakeClock


def test_explicit_unbounded_release_is_authenticated():
    fixture = replace(spec(), timeout_ms=None)
    binding = {
        "spec_sha256": fixture.sha256, "supervisor_sha256": "a" * 64,
        "run_id": fixture.run_id,
    }
    wire = release_record(fixture, "a" * 64, None)
    assert decode(wire)["remaining_ms"] is None
    assert supervisor.released_deadline(decode(wire), binding, None) is None
    for timeout, remaining in [(None, 1), (1000, None), (None, False), (1000, True)]:
        with pytest.raises(ValueError):
            supervisor.released_deadline(
                {"kind": "release", **binding, "remaining_ms": remaining},
                binding, timeout,
            )
    with pytest.raises(ValueError, match="explicitly"):
        supervisor.released_deadline({"kind": "release", **binding}, binding, None)
    with pytest.raises(ValueError):
        supervisor.released_deadline(
            {**decode(wire), "run_id": "wrong"}, binding, None,
        )
    for remaining in (1, False):
        with pytest.raises((ValueError, IntegrityError)):
            release_record(fixture, "a" * 64, remaining)


def test_timing_amendment_detached_receipt_matches():
    root = Path(__file__).resolve().parents[1] / "docs"
    name = "SELF_MODIFICATION_AMENDMENT_02.md"
    assert (root / "SELF_MODIFICATION_AMENDMENT_02.sha256").read_text() == (
        sha256((root / name).read_bytes()) + "  " + name + "\n"
    )


@pytest.mark.parametrize("stop", ["cancel", "clock"])
def test_unbounded_pipe_survives_days_but_preserves_stop_gates(stop):
    clock, cancelled = FakeClock(), threading.Event()
    child = PipeCommand(
        [sys.executable, "-I", "-S", "-u", "-c",
         "import sys,os; os.write(1,b'ready\\n'); print(sys.stdin.readline())"],
        Deadline(None, clock.boot), stdout_limit=4096,
        cancelled=cancelled, clock=clock,
    )
    try:
        assert child.line(2048) == b"ready\n"
        clock.ns += 7 * 86400 * 1_000_000_000
        child._check_time()
        assert child.process.poll() is None
        if stop == "cancel":
            cancelled.set()
            error = InterruptedError
        else:
            clock.boot = "changed"
            error = IntegrityError
        with pytest.raises(error):
            child.finish()
    finally:
        child.close()
    assert child.process.returncode is not None


@pytest.mark.parametrize("scenario,expected", [
    ("working", 125), ("startup", 125), ("settling_stalled", 124),
    ("missing_settling", 125), ("partial_settling", 125),
    ("trailing_partial", 125), ("exit_before_final_read", 0),
    ("nonzero", 7), ("nonzero_before_release", 7),
    ("exit_before_release_stalled", 124),
])
def test_unbounded_watchdog_phase_protocol(monkeypatch, scenario, expected):
    state = SimpleNamespace(now=0.0, polls=0, reads=0, reaped=False)
    released = supervisor.canonical({"deadline": None})
    settling = supervisor.canonical({"settling": True})
    packets = {
        "working": [released], "startup": [],
        "settling_stalled": [released + settling],
        "missing_settling": [released, b""],
        "partial_settling": [released + b'{"settling":', b""],
        "trailing_partial": [released + settling + b"{", b""],
        "exit_before_final_read": [released, settling, b""],
        "nonzero": [released],
        "nonzero_before_release": [b""],
        "exit_before_release_stalled": [None, released],
    }[scenario]
    killed = []

    class Selector:
        def register(self, *args):
            pass

        def unregister(self, *args):
            pass

        def select(self, timeout):
            state.polls += 1
            state.now += 86400 if scenario in {"working", "startup"} else 1
            assert state.polls < 10, "watchdog failed to settle"
            if state.reads < len(packets):
                if packets[state.reads] is None:
                    state.reads += 1
                    return []
                return [(None, None)]
            return []

        def close(self):
            pass

    def read(*args):
        packet = packets[state.reads]
        state.reads += 1
        return packet

    def waitpid(*args):
        if scenario not in {
            "exit_before_final_read", "nonzero", "nonzero_before_release",
            "exit_before_release_stalled",
        }:
            return 0, 0
        if state.reaped:
            raise ChildProcessError
        state.reaped = True
        return 42, 7 if scenario.startswith("nonzero") else 0

    monkeypatch.setattr(supervisor, "container_only", lambda: None)
    monkeypatch.setattr(supervisor, "attachment_closed", lambda: (
        state.polls >= 3 and scenario in {"working", "startup"}
    ))
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(supervisor.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(supervisor.os, "WNOHANG", 1, raising=False)
    monkeypatch.setattr(supervisor.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(supervisor.os, "waitpid", waitpid)
    monkeypatch.setattr(supervisor.os, "waitstatus_to_exitcode", lambda s: s,
                        raising=False)
    monkeypatch.setattr(supervisor.os, "read", read)
    monkeypatch.setattr(supervisor.os, "close", lambda *a: None)
    monkeypatch.setattr(supervisor.os, "kill", lambda *a: killed.append(a))
    assert supervisor.watchdog(42, 10, None) == expected
    assert killed == [(-1, supervisor.signal.SIGKILL)]
    if scenario in {"working", "startup"}:
        assert state.now == 3 * 86400
