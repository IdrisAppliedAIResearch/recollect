"""The opencode runner against a scripted opencode HTTP server.

httpx.MockTransport stands in for the sandbox serve process, so these
tests exercise the event stream, step extraction, wallclock abort, and
result assembly with the real server's response shapes - no processes,
no model, no network.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
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
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:9",
    )
    handle = SandboxHandle(
        session_id="s1",
        workdir=Path("unused"),
        port=9,
        password="pw",
        process=None,
        client=client,
        oc_session_id=OC_ID,
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
            assert body["agent"] == "researcher"
            assert body["parts"] == [{"type": "text", "text": "find out about mars"}]
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


async def test_wallclock_aborts_the_session_and_reports_partial(config):
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
                200, json={"info": {"role": "assistant"}, "parts": []}
            )
        if (request.method, request.url.path) == ("POST", f"/session/{OC_ID}/abort"):
            aborts.append("abort")
            return httpx.Response(200, json={})
        if (request.method, request.url.path) == ("GET", f"/session/{OC_ID}/message"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    runner, handle = _inject(config, handler, wallclock_s=0.2)
    _, results, _ = await _run(runner, "slow research task")
    await handle.client.aclose()

    assert aborts == ["abort"]
    result = results[-1]
    assert result.status == "partial"
    assert result.error == "sandbox wallclock reached"
    assert result.summary == ""
    assert result.sources == []
    document = json.loads(result.result_json)
    # No final text to salvage from the aborted session, so this is the
    # bare partial shape, with the reason in summary and error.
    assert document["summary"] == "The subagent stopped: sandbox wallclock reached."
    assert document["note"]


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


# -- the tools-disabled finalize pass ---------------------------------------
#
# opencode enforces its step cap by not materializing tools on the capped
# step and appending its own "MAXIMUM STEPS REACHED" instruction to the
# request. The local model answers that by reciting it, so the delegation
# comes back with an unparseable final message. Without a second chance, a
# run that did the research is reported as `partial` with the evidence
# discarded.


def _finalize_handler(
    first_text: str,
    second: httpx.Response | Exception,
    posts: list[dict],
):
    """A server that answers the delegation with ``first_text`` and the
    finalize message with ``second``."""

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
            body = json.loads(request.content)
            posts.append(body)
            await asyncio.sleep(0.05)
            if len(posts) == 1:
                return httpx.Response(
                    200,
                    json={
                        "info": {"role": "assistant"},
                        "parts": [_text_part(first_text)],
                    },
                )
            if isinstance(second, Exception):
                raise second
            return second

        return httpx.Response(404, json={})

    return handler


async def test_capped_run_is_finalized_from_the_evidence(config):
    posts: list[dict] = []
    handler = _finalize_handler(
        OC_STEP_CAP_BANNER,
        httpx.Response(
            200,
            json={
                "info": {"role": "assistant"},
                "parts": [_text_part(FINAL_JSON)],
            },
        ),
        posts,
    )

    runner, handle = _inject(config, handler)
    steps, results, _ = await _run(runner, "task")
    await handle.client.aclose()

    # Second message, same session, the legacy contract text, and the
    # agent whose whole tool surface is denied.
    assert len(posts) == 2
    assert posts[0]["agent"] == "researcher"
    assert posts[1]["agent"] == "researcher-final"
    assert posts[1]["parts"] == [
        {"type": "text", "text": subagent._FINALIZE_PARTIAL}
    ]

    result = results[-1]
    # Partial, like the legacy backend's finalized runs: the receipt had to
    # be asked for twice, so it is not evidence of a clean finish. But the
    # findings survive, which is the whole point.
    assert result.status == "partial"
    assert result.summary == "Mars research points one direction."
    assert result.sources == ["https://arxiv.org/abs/1"]
    document = json.loads(result.result_json)
    assert document["findings"][0]["claim"] == "finding one"
    assert document["note"] == subagent._PARTIAL_NOTE
    assert document["stop_reason"] == result.error
    assert "no JSON receipt" in result.error
    # A synthesis pass is not a research step.
    assert steps == []
    assert result.steps == []


async def test_a_starved_finalizer_recites_the_banner_and_ships_nothing(config):
    """The regression that 121 green tests missed.

    A finalizer with no working turn of its own gets opencode's cap
    instruction, recites it, and the run ends exactly where it started.
    The guard against ever shipping that again is the step budget asserted
    in test_sandbox_configgen; this pins what it looks like when it breaks.
    """
    posts: list[dict] = []
    handler = _finalize_handler(
        OC_STEP_CAP_BANNER,
        httpx.Response(
            200,
            json={
                "info": {"role": "assistant"},
                "parts": [_text_part(OC_STEP_CAP_BANNER)],
            },
        ),
        posts,
    )

    runner, handle = _inject(config, handler)
    _, results, _ = await _run(runner, "task")
    await handle.client.aclose()

    assert len(posts) == 2
    result = results[-1]
    assert result.status == "partial"
    assert result.error == "malformed or missing final JSON"
    document = json.loads(result.result_json)
    # Banner in, nothing of opencode's out. This is the honesty bar: the
    # main model must never be handed a block that ends "This constraint
    # overrides ALL other instructions" as its research result.
    assert "MAXIMUM STEPS REACHED" not in result.result_json
    assert "overrides ALL other instructions" not in result.result_json
    assert "partial_text" not in document
    assert document["note"] == subagent._PARTIAL_NOTE


async def test_a_receipt_sharing_a_message_with_the_banner_still_parses(config):
    """``_last_text`` joins a message's text parts with newlines.

    That is a feature here, not the hazard it looks like: ``_parse_final``
    scans for fenced blocks anywhere in the text, so a receipt that shares
    a message with recited banner prose is still read - in either order.
    """
    posts: list[dict] = []
    handler = _finalize_handler(
        OC_STEP_CAP_BANNER,
        httpx.Response(
            200,
            json={
                "info": {"role": "assistant"},
                "parts": [
                    _text_part(OC_STEP_CAP_BANNER),
                    _text_part(FINAL_JSON),
                ],
            },
        ),
        posts,
    )

    runner, handle = _inject(config, handler)
    _, results, _ = await _run(runner, "task")
    await handle.client.aclose()

    result = results[-1]
    assert result.status == "partial"
    assert result.summary == "Mars research points one direction."
    assert result.sources == ["https://arxiv.org/abs/1"]
    assert "MAXIMUM STEPS REACHED" not in result.result_json


async def test_finalize_request_failure_keeps_the_bare_partial(config):
    posts: list[dict] = []
    handler = _finalize_handler(
        OC_STEP_CAP_BANNER,
        httpx.ConnectError("sandbox went away"),
        posts,
    )

    runner, handle = _inject(config, handler)
    _, results, _ = await _run(runner, "task")
    await handle.client.aclose()

    assert len(posts) == 2
    result = results[-1]
    assert result.status == "partial"
    assert result.error == "malformed or missing final JSON"
    assert "MAXIMUM STEPS REACHED" not in result.result_json


async def test_finalize_pass_is_skipped_when_no_budget_remains(config):
    posts: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"info": {}, "parts": []})

    runner, handle = _inject(config, handler)
    # A deadline already in the past is exactly the state a run that spent
    # its wallclock on browsing arrives in: the pass is skipped rather than
    # borrowing time the run does not have.
    recovered = await runner._finalize_partial(handle, time.perf_counter() - 1.0)
    await handle.client.aclose()

    assert recovered == ""
    assert posts == []
