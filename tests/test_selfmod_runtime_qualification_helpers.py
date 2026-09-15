"""Offline regressions for qualification helpers, not additional live evidence."""

import json
from types import SimpleNamespace

import pytest

from tests.selfmod_containment_helpers import CONTAINER_ID, spec
from tests.test_selfmod_runtime_docker import assert_faults, cleanup_runtime


@pytest.mark.parametrize("fault", [None, "close", "lookup", "foreign", "remove"])
async def test_teardown_reconciles_despite_pipe_failure_and_refuses_foreign(fault):
    fixture, calls = spec(), []
    state = {"exists": True, "running": True}

    def close():
        calls.append("close")
        if fault == "close":
            raise OSError("pipe close failed")

    async def ids(config):
        assert config == fixture
        calls.append("lookup")
        if fault == "lookup":
            raise OSError("lookup failed")
        return {CONTAINER_ID} if state["exists"] else set()

    async def observe(*args):
        calls.append(args[0])
        assert args[-1] == CONTAINER_ID
        if args[0] == "inspect":
            return json.dumps([{
                "Id": CONTAINER_ID, "Name": "/" + fixture.name,
                "Image": fixture.image_id,
                "Config": {"Labels": {
                    "recollect.selfmod": "foreign" if fault == "foreign"
                    else fixture.run_id,
                    "recollect.spec": fixture.sha256,
                }},
                "State": {
                    "Running": state["running"],
                    "Status": "running" if state["running"] else "exited",
                    "Pid": 10 if state["running"] else 0,
                },
            }]).encode()
        if args[0] == "kill":
            assert args == ("kill", "--signal=KILL", CONTAINER_ID)
            state["running"] = False
        elif args[0] == "rm":
            assert args == ("rm", CONTAINER_ID)
            if fault == "remove":
                raise OSError("remove failed")
            state["exists"] = False
        else:
            raise AssertionError(args)
        return b""

    runtime = SimpleNamespace(
        _spec=fixture, _attachment=SimpleNamespace(close=close),
    )
    errors = await cleanup_runtime(runtime, ids, observe)
    assert bool(errors) is (fault is not None)
    assert calls[:2] == ["close", "lookup"]
    if fault in {None, "close"}:
        assert not state["exists"]
        assert calls == ["close", "lookup", "inspect", "kill", "inspect", "rm",
                         "lookup"]
        if fault == "close":
            assert len(errors) == 1 and str(errors[0]) == "pipe close failed"
    else:
        assert state["exists"]
        if fault in {"lookup", "foreign"}:
            assert "kill" not in calls and "rm" not in calls


@pytest.mark.parametrize("result", [None, (b"", b"ordinary CLI failure", 1)])
def test_failed_native_lookup_cannot_qualify_as_lost_successful_response(result):
    probe = SimpleNamespace(faults=[("container", SimpleNamespace(result=result))])
    with pytest.raises(AssertionError):
        assert_faults(probe, "container", spec(), None)


def test_missing_injection_cannot_qualify():
    with pytest.raises(AssertionError, match="not injected"):
        assert_faults(SimpleNamespace(faults=[]), "container", spec(), None)
