"""Native capture parsing is evidence validation, never execution authority."""

import base64
from dataclasses import asdict, replace

import pytest

from recollect.selfmod.contracts import ChangePolicy, File, Snapshot
from recollect.selfmod.journal import IntegrityError, decode, encode, sha256
from recollect.selfmod.native_admission import NativeRun
from recollect.selfmod.native_capture import (
    NativeCaptureSpec,
    capture_request,
    verify_capture,
    verify_stop,
)
from tests.selfmod_containment_helpers import spec as fixture_spec
from tests.test_selfmod_native import settings
from tests.test_selfmod_native_admission import admit
from tests.test_selfmod_native_admission import case as case
from tests.test_selfmod_native_history import archive as archive
from tests.test_selfmod_native_history import db as db
from tests.test_selfmod_native_history import history as native_history

NATIVE = {"pid": 42, "start": 1234}


def spec():
    fixture = fixture_spec()
    run = NativeRun("a" * 32, "controller", "cycle", "grant", "author", 1,
                    "implement", fixture.binding)
    authority = {**decode(settings().authority), "run": asdict(run),
                 "policy": asdict(fixture.policy),
                 "baseline_sha256": fixture.baseline.sha256}
    return NativeCaptureSpec(run, replace(settings(), policy=fixture.policy,
                                         authority=encode(authority)),
                             fixture.baseline)


def stop(config):
    def task(pid, start, root=False):
        uid, mask = (0, 0xE5) if root else (65532, 0)
        return {"pid": pid, "tid": pid, "start": start, "state": "S",
                "uids": [uid] * 4, "gids": [uid] * 4, "groups": [],
                "nnp": 1, "seccomp": 2,
                "caps": {"CapEff": mask, "CapPrm": mask, "CapInh": 0,
                         "CapAmb": 0, "CapBnd": 0xE5}}
    return encode({"kind": "native_stopped", "spec_sha256": config.sha256,
                   "native": NATIVE,
                   "before": [task(1, 10, True), task(99, 5000, True), task(42, 1234)],
                   "after": []})


def result(config, request, *, changed=None):
    files = {f.path: f.content for f in config.baseline.files}
    files["editable.py"] = b"value = 2\n"
    files.update(changed or {})
    entries = []
    for path, data in files.items():
        editable = path in config.policy.modify
        fresh = config.policy.permits(path, "create")
        entries.append({"path": path, "kind": "file", "mode": (
            0o664 if editable else 0o644 if fresh else 0o444),
            "uid": 65532 if fresh else 0, "gid": 65532 if editable or fresh else 0,
            "links": 1, "bytes": len(data)})
    entries.append({"path": "generated", "kind": "directory", "mode": 0o775,
                    "uid": 0, "gid": 65532, "links": 2, "bytes": 40})
    snapshot = Snapshot(tuple(File(p, d) for p, d in files.items()))
    return {"kind": "native_capture", "request": decode(request),
            "snapshot_sha256": snapshot.sha256,
            "files": [{"path": p, "base64": base64.b64encode(d).decode()}
                      for p, d in files.items()], "entries": entries,
            "identities": [{"path": p, "device": 1, "inode": i + 1}
                           for i, p in enumerate([".", *(e["path"] for e in entries)])]}


def test_manifest_freezes_helpers_authority_policy_and_baseline():
    config = spec()
    inventory = {f.path: f.content for f in config.inputs.files}
    envelope = decode(inventory["capture-spec.json"])
    assert envelope["spec_sha256"] == sha256(encode(envelope["payload"]))
    assert inventory["task.json"] == config.settings.authority
    assert envelope["payload"]["baseline_sha256"] == config.baseline.sha256
    for path, digest in envelope["payload"]["helpers"].items():
        assert sha256(inventory[path]) == digest
    assert decode(config.stop_request(NATIVE))["native"] == NATIVE
    original_sha = config.sha256
    payload = config.payload
    payload["limits"]["files"] = 1000000
    payload["helpers"].clear()
    assert config.sha256 == original_sha


async def test_capture_spec_uses_actual_admission_context_without_receipt(case):
    admission = admit(case)
    try:
        config = NativeCaptureSpec(admission.run, admission.settings,
                                   case.dev._baseline)
        assert config.settings.authority == admission.settings.authority
        assert config.run.binding == case.dev.binding
        assert case.dev._receipt is None
    finally:
        await admission.close()
    assert case.dev._receipt is None


@pytest.mark.parametrize("native", [{}, {"pid": True, "start": 1},
                                   {"pid": 1, "start": 1},
                                   {"pid": 2, "start": "1"}])
def test_requires_native_pid_and_start_identity(native):
    with pytest.raises(IntegrityError):
        spec().stop_request(native)


@pytest.mark.parametrize("fault", ["baseline", "binding", "missing_modify",
                                   "overlap", "size", "run", "authority"])
def test_invalid_capture_spec_rejected(fault):
    config = spec()
    if fault == "baseline":
        changes = {"settings": settings()}
    elif fault == "binding":
        changes = {"run": replace(config.run, binding=replace(
            config.run.binding, baseline_sha256="b" * 64))}
    elif fault == "missing_modify":
        changes = {"settings": replace(config.settings, policy=ChangePolicy(
            config.baseline.sha256, modify=("missing.py",)))}
    elif fault == "overlap":
        changes = {"settings": replace(config.settings, policy=ChangePolicy(
            config.baseline.sha256, create_under=("editable.py/child",)))}
    elif fault == "size":
        source = Snapshot((File("large.py", b"x" * (256 * 1024 + 1)),))
        changes = {"baseline": source, "settings": replace(config.settings,
                   policy=ChangePolicy(source.sha256)), "run": replace(
                       config.run, binding=replace(config.run.binding,
                                                   baseline_sha256=source.sha256))}
    elif fault == "run":
        changes = {"run": replace(config.run, run_id="worker-selected")}
    else:
        changes = {"settings": replace(config.settings, authority=settings().authority)}
    if fault in {"missing_modify", "overlap", "size"}:
        next_settings = changes["settings"]
        changes["settings"] = replace(next_settings, authority=encode({
            **decode(config.settings.authority),
            "run": asdict(changes.get("run", config.run)),
            "policy": asdict(next_settings.policy),
            "baseline_sha256": changes.get("baseline", config.baseline).sha256,
        }))
    with pytest.raises(ValueError):
        replace(config, **changes)


@pytest.mark.parametrize("fault", ["run", "pid", "start", "paused", "running",
                                   "empty_before", "extra", "noncanonical",
                                   "pid_float", "start_float", "bad_task",
                                   "missing_native", "duplicate_task", "bool_cred"])
def test_stop_record_is_bound_and_pausing_is_not_termination(fault):
    config = spec()
    value = decode(stop(config))
    if fault == "run":
        value["spec_sha256"] = "b" * 64
    elif fault in ("pid", "start"):
        value["native"][fault] += 1
    elif fault in ("paused", "running"):
        value["after"] = [{"state": "T" if fault == "paused" else "R"}]
    elif fault == "empty_before":
        value["before"] = []
    elif fault == "extra":
        value["success"] = True
    elif fault in ("pid_float", "start_float"):
        key = fault.removesuffix("_float")
        value["native"][key] = float(value["native"][key])
    elif fault == "bad_task":
        value["before"] = [{}]
    elif fault == "missing_native":
        value["before"].pop()
    elif fault == "duplicate_task":
        value["before"].append(value["before"][-1])
    elif fault == "bool_cred":
        value["before"][-1]["nnp"] = True
    raw = encode(value) + (b" " if fault == "noncanonical" else b"")
    with pytest.raises(IntegrityError):
        verify_stop(raw, config, NATIVE)


async def test_exact_source_capture_requires_real_final_history(db, archive):
    config = spec()
    history, _ = native_history(db, archive)
    with pytest.raises(IntegrityError, match="finalized"):
        capture_request(config, stop(config), NATIVE, history)
    await history.capture()
    with pytest.raises(IntegrityError, match="finalized"):
        capture_request(config, stop(config), NATIVE, history)
    await history.finalize()
    request = capture_request(config, stop(config), NATIVE, history)
    value = result(config, request, changed={"generated/new.py": b"new = True\n"})
    captured = verify_capture(encode(value), config, stop(config), NATIVE, history)
    assert {f.path: f.content for f in captured.files}["editable.py"] == b"value = 2\n"
    assert captured.sha256 == value["snapshot_sha256"]
    assert not hasattr(captured, "receipt")


@pytest.mark.parametrize("fault", ["spec", "stop", "head", "head_sha", "session",
                                   "digest", "drift", "new", "missing", "link",
                                   "mode", "inode", "forged_history", "extra",
                                   "head_float", "head_bool", "alias_inode",
                                   "bad_inode"])
async def test_capture_mismatches_never_return_candidate(db, archive, fault):
    config = spec()
    history, _ = native_history(db, archive)
    await history.finalize()
    request = capture_request(config, stop(config), NATIVE, history)
    value = result(config, request)
    fields = {"spec": "spec_sha256", "stop": "stop_sha256", "head": "head",
              "head_sha": "head_sha256", "session": "session_id"}
    if fault in fields:
        value["request"][fields[fault]] = "wrong"
    elif fault == "digest":
        value["snapshot_sha256"] = "b" * 64
    elif fault in ("drift", "new"):
        value = result(config, request, changed={
            "protected.py" if fault == "drift" else "unrelated.py": b"drift"})
    elif fault == "missing":
        value["files"].pop()
    elif fault == "link":
        value["entries"][0]["links"] = 2
    elif fault == "mode":
        value["entries"][0]["mode"] = 0o777
    elif fault == "inode":
        value["identities"].pop()
    elif fault == "extra":
        value["execution_receipt"] = True
    elif fault in ("head_float", "head_bool"):
        value["request"]["head"] = (float(value["request"]["head"])
                                    if fault == "head_float" else False)
    elif fault == "alias_inode":
        value["identities"][-1]["inode"] = value["identities"][0]["inode"]
    elif fault == "bad_inode":
        value["identities"][-1] = None
    else:
        history = history.final_metadata
    with pytest.raises(IntegrityError):
        verify_capture(encode(value), config, stop(config), NATIVE, history)
