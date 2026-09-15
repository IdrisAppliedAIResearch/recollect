"""Manifest-input collection is read-only and never declares registration ready."""

import hashlib
import json
from types import SimpleNamespace

from recollect.selfmod import readiness


def fake_runner(outputs):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        key = " ".join(argv[:3])
        code, stdout = outputs.get(key, (0, b""))
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=b"")

    runner.calls = calls
    return runner


def test_immutable_receipts_detect_drift(tmp_path):
    (tmp_path / "SELF_MODIFICATION_PREREGISTRATION.md").write_bytes(b"frozen\n")
    digest = hashlib.sha256(b"frozen\n").hexdigest()
    for receipt in readiness.IMMUTABLE_RECEIPTS:
        (tmp_path / receipt).write_text("")
    (tmp_path / "SELF_MODIFICATION_PREREGISTRATION.sha256").write_text(
        f"{digest}  SELF_MODIFICATION_PREREGISTRATION.md\n")
    assert readiness.immutable_hashes(tmp_path)[0]["matches"]
    (tmp_path / "SELF_MODIFICATION_PREREGISTRATION.md").write_bytes(b"edited\n")
    assert not readiness.immutable_hashes(tmp_path)[0]["matches"]


def test_junit_receipts_report_failures_not_just_existence(tmp_path):
    (tmp_path / "pass.xml").write_text(
        '<testsuites><testsuite tests="5" failures="0" errors="0" skipped="1"/>'
        "</testsuites>")
    (tmp_path / "fail.xml").write_text(
        '<testsuite tests="3" failures="1" errors="0" skipped="0"/>')
    (tmp_path / "broken.xml").write_text("<testsuite")
    receipts = {r["file"]: r for r in readiness.junit_receipts(tmp_path)}
    assert receipts["pass.xml"]["passed"] == 4 and receipts["pass.xml"]["clean"]
    assert not receipts["fail.xml"]["clean"]
    assert receipts["broken.xml"]["parse_error"]


def test_collection_lists_user_gated_inputs_and_is_never_ready(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".agent").mkdir()
    for receipt in readiness.IMMUTABLE_RECEIPTS:
        (repo / "docs" / receipt).write_text("")
    (repo / "untracked.py").write_bytes(b"x = 1\n")
    runner = fake_runner({
        "git rev-parse HEAD": (0, b"abc123\n"),
        "git branch --show-current": (0, b"selfmodifying-experiment\n"),
        "git status --porcelain=v1": (0, b"?? untracked.py\0"),
        "git ls-files --others": (0, b"untracked.py\0"),
        "nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader":
            (0, b"GPU\n"),
    })
    inputs = readiness.collect(
        repo, tmp_path / "out", runner=runner, model_url="http://127.0.0.1:8001/v1",
        fetch=lambda url: {"total_slots": 1,
                           "default_generation_settings": {"n_ctx": 131072}},
    )
    assert inputs["ready_for_registration"] is False
    assert inputs["registration"] == "not_performed" and inputs["cp0"] == "not_sealed"
    assert "model_server_not_three_slot" in inputs["unresolved"]
    assert set(readiness.USER_GATED) <= set(inputs["unresolved"])
    assert inputs["git"]["untracked_files"] == 1 and not inputs["git"]["clean"]
    written = json.loads((tmp_path / "out" / "manifest-inputs.json").read_text())
    assert written["git"]["head"] == "abc123"
    # Collection issues only observation commands.
    assert all(call[0] in {"git", "nvidia-smi"} for call in runner.calls)
    mutating = {"commit", "push", "stop", "kill", "rm", "restart", "reset"}
    assert not any(set(call) & mutating for call in runner.calls)
