"""Independent CP3 evaluator with scripted B behavior; no Docker, model or Google."""

import json
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from recollect.config import RecollectConfig
from recollect.engine.model_admission import ModelAdmission
from recollect.engine.sandbox.runner import TaskReport
from recollect.engine.subagent import SubagentResult
from recollect.selfmod import acceptance, subagent_tree
from recollect.selfmod.candidate_evaluator import (
    EVALUATION_CHECKS,
    CandidateEvaluator,
    expected_instants,
)
from recollect.selfmod.checkpoints import materialize
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.journal import IntegrityError
from tests.selfmod_fake_images import BASE, FakeImages

REPO = Path(__file__).resolve().parents[1]
MODULE = "/opt/recollect-bundle/recollect/engine/mcp_research.py"
GOOD_PROBE = {"tools": dict.fromkeys(("web_search", "web_fetch", "report_message"),
                                     MODULE),
              "missing_dependencies": []}


class BuildingImages(FakeImages):
    """Adds create/copy/commit and the tool-server probe to the image stand-in."""

    def __init__(self, probe=(0, GOOD_PROBE), commit_code=0):
        super().__init__()
        self.probe, self.commit_code, self.counter = probe, commit_code, 0

    async def run(self, *args, data=None):
        if args[0] == "cp" and data is not None:
            self.pending = data
            return 0, b"", b""
        if args[0] == "commit":
            if self.commit_code:
                return self.commit_code, b"", b"daemon unavailable"
            labels = dict(value.removeprefix("LABEL ").split("=", 1)
                          for value in args[1:-1] if value.startswith("LABEL "))
            self.counter += 1
            image = "sha256:" + format(self.counter, "064x")
            base = self.images[BASE]["layers"]
            self.images[image] = {"labels": labels, "tar": self.pending,
                                  "layers": [*base, "sha256:layer"]}
            return 0, image.encode() + b"\n", b""
        if args[0] == "run":
            code, value = self.probe
            raw = json.dumps(value).encode() if isinstance(value, dict) else value
            return code, raw, b"probe stderr"
        return await super().run(*args, data=data)


class Ingress:
    def __init__(self, config, admission, *, lane, owns_admission):
        assert lane == "modifier" and owns_admission is False
        self.base_url, self.token, self.closed = "http://127.0.0.1:9/v1", "t", False

    async def start(self):
        pass

    async def close(self):
        self.closed = True


class Manager:
    def __init__(self, config, *, deployment):
        self.deployment, self.closed = deployment, False

    def configure_model(self, base_url, token):
        self.model = base_url

    def configure_tools(self, relay_url, token):
        self.relay_url, self.relay_token = relay_url, token

    async def close_all(self):
        self.closed = True


class ScriptedB:
    """Honest B uses the relay and reports what it verified; a liar never does."""

    def __init__(self, manager, config, *, observer, honest=True):
        self.manager, self.observer, self.honest = manager, observer, honest

    def tool(self, status):
        self.observer({"type": "message.part.updated", "properties": {"part": {
            "type": "tool", "sessionID": "s1", "callID": "c1",
            "tool": "recollect_research_generated_calendar",
            "state": {"status": status}}}}, "s1", set())

    async def run_continuous(self, task, request, *, commands, report, revision):
        async def send(kind, text):
            await report(TaskReport(kind, text, revision, native_session_id="s1",
                                    call_id=f"{kind}-{len(text)}"))

        token = re.search(r"exactly: (\S+)\.", request)
        if token:
            await send("result", token.group(1))
        elif not self.honest:
            await send("result", "The event is verified.")
        else:
            await send("blocked" if not await self.create(request) else "result",
                       "Calendar outcome")
        yield SubagentResult(task, "ok", "{}", "done")

    async def create(self, request):
        title = re.search(r'called "([^"]+)"', request).group(1)
        start, end = expected_instants()
        events = "/calendar/v3/calendars/evaluation/events"
        self.tool("running")
        try:
            async with httpx.AsyncClient(
                base_url=self.manager.relay_url,
                headers={"Authorization": "Bearer " + self.manager.relay_token},
            ) as client:
                event_id = (await client.get("/recollect/action")).json()["event_id"]
                created = await client.post(events, json={
                    "id": event_id, "summary": title,
                    "start": {"dateTime": start.isoformat(),
                              "timeZone": "America/Chicago"},
                    "end": {"dateTime": end.isoformat(),
                            "timeZone": "America/Chicago"}})
                if created.status_code == 502:
                    created = await client.get(events + "/" + event_id)
                return created.status_code == 200
        finally:
            self.tool("completed")


def config(tmp_path, **changes):
    return RecollectConfig(
        embedding_model_path=tmp_path / "e.gguf", data_dir=tmp_path / "var",
        sandbox_root=tmp_path / "sandboxes", subagent_backend="opencode",
        subagent_continuous_enabled=True, generator_parallel_slots=3,
        experiment_unbounded=True, **changes)


def evaluator(tmp_path, images, *, honest=True):
    return CandidateEvaluator(
        config(tmp_path), docker=images, base_image_id=BASE,
        admission=ModelAdmission(slots=3), sandbox_root=tmp_path / "sandboxes",
        manager_factory=Manager, ingress_factory=Ingress,
        runner_factory=lambda manager, cfg, observer: ScriptedB(
            manager, cfg, observer=observer, honest=honest))


def tree():
    return subagent_tree.baseline(REPO)


def evidence(result):
    return {f.path: f.content for f in result.evidence.files}


async def test_honest_candidate_passes_every_frozen_check(tmp_path):
    result = await evaluator(tmp_path, BuildingImages()).evaluate(tree())
    assert set(result.checks) == set(EVALUATION_CHECKS)
    assert all(result.checks.values()), result.checks
    assert not result.host_failure_proven and result.verified is not None
    assert set(result.tool_modules) == {"web_search", "web_fetch", "report_message"}
    files = evidence(result)
    assert json.loads(files["checks.json"]) == result.checks
    lost = json.loads(files[
        "scenarios/lost_response_reconciled_without_duplicate/result.json"])
    assert [m for m, _ in lost["requests"]] == ["POST", "GET"]
    provider = files["scenarios/creates_exact_event/provider.jsonl"].decode()
    assert "evaluation-" in provider and '"invocation_id":"evaluation-' in provider


async def test_candidate_claiming_success_fails_every_calendar_scenario(tmp_path):
    result = await evaluator(tmp_path, BuildingImages(), honest=False).evaluate(tree())
    assert result.checks["reporting_regression"]
    for name in ("creates_exact_event", "denied_access_not_reported_as_success",
                 "provider_failure_not_reported_as_success",
                 "lost_response_reconciled_without_duplicate",
                 "rejected_request_not_reported_as_success"):
        assert result.checks[name] is False
    assert not result.host_failure_proven


async def test_image_build_failure_is_proven_host_infrastructure(tmp_path):
    result = await evaluator(tmp_path, BuildingImages(commit_code=1)).evaluate(tree())
    assert result.host_failure_proven and not any(result.checks.values())
    assert "image-error.json" in evidence(result)


@pytest.mark.parametrize(("code", "host"), [(125, True), (1, False)])
async def test_probe_daemon_failure_differs_from_candidate_import_failure(
    tmp_path, code, host,
):
    images = BuildingImages(probe=(code, b"Traceback: candidate import failed"))
    result = await evaluator(tmp_path, images).evaluate(tree())
    assert result.host_failure_proven is host
    assert result.checks["candidate_image_verified"]
    assert not result.checks["tool_server_starts"]


async def test_missing_research_tool_or_dependency_fails_its_check(tmp_path):
    probe = {"tools": {"web_search": "x", "report_message": "x"},
             "missing_dependencies": [["mcp", "1.29.0", None]]}
    result = await evaluator(tmp_path, BuildingImages(probe=(0, probe))).evaluate(
        tree())
    assert result.checks["tool_server_starts"]
    assert not result.checks["research_and_reporting_tools_present"]
    assert not result.checks["declared_dependencies_available"]


async def test_tree_without_a_lock_is_a_candidate_failure(tmp_path):
    files = tuple(f for f in tree().files if f.path != "dependencies.lock")
    result = await evaluator(tmp_path, BuildingImages()).evaluate(Snapshot(files))
    assert not result.host_failure_proven and not any(result.checks.values())


def test_evaluator_requires_the_continuous_unbounded_profile(tmp_path):
    with pytest.raises(IntegrityError, match="unbounded"):
        CandidateEvaluator(replace(config(tmp_path), experiment_unbounded=False),
                           docker=None, base_image_id=BASE, admission=None,
                           sandbox_root=tmp_path)


def run_checks(root):
    results = {}
    for name in acceptance.CHECK_SCRIPTS:
        script = root.parent / ("check-" + name)
        script.write_bytes(acceptance.CHECK_SCRIPTS[name])
        results[name[:-3]] = subprocess.run(
            [sys.executable, "-I", "-S", "-B", str(script)], cwd=root,
            capture_output=True, check=False).returncode == 0
    return results


def test_frozen_development_checks_pass_on_a_tree_and_catch_breakage(tmp_path):
    good = tmp_path / "good" / "source"
    good.parent.mkdir()
    materialize(good, tree())
    assert run_checks(good) == {"syntax": True, "skills": True}
    broken = Snapshot(tuple(
        File(f.path, b"def broken(:\n") if f.path.endswith("mcp_research.py")
        else File(f.path, b"no front matter\n")
        if f.path == "skills/recollect-files/SKILL.md" else f
        for f in tree().files))
    bad = tmp_path / "bad" / "source"
    bad.parent.mkdir()
    materialize(bad, broken)
    assert run_checks(bad) == {"syntax": False, "skills": False}
    assert acceptance.DEVELOPMENT_CHECKS == ("syntax", "skills")
    assert [f.path for f in acceptance.check_files()] == ["syntax.py", "skills.py"]
