"""Continuous native task control, durable report recovery and cleanup ordering."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from recollect.config import RecollectConfig
from recollect.engine.mcp_research import report_message
from recollect.engine.sandbox.configgen import build_config
from recollect.engine.sandbox.manager import SandboxHandle, SandboxManager
from recollect.engine.sandbox.runner import OpenCodeRunner, TaskCommand


def report_part(kind="accepted", revision=1, related="", session="ses_parent"):
    data = {
        "kind": kind, "text": "Verified report", "revision": revision,
        "related_message_id": related, "sources": [], "artifacts": [],
    }
    return {
        "type": "tool", "sessionID": session,
        "callID": f"call_{kind}_{revision}",
        "tool": "recollect_research_report_message",
        "state": {"status": "completed", "input": data, "output": json.dumps(data)},
    }


def event(part):
    return {"type": "message.part.updated", "properties": {"part": part}}


class NativeProtocol:
    def __init__(self, tmp_path):
        self.parts = []
        self.prompts = []
        self.order = []
        self.first_started = asyncio.Event()
        self.release = asyncio.Event()
        self.config = RecollectConfig(
            embedding_model_path=tmp_path / "embedding.gguf",
            data_dir=tmp_path / "var", sandbox_root=tmp_path / "sandbox",
        )
        self.workdir = tmp_path / "workspace"
        self.workdir.mkdir()
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.request), base_url="http://native"
        )
        self.slot = asyncio.Lock()
        self.manager = SandboxManager(self.config, model_slot=self.slot)
        self.handle = SandboxHandle(
            workdir=self.workdir, port=9, password="test", process=None,
            client=self.client,
        )
        self.manager._handle = self.handle
        self.runner = OpenCodeRunner(self.manager, self.config)

    async def request(self, request):
        path = request.url.path
        if path == "/session" and request.method == "POST":
            return httpx.Response(200, json={"id": "ses_parent"})
        if path.endswith("/abort"):
            self.order.append("abort")
            self.release.set()
            return httpx.Response(200, json=True)
        if request.method == "DELETE":
            self.order.append("delete")
            return httpx.Response(200, json=True)
        if path.endswith("/children"):
            return httpx.Response(200, json=[])
        if path == "/event":
            # The stream dies without carrying the reports. History is authority.
            return httpx.Response(200, content=b"")
        if path.endswith("/message") and request.method == "GET":
            return httpx.Response(200, json=[{
                "info": {"id": "assistant_one", "role": "assistant"},
                "parts": self.parts,
            }])
        if path.endswith("/message") and request.method == "POST":
            prompt = json.loads(request.content)["parts"][0]["text"]
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                self.parts.append(report_part())
                self.first_started.set()
                await self.release.wait()
            else:
                self.parts.append(report_part(revision=2, related="steer_one"))
                self.release.set()
            return httpx.Response(200, json={"parts": [{
                "type": "text", "text": "Research finished with saved findings."
            }]})
        raise AssertionError((request.method, path))


@pytest.mark.asyncio
async def test_live_steering_reuses_session_and_recovers_reports(tmp_path):
    native = NativeProtocol(tmp_path)
    commands = asyncio.Queue()
    reports = []

    async def report(value):
        reports.append(value)

    async def save(path):
        assert path == native.workdir
        native.order.append("save")

    async def consume():
        return [item async for item in native.runner.run_continuous(
            "conversation", "Research batteries", commands=commands,
            report=report, save_workspace=save,
        )]

    # Foreground inference already owns the slot; background ownership must not
    # wait on it because the dedicated ingress now admits each model request.
    await native.slot.acquire()
    running = asyncio.create_task(consume())
    await asyncio.wait_for(native.first_started.wait(), 1)
    await commands.put(TaskCommand("steer_one", "steer", "Include installation", 2))
    await commands.put(TaskCommand("steer_one", "steer", "Include installation", 2))
    items = await asyncio.wait_for(running, 3)
    assert native.slot.locked()
    native.slot.release()
    assert len(native.prompts) == 2
    assert [r.revision for r in reports if r.kind == "accepted"] == [1, 2]
    assert items[-1].status == "ok"
    assert native.order.index("abort") < native.order.index("save")
    assert native.order.index("save") < native.order.index("delete")
    await native.client.aclose()


@pytest.mark.asyncio
async def test_cancel_preserves_files_before_scrub(tmp_path):
    native = NativeProtocol(tmp_path)
    commands = asyncio.Queue()
    reports = []
    saved = []

    async def report(value):
        reports.append(value)

    async def restore(path):
        (path / "prior.md").write_text("saved finding", encoding="utf-8")

    async def save(path):
        assert "abort" in native.order
        saved.append((path / "prior.md").read_text(encoding="utf-8"))

    async def consume():
        return [item async for item in native.runner.run_continuous(
            "conversation", "Research", commands=commands, report=report,
            restore_workspace=restore, save_workspace=save,
        )]

    running = asyncio.create_task(consume())
    await asyncio.wait_for(native.first_started.wait(), 1)
    await commands.put(TaskCommand("cancel_one", "cancel"))
    items = await asyncio.wait_for(running, 3)
    assert saved == ["saved finding"]
    assert not list(native.workdir.iterdir())
    assert items[-1].status == "partial"
    assert any(r.kind == "canceled" for r in reports)
    assert not native.manager._invocation_lock.locked()
    await native.client.aclose()


def test_report_ownership_and_completed_receipt_are_required():
    parse = OpenCodeRunner._report_from_event
    good = report_part()
    assert parse(event(good), "ses_parent", set()) is not None
    assert parse(event(good), "ses_other", set()) is None
    good["state"]["status"] = "running"
    assert parse(event(good), "ses_parent", set()) is None
    bad = report_part()
    bad["state"]["output"] = json.dumps({
        **bad["state"]["input"], "revision": 100,
    })
    assert parse(event(bad), "ses_parent", set()) is None


@pytest.mark.asyncio
async def test_report_tool_bounds_and_receipt():
    data = json.loads(await report_message("finding", "A finding", 1))
    assert data["text"] == "A finding"
    with pytest.raises(ValueError, match="bounded"):
        await report_message("finding", "x" * 4001, 1)


def test_continuous_config_reports_real_context_without_changing_legacy():
    kwargs = dict(base_url="http://model/v1", model="qwen", api_key="test", steps=24)
    old = build_config(Path("."), **kwargs)
    new = build_config(
        Path("."), **kwargs, context_limit=32768, output_limit=2048, continuous=True
    )
    assert old["provider"]["recollect"]["models"]["qwen"]["limit"]["context"] == 200000
    assert new["provider"]["recollect"]["models"]["qwen"]["limit"] == {
        "context": 32768, "output": 2048,
    }
    assert new["mcp"]["recollect_research"]["environment"] == {
        "RECOLLECT_TASK_REPORTING": "1",
    }


@pytest.mark.asyncio
async def test_question_keeps_native_session_until_steered(tmp_path):
    native = NativeProtocol(tmp_path)
    native.release.set()
    commands = asyncio.Queue()
    question_seen = asyncio.Event()

    async def report(value):
        if value.kind == "question":
            question_seen.set()

    # The authoritative history orders acceptance before the later question.
    original = native.request

    async def ordered(request):
        response = await original(request)
        if (request.method == "POST" and request.url.path.endswith("/message")
                and len(native.prompts) == 1):
            native.parts.append(report_part("question"))
        if request.method == "GET" and request.url.path.endswith("/message"):
            response = httpx.Response(200, json=[{
                "info": {"id": "assistant_one", "role": "assistant"},
                "parts": sorted(native.parts, key=lambda p: (
                    p["state"]["input"]["revision"],
                    p["state"]["input"]["kind"] != "accepted",
                )),
            }])
        return response

    native.client._transport = httpx.MockTransport(ordered)

    async def consume():
        return [item async for item in native.runner.run_continuous(
            "conversation", "Research", commands=commands, report=report,
        )]

    running = asyncio.create_task(consume())
    await asyncio.wait_for(question_seen.wait(), 1)
    await asyncio.sleep(0)
    assert not running.done()
    assert native.manager._active is not None
    await commands.put(TaskCommand("steer_one", "steer", "Answer: yes", 2))
    result = (await asyncio.wait_for(running, 3))[-1]
    assert result.status == "ok"
    assert len(native.prompts) == 2
    await native.client.aclose()


@pytest.mark.asyncio
async def test_unacknowledged_latest_instruction_is_partial(tmp_path):
    native = NativeProtocol(tmp_path)
    native.release.set()

    async def no_ack_report(value):
        pass

    original = native.request

    async def no_history(request):
        if request.method == "GET" and request.url.path.endswith("/message"):
            return httpx.Response(200, json=[])
        return await original(request)

    native.client._transport = httpx.MockTransport(no_history)
    items = [item async for item in native.runner.run_continuous(
        "conversation", "Research", commands=asyncio.Queue(), report=no_ack_report,
    )]
    assert items[-1].status == "partial"
    assert "not acknowledged" in items[-1].error
    await native.client.aclose()


@pytest.mark.asyncio
async def test_failed_workspace_save_releases_invocation(tmp_path):
    native = NativeProtocol(tmp_path)
    native.release.set()

    async def report(value):
        pass

    async def save(path):
        raise OSError("archive full")

    with pytest.raises(OSError, match="archive full"):
        _ = [item async for item in native.runner.run_continuous(
            "conversation", "Research", commands=asyncio.Queue(), report=report,
            save_workspace=save,
        )]
    assert native.manager._active is None
    assert not native.manager._invocation_lock.locked()
    await native.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("productive", [True, False])
async def test_native_step_checkpoints_continue_only_with_new_evidence(
    tmp_path, productive,
):
    native = NativeProtocol(tmp_path)
    native.release.set()
    original = native.request
    checkpoints = []

    async def capped(request):
        response = await original(request)
        if request.method == "POST" and request.url.path.endswith("/message"):
            count = len(native.prompts)
            native.parts.append({
                "type": "tool", "sessionID": "ses_parent",
                "callID": f"search_{count}", "tool": "recollect_research_web_fetch",
                "state": {
                    "status": "completed", "input": {"url": "https://example.org"},
                    "output": str(count) if productive else "unchanged evidence",
                },
            })
            finished = productive and count == 3
            if finished:
                native.parts.append(report_part("result"))
            return httpx.Response(200, json={"parts": [{
                "type": "text", "text": "Research finished." if finished
                else "MAXIMUM STEPS REACHED",
            }]})
        return response

    native.client._transport = httpx.MockTransport(capped)

    async def report(value):
        pass

    async def save(path):
        assert native.order[-1] == "abort"
        checkpoints.append(len(native.prompts))

    items = [item async for item in native.runner.run_continuous(
        "conversation", "Research", commands=asyncio.Queue(), report=report,
        save_workspace=save,
    )]
    assert len(native.prompts) == 3
    assert checkpoints == [1, 2, 3]
    assert items[-1].status == ("ok" if productive else "partial")
    if not productive:
        assert "without new evidence" in items[-1].error
    await native.client.aclose()
