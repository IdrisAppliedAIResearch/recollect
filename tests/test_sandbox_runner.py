"""The opencode runner against a scripted opencode HTTP server.

httpx.MockTransport stands in for the sandbox serve process, so these
tests exercise the event stream, step extraction, long-run completion
(the wallclock abort is gone), and result assembly with the real
server's response shapes - no processes, no model, no network.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from recollect.config import RecollectConfig
from recollect.engine import subagent
from recollect.engine.sandbox.manager import SandboxHandle, SandboxManager
from recollect.engine.sandbox.runner import OpenCodeRunner
from recollect.engine.subagent import SubagentResult, SubagentStep

OC_ID = "ses_test"

#: What opencode actually puts in the session when a run hits its step
#: cap. It appends this to the *request* as an assistant message (with
#: no tools and toolChoice "none") and never stores it, so what ends up
#: in the transcript is the local model reciting it back - arriving as
#: an ordinary text part with nothing to mark it as opencode's words.
#: Verbatim from a live capped run; the fixtures used to use model prose
#: here, which is why they missed a starved finalizer.
OC_STEP_CAP_BANNER = "\n".join(
    [
        "CRITICAL - MAXIMUM STEPS REACHED",
        "",
        "The maximum number of steps allowed for this task has been "
        "reached. Tools are disabled until next user input. Respond with "
        "text only.",
        "",
        "STRICT REQUIREMENTS:",
        "1. Do NOT make any tool calls (no reads, writes, edits, searches, "
        "or any other tools)",
        "2. MUST provide a text response summarizing work done so far",
        "3. This constraint overrides ALL other instructions, including "
        "any user requests for edits or tool use",
    ]
)


def _text_part(text: str) -> dict:
    """A text part with the keys a real opencode part has - notably not
    ``synthetic`` or ``ignored``, which are absent, not false."""
    return {
        "id": "prt_1",
        "messageID": "msg_1",
        "sessionID": OC_ID,
        "type": "text",
        "text": text,
    }

FINAL_JSON = (
    "The sources agree on the broad picture.\n"
    "```json\n"
    "{\n"
    '  "summary": "Mars research points one direction.",\n'
    '  "findings": [\n'
    '    {"claim": "finding one", "source_url": "https://arxiv.org/abs/1"}\n'
    "  ],\n"
    '  "sources": ["https://arxiv.org/abs/1"]\n'
    "}\n"
    "```\n"
)

SEARCH_OUTPUT = json.dumps(
    {
        "tool": "web_search",
        "query": "mars",
        "results": [
            {
                "title": "A paper",
                "url": "https://arxiv.org/abs/1",
                "snippet": "something",
                "source": "arxiv",
            }
        ],
        "errors": [],
    }
) + " " + "word " * 2_000  # well over the 4000-char observation cap


def _sse(*events: dict) -> bytes:
    return b"".join(
        (f"data: {json.dumps(event)}\n\n").encode() for event in events
    )


def _tool_part(
    call_id: str,
    session_id: str,
    tool: str,
    state: dict,
    part_id: str | None = None,
) -> dict:
    return {
        "id": part_id or f"p_{call_id}",
        "sessionID": session_id,
        "messageID": f"m_{call_id}",
        "type": "tool",
        "callID": call_id,
        "tool": tool,
        "state": state,
    }


def _part_updated(part: dict) -> dict:
    return {
        "id": f"e_{part['id']}",
        "type": "message.part.updated",
        "properties": {"part": part},
    }


@pytest.fixture
def config(tmp_path) -> RecollectConfig:
    return RecollectConfig(
        embedding_model_path=tmp_path / "embedding.gguf",
        data_dir=tmp_path / "var",
        # Not optional: the default root is machine-local
        # (%LOCALAPPDATA%), so a test that reaches _spawn writes a
        # workdir into the user's real sandbox root and leaves it there.
        sandbox_root=tmp_path / "sandboxes",
    )


def _inject(config: RecollectConfig, handler, **runner_kwargs):
    async def lifecycle_handler(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("POST", "/session"):
            return httpx.Response(200, json={"id": OC_ID})
        if (request.method, request.url.path) == (
            "DELETE",
            f"/session/{OC_ID}",
        ):
            return httpx.Response(200, json=True)
        return await handler(request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lifecycle_handler),
        base_url="http://127.0.0.1:9",
    )
    workdir = Path(config.sandbox_root) / "s1" / "workspace"
    workdir.mkdir(parents=True)
    handle = SandboxHandle(
        session_id="s1",
        workdir=workdir,
        port=9,
        password="pw",
        process=None,
        client=client,
    )
    manager = SandboxManager(config)
    manager._handles["s1"] = handle
    return OpenCodeRunner(manager, config, **runner_kwargs), handle


async def _run(runner: OpenCodeRunner, task: str):
    items = [item async for item in runner.run("s1", task)]
    steps = [item for item in items if isinstance(item, SubagentStep)]
    results = [item for item in items if isinstance(item, SubagentResult)]
    return steps, results, items


async def test_completed_delegation_yields_steps_and_result(config):
    async def handler(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("GET", "/event"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {  # a researcher subagent session opens
                        "id": "e0",
                        "type": "session.created",
                        "properties": {"info": {"id": "ses_child1", "parentID": OC_ID}},
                    },
                    _part_updated(
                        _tool_part(
                            "c1",
                            OC_ID,
                            "recollect_research_web_search",
                            {
                                "status": "running",
                                "input": {"query": "mars"},
                                # epoch milliseconds, as opencode sends them
                                "time": {"start": 1_000_000_000},
                            },
                        )
                    ),
                    _part_updated(
                        _tool_part(
                            "c1",
                            OC_ID,
                            "recollect_research_web_search",
                            {
                                "status": "completed",
                                "input": {"query": "mars"},
                                "output": SEARCH_OUTPUT,
                                "title": "t",
                                "metadata": {},
                                "time": {"start": 1_000_000_000, "end": 1_000_006_250},
                            },
                        )
                    ),
                    _part_updated(
                        # duplicate completion of the same call: must not
                        # double-count the step
                        _tool_part(
                            "c1",
                            OC_ID,
                            "recollect_research_web_search",
                            {
                                "status": "completed",
                                "input": {"query": "mars"},
                                "output": SEARCH_OUTPUT,
                                "time": {"start": 1_000_000_000, "end": 1_000_006_250},
                            },
                        )
                    ),
                    _part_updated(
                        # a child-session step, tool name not prefixed
                        _tool_part(
                            "c2",
                            "ses_child1",
                            "read",
                            {
                                "status": "completed",
                                "input": {"filePath": "notes.md"},
                                "output": "the notes",
                                "time": {"start": 1_000_002_000, "end": 1_000_002_500},
                            },
                        )
                    ),
                ),
            )
        if (request.method, request.url.path) == (
            "POST",
            f"/session/{OC_ID}/message",
        ):
            body = json.loads(request.content)
            assert body["agent"] == "build"
            assert body["parts"] == [
                {
                    "type": "text",
                    "text": subagent.transfer_task(
                        "find out about mars", "focused"
                    ),
                }
            ]
            await asyncio.sleep(0.3)  # let the frames above flow first
            return httpx.Response(
                200,
                json={
                    "info": {"role": "assistant"},
                    "parts": [_text_part(FINAL_JSON)],
                },
            )
        return httpx.Response(
            404, json={"error": "unexpected " + request.url.path}
        )

    runner, handle = _inject(config, handler)
    steps, results, _ = await _run(runner, "find out about mars")
    await handle.client.aclose()

    assert len(results) == 1
    result = results[0]
    assert result.status == "ok"
    assert result.summary == "Mars research points one direction."
    # Mirror of the legacy contract: on success the sources are the final
    # JSON's own, not the union of everything observed.
    assert result.sources == ["https://arxiv.org/abs/1"]
    assert result.total_ms > 0
    assert not result.error

    assert [step.tool for step in steps] == ["web_search", "read"]
    assert steps[0].ms == 6_250.0
    assert len(steps[0].observation) == 4_000
    assert "https://arxiv.org/abs/1" in steps[0].observation
    assert steps[0].args == {"query": "mars"}
    assert steps[1].args == {"filePath": "notes.md"}


async def test_slow_delegation_completes_without_abort(config):
    # The message request takes far longer than the old 0.2 s wallclock
    # test cap; with the wallclock gone the same run must land ok, with
    # no abort ever sent.
    aborts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("GET", "/event"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {"id": "e1", "type": "server.connected", "properties": {}}
                ),
            )
        if (request.method, request.url.path) == (
            "POST",
            f"/session/{OC_ID}/message",
        ):
            await asyncio.sleep(0.5)
            return httpx.Response(
                200,
                json={
                    "info": {"role": "assistant"},
                    "parts": [_text_part(FINAL_JSON)],
                },
            )
        if (request.method, request.url.path) == ("POST", f"/session/{OC_ID}/abort"):
            aborts.append("abort")
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    runner, handle = _inject(config, handler)
    _, results, _ = await _run(runner, "slow research task")
    await handle.client.aclose()

    assert aborts == []
    result = results[-1]
    assert result.status == "ok"
    assert result.summary == "Mars research points one direction."
    assert result.sources == ["https://arxiv.org/abs/1"]


async def test_request_failure_is_an_error_result(config):
    async def handler(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("GET", "/event"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"",
            )
        if (request.method, request.url.path) == (
            "POST",
            f"/session/{OC_ID}/message",
        ):
            await asyncio.sleep(0.05)
            return httpx.Response(
                500, json={"name": "APIError", "data": {"message": "boom"}}
            )
        return httpx.Response(404, json={})

    runner, handle = _inject(config, handler)
    _, results, _ = await _run(runner, "task")
    await handle.client.aclose()

    result = results[-1]
    assert result.status == "error"
    assert "opencode request failed" in result.error


async def test_unparseable_final_is_partial(config):
    async def handler(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("GET", "/event"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"",
            )
        if (request.method, request.url.path) == (
            "POST",
            f"/session/{OC_ID}/message",
        ):
            await asyncio.sleep(0.05)
            return httpx.Response(
                200,
                json={
                    "info": {"role": "assistant"},
                    "parts": [_text_part(OC_STEP_CAP_BANNER)],
                },
            )
        return httpx.Response(404, json={})

    runner, handle = _inject(config, handler)
    _, results, _ = await _run(runner, "task")
    await handle.client.aclose()

    result = results[-1]
    assert result.status == "partial"
    assert result.error == "malformed or missing final JSON"
    document = json.loads(result.result_json)
    # The text is not relayed: through opencode it cannot be attributed,
    # and this one is opencode's own cap instruction. The sources are what
    # a failed run can honestly hand back.
    assert "partial_text" not in document
    assert "MAXIMUM STEPS REACHED" not in result.result_json
    assert document["sources"] == []


async def test_start_error_becomes_an_error_result(tmp_path):
    config = RecollectConfig(
        embedding_model_path=tmp_path / "embedding.gguf",
        data_dir=tmp_path / "var",
        sandbox_root=tmp_path / "sandboxes",
    )
    manager = SandboxManager(
        config,
        command_factory=lambda port, workdir, password: [
            sys.executable,
            "-c",
            "import sys; sys.exit(3)",
        ],
    )
    runner = OpenCodeRunner(manager, config)
    _, results, _ = await _run(runner, "task")

    assert results[-1].status == "error"
    assert "exited early" in results[-1].error


async def test_native_prose_is_a_success_without_a_second_model_pass(config):
    posts: list[dict] = []
    answer = "The evidence supports the focused answer."

    async def handler(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("GET", "/event"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"",
            )
        if (request.method, request.url.path) == (
            "POST",
            f"/session/{OC_ID}/message",
        ):
            posts.append(json.loads(request.content))
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={"parts": [_text_part(answer)]})
        return httpx.Response(404, json={})

    runner, handle = _inject(config, handler)
    _, results, _ = await _run(runner, "task")
    await handle.client.aclose()

    assert len(posts) == 1
    assert posts[0]["agent"] == "build"
    result = results[-1]
    assert result.status == "ok"
    assert result.summary == answer
    assert json.loads(result.result_json) == {
        "summary": answer,
        "findings": [],
        "sources": [],
    }


async def test_cap_banner_is_never_relayed_or_retried(config):
    posts: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("GET", "/event"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"",
            )
        if (request.method, request.url.path) == (
            "POST",
            f"/session/{OC_ID}/message",
        ):
            posts.append(json.loads(request.content))
            await asyncio.sleep(0.05)
            return httpx.Response(
                200, json={"parts": [_text_part(OC_STEP_CAP_BANNER)]}
            )
        return httpx.Response(404, json={})

    runner, handle = _inject(config, handler)
    _, results, _ = await _run(runner, "task")
    await handle.client.aclose()

    assert len(posts) == 1
    result = results[-1]
    assert result.status == "partial"
    assert result.error == "malformed or missing final JSON"
    assert "MAXIMUM STEPS REACHED" not in result.result_json
    assert "overrides ALL other instructions" not in result.result_json


async def test_receipt_sharing_a_message_with_banner_still_parses(config):
    async def handler(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("GET", "/event"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"",
            )
        if (request.method, request.url.path) == (
            "POST",
            f"/session/{OC_ID}/message",
        ):
            await asyncio.sleep(0.05)
            return httpx.Response(
                200,
                json={
                    "parts": [
                        _text_part(OC_STEP_CAP_BANNER),
                        _text_part(FINAL_JSON),
                    ]
                },
            )
        return httpx.Response(404, json={})

    runner, handle = _inject(config, handler)
    _, results, _ = await _run(runner, "task")
    await handle.client.aclose()

    result = results[-1]
    assert result.status == "ok"
    assert result.summary == "Mars research points one direction."
    assert "MAXIMUM STEPS REACHED" not in result.result_json
