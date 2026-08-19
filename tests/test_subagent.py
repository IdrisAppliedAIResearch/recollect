"""The ephemeral research subagent.

These tests run the whole loop with a scripted generator and stubbed web
tools: no model, no network. They pin the three properties the design
exists for - the loop's ordering and replay shape, its caps and guards, and
the non-recording contract (a delegated turn's episode contains only the
main model's words, never the subagent's).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

import pytest

import recollect.engine.subagent as subagent_module
from recollect.config import RecollectConfig
from recollect.engine.generator import (
    GenerationError,
    GeneratorSettings,
    StreamChunk,
)
from recollect.engine.subagent import (
    SubagentConfig,
    SubagentResult,
    SubagentStep,
    run_subagent,
)
from recollect.trace import ToolCallTrace
from tests.conftest import FakeEmbedder

FINAL_JSON = (
    "Here is what the sources say.\n"
    "```json\n"
    "{\n"
    '  "summary": "Mars research points one direction.",\n'
    '  "findings": [\n'
    '    {"claim": "finding one", "source_url": "https://arxiv.org/abs/1"}\n'
    "  ],\n"
    '  "sources": ["https://arxiv.org/abs/1", "https://arxiv.org/abs/7"]\n'
    "}\n"
    "```\n"
)


class ScriptedGenerator:
    """A generator that replays a prepared sequence of model responses.

    ``responses`` items are either a final-text string or a list of
    ``(tool_name, arguments_json)`` pairs. ``key_fn`` lets one instance serve
    the main model and the subagent: the key decides which script a call
    consumes, so the two contexts never cross-wire.
    """

    def __init__(self, scripts: dict[str, list], key_fn) -> None:
        self.scripts = {key: list(items) for key, items in scripts.items()}
        self.key_fn = key_fn
        self.calls: list[dict] = []
        self.settings = GeneratorSettings(
            base_url="http://127.0.0.1:9", model="scripted"
        )

    def build_messages(
        self,
        *,
        system_prompt: str,
        context_block: str,
        user_message: str,
    ) -> list[dict]:
        messages = [{"role": "system", "content": system_prompt}]
        if context_block:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your memory of this conversation so far:\n\n"
                        f"{context_block}"
                    ),
                }
            )
        messages.append({"role": "user", "content": user_message})
        return messages

    async def stream(self, messages, *, trace, tools=None, max_tokens=None):
        key = self.key_fn(tools)
        if (
            not tools
            and messages
            and str(messages[0].get("content", "")).startswith(
                "You are a focused research subagent"
            )
        ):
            key = "sub"
        script = self.scripts[key]
        if not script:
            raise AssertionError(f"script {key!r} exhausted")
        response = script.pop(0)
        self.calls.append(
            {
                "key": key,
                "tools": list(tools or ()),
                "messages": [dict(m) for m in messages],
                "max_tokens": max_tokens,
            }
        )
        if isinstance(response, str):
            trace.response_text = response
            trace.finish_reason = "stop"
            for word in response.split(" "):
                yield StreamChunk("token", word + " ")
        else:
            response_text = ""
            tool_calls = response
            if isinstance(response, dict):
                response_text = response.get("text", "")
                tool_calls = response["tool_calls"]
            trace.response_text = response_text
            if response_text:
                yield StreamChunk("token", response_text)
            trace.tool_calls = [
                ToolCallTrace(
                    id=f"call_{len(self.calls)}_{i}", name=name, arguments=args
                )
                for i, (name, args) in enumerate(tool_calls)
            ]
            trace.finish_reason = "tool_calls"


def _key(main_marker: str = "run_subagent", sub_marker: str = "web_search"):
    def key(tools) -> str:
        names = [t["function"]["name"] for t in tools or ()]
        if "web_search" in names:
            return "sub"
        if names:
            return "main"
        return "final"

    return key


def _collect(events):
    steps = [e for e in events if isinstance(e, SubagentStep)]
    result = next(e for e in events if isinstance(e, SubagentResult))
    return steps, result


# ---------------------------------------------------------------------------
# The inner loop
# ---------------------------------------------------------------------------


class TurnEmbedder(FakeEmbedder):
    """FakeEmbedder with the bookkeeping attributes the session layer reads."""

    def __init__(self) -> None:
        super().__init__()
        self.last_cache_hit = False
        self.last_latency_ms = 0.0

    def __call__(self, text: str):
        self.last_latency_ms = 0.001
        return super().__call__(text)


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events = []
    for block in raw.strip().split("\n\n"):
        lines = block.split("\n")
        name = lines[0].removeprefix("event: ").strip()
        data = json.loads(lines[1].removeprefix("data: ").strip())
        events.append((name, data))
    return events


def _make_state(scripts: dict[str, list], config: RecollectConfig) -> object:
    """An AppState without touching a model file or opening a generator.

    Bypasses ``__init__`` on purpose: the real one loads the GGUF embedder
    and dials the llama server. Every collaborator the turn needs is faked.
    """
    from recollect.api import AppState
    from recollect.session import SessionManager

    state = AppState.__new__(AppState)
    state.config = config
    state.sessions = SessionManager(config, TurnEmbedder())
    state.generator = ScriptedGenerator(scripts, _key())
    state.web_client = object()
    state._locks = defaultdict(asyncio.Lock)
    state.embedder_health = {}
    return state


@pytest.fixture
def config(tmp_path) -> RecollectConfig:
    return RecollectConfig(
        embedding_model_path=tmp_path / "unused.gguf",
        data_dir=tmp_path / "var",
    )


@pytest.fixture
def stub_tools(monkeypatch):
    """Replace the web tools with deterministic stubs that record calls."""
    calls: list[tuple] = []

    async def fake_search(client, query, *, max_results=8, state=None):
        calls.append(("search", query, max_results))
        return json.dumps(
            {
                "tool": "web_search",
                "query": query,
                "results": [
                    {
                        "source": "arxiv",
                        "title": f"paper for {query}",
                        "url": f"https://arxiv.org/abs/{len(calls)}",
                        "snippet": "abstract",
                    }
                ],
                "errors": [],
            }
        )

    async def fake_fetch(client, url, *, max_chars=4_000):
        calls.append(("fetch", url, max_chars))
        return json.dumps(
            {
                "tool": "web_fetch",
                "url": url,
                "final_url": url,
                "status": 200,
                "truncated": False,
                "text": f"BODY-OF-{url}",
            }
        )

    monkeypatch.setattr(subagent_module, "web_search", fake_search)
    monkeypatch.setattr(subagent_module, "web_fetch", fake_fetch)
    return calls


async def test_loop_order_replay_shape_and_final_json(stub_tools):
    gen = ScriptedGenerator(
        {
            "sub": [
                [("web_search", '{"query": "mars greenhouse"}')],
                [("web_fetch", '{"url": "https://arxiv.org/abs/7"}')],
                FINAL_JSON,
            ]
        },
        lambda tools: "sub",
    )

    events = [
        item
        async for item in run_subagent(
            object(), gen, "Research the task", config=SubagentConfig()
        )
    ]
    steps, result = _collect(events)

    assert [s.tool for s in steps] == ["web_search", "web_fetch"]
    assert steps[0].args == {"query": "mars greenhouse"}
    assert "BODY-OF-https://arxiv.org/abs/7" in steps[1].observation
    assert result.ok and result.status == "ok"
    assert result.summary == "Mars research points one direction."
    assert result.sources == ["https://arxiv.org/abs/1", "https://arxiv.org/abs/7"]

    # The replay handed to the final generation must carry each assistant
    # turn with its tool call, followed by that call's result, in order.
    final_messages = gen.calls[2]["messages"]
    roles = [m["role"] for m in final_messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assistant = final_messages[2]
    assert assistant["tool_calls"][0]["function"]["name"] == "web_search"
    assert final_messages[3]["tool_call_id"] == assistant["tool_calls"][0]["id"]
    assert final_messages[3]["content"] == steps[0].observation
    assert final_messages[4]["tool_calls"][0]["function"]["name"] == "web_fetch"
    assert final_messages[5]["tool_call_id"] == final_messages[4]["tool_calls"][0]["id"]
    assert final_messages[5]["content"] == steps[1].observation
    # The subagent generation used its own token budget, not the main model's.
    assert gen.calls[0]["max_tokens"] == SubagentConfig().max_tokens


async def test_malformed_final_is_partial_and_keeps_raw_text(stub_tools):
    gen = ScriptedGenerator(
        {
            "sub": [
                [("web_search", '{"query": "q"}')],
                "I cannot produce the required JSON, but the answer is X.",
            ]
        },
        lambda tools: "sub",
    )
    _, result = _collect(
        [item async for item in run_subagent(object(), gen, "task")]
    )
    assert result.status == "partial"
    assert result.error == "malformed or missing final JSON"
    assert "the answer is X" in result.result_json


async def test_step_cap_stops_partial(stub_tools):
    calls = [
        [("web_search", f'{{"query": "q{i}"}}')] for i in range(10)
    ]
    scripts = [*calls[:3], FINAL_JSON, *calls[3:]]
    gen = ScriptedGenerator({"sub": scripts}, lambda tools: "sub")
    events = [
        item
        async for item in run_subagent(
            object(), gen, "task", config=SubagentConfig(max_steps=3)
        )
    ]
    steps, result = _collect(events)
    assert len(steps) == 3
    assert result.status == "partial"
    assert result.error == "step or wallclock limit reached"
    assert "limit" in result.result_json
    # The unspent scripts prove the loop stopped on the cap, not on content.
    assert len(gen.scripts["sub"]) == 7


async def test_total_tool_call_cap_counts_parallel_calls(stub_tools):
    batch = [
        ("web_search", f'{{"query": "q{i}"}}') for i in range(12)
    ]
    gen = ScriptedGenerator({"sub": [batch, FINAL_JSON]}, lambda tools: "sub")
    events = [
        item
        async for item in run_subagent(
            object(),
            gen,
            "task",
            config=SubagentConfig(max_steps=8, max_tool_calls=5),
        )
    ]
    steps, result = _collect(events)

    assert len(steps) == 5
    assert len([call for call in stub_tools if call[0] == "search"]) == 5
    assert result.status == "partial"
    assert result.error == "tool-call limit reached (5)"
    assert "Mars research points one direction" in result.result_json
    final_messages = gen.calls[1]["messages"]
    assistant = next(
        message for message in final_messages if message["role"] == "assistant"
    )
    assert len(assistant["tool_calls"]) == 5
    assert len(
        [message for message in final_messages if message["role"] == "tool"]
    ) == 5


async def test_repeated_call_corrects_then_stops(stub_tools):
    same = [("web_search", '{"query": "same"}')]
    gen = ScriptedGenerator(
        {"sub": [same, same, same, FINAL_JSON]}, lambda tools: "sub"
    )
    events = [item async for item in run_subagent(object(), gen, "task")]
    steps, result = _collect(events)

    # First call executed, repeat corrected in place, second repeat stopped
    # the run: exactly one real tool call and one recorded step.
    assert len([c for c in stub_tools if c[0] == "search"]) == 1
    assert len(steps) == 1
    assert result.status == "partial"
    assert result.error == "repeated the same tool call"


async def test_generator_error_is_an_error_result(stub_tools):
    class Broken(ScriptedGenerator):
        async def stream(self, messages, *, trace, tools=None, max_tokens=None):
            raise GenerationError("boom")
            yield  # pragma: no cover - makes this an async generator

    _, result = _collect(
        [
            item
            async for item in run_subagent(
                object(), Broken({}, lambda t: "sub"), "task"
            )
        ]
    )
    assert result.status == "error"
    assert "boom" in (result.error or "")
    assert "generator" in result.result_json


# ---------------------------------------------------------------------------
# The turn: what reaches the episode store and the trace
# ---------------------------------------------------------------------------


async def _run_turn(state, session_id, message: str) -> list[tuple[str, dict]]:
    from recollect.api import _stream_turn

    raw = "".join([part async for part in _stream_turn(state, session_id, message)])
    return _parse_sse(raw)


def _episode_rows(state, session_id) -> list[dict]:
    from recollect.engine._internals import read_episodes

    store = state.sessions.open_store(session_id)
    try:
        return list(read_episodes(store))
    finally:
        store.close()


def _load_trace(config, session_id) -> dict:
    files = list(config.traces_dir(session_id).glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


async def test_delegated_turn_records_only_main_text_and_summary(stub_tools, config):
    state = _make_state(
        {
            "main": [
                {
                    "text": (
                        "PHASE_ONE_PREAMBLE </think> <tool_call>"
                        "function=run_subagent"
                    ),
                    "tool_calls": [
                        ("run_subagent", '{"task": "find the mars paper"}')
                    ],
                }
            ],
            "sub": [
                [("web_search", '{"query": "mars greenhouse"}')],
                [("web_fetch", '{"url": "https://arxiv.org/abs/7"}')],
                FINAL_JSON,
            ],
            "final": ["Based on the sources, the answer is definitive."],
        },
        config,
    )
    session = state.sessions.create_session("test")
    message = "What does the research say?"
    events = await _run_turn(state, session.session_id, message)
    names = [name for name, _ in events]

    # The subagent's work streamed as its own events between the two main
    # model generations, before the turn completes.
    assert "subagent_start" in names
    assert names.count("subagent_step") == 2
    assert "subagent_done" in names
    assert (
        names.index("subagent_start")
        < names.index("subagent_done")
        < names.index("done")
    )
    assert events[names.index("done")][1]["turn_id"]

    # A real local model may duplicate its structured call as malformed
    # content. Phase one stays invisible; only the final answer is a token.
    visible_text = "".join(
        data["text"] for name, data in events if name == "token"
    )
    assert "PHASE_ONE_PREAMBLE" not in visible_text
    assert "</think>" not in visible_text
    assert "<tool_call>" not in visible_text
    final_call = next(call for call in state.generator.calls if call["key"] == "final")
    assert final_call["messages"][-2]["content"] == ""

    # The UI renders each step event verbatim: one nested "step" object per
    # event, 1-based index, exactly the keys the row has cells for.
    step_events = [data for name, data in events if name == "subagent_step"]
    assert [event["step"]["index"] for event in step_events] == [1, 2]
    assert [event["step"]["tool"] for event in step_events] == [
        "web_search",
        "web_fetch",
    ]
    assert step_events[0]["step"]["args"] == {"query": "mars greenhouse"}
    for event in step_events:
        assert event["run_id"]
        assert set(event["step"]) == {
            "index",
            "tool",
            "args",
            "observation",
            "ms",
        }

    # The episode is the user's message and the main model's final answer -
    # nothing the subagent saw or said is in it.
    rows = _episode_rows(state, session.session_id)
    pairs = [(r["user_message"], r["assistant_message"]) for r in rows]
    assert pairs == [(message, "Based on the sources, the answer is definitive.")]
    assert all(
        "BODY-OF-" not in p
        and "abstract" not in p
        and "PHASE_ONE_PREAMBLE" not in p
        and "</think>" not in p
        and "<tool_call>" not in p
        for _, p in pairs
    )

    # The turn's trace carries the one-line subagent summary - and only it.
    trace = _load_trace(config, session.session_id)
    sub = trace["subagent"]
    assert sub["status"] == "ok"
    assert sub["steps"] == 2
    assert sub["tools_used"] == ["web_fetch", "web_search"]
    assert sub["sources"] == ["https://arxiv.org/abs/1", "https://arxiv.org/abs/7"]
    assert sub["returned_chars"] > 0
    assert sub["task"] == "find the mars paper"
    assert "BODY-OF-" not in json.dumps(trace)


async def test_raw_json_final_is_repaired_before_stream_or_storage(
    stub_tools, config
):
    natural_answer = "The evidence supports the finding with one caveat."
    state = _make_state(
        {
            "main": [
                [("run_subagent", '{"task": "find the mars paper"}')]
            ],
            "sub": [FINAL_JSON],
            # The first phase-two draft repeats the internal result. The
            # second is the one bounded repair attempt.
            "final": [FINAL_JSON, natural_answer],
        },
        config,
    )
    session = state.sessions.create_session("test")
    events = await _run_turn(state, session.session_id, "What did it find?")

    visible_text = "".join(
        data["text"] for name, data in events if name == "token"
    )
    assert natural_answer in visible_text
    assert '"findings"' not in visible_text
    assert "```json" not in visible_text

    final_calls = [
        call for call in state.generator.calls if call["key"] == "final"
    ]
    assert len(final_calls) == 2
    assert "INTERNAL RESEARCH RESULT" in final_calls[0]["messages"][-1]["content"]
    assert final_calls[0]["messages"][-2]["content"] == ""
    assert "Rewrite it as" in final_calls[1]["messages"][-1]["content"]

    rows = _episode_rows(state, session.session_id)
    assert [(row["user_message"], row["assistant_message"]) for row in rows] == [
        ("What did it find?", natural_answer)
    ]
    assert all(
        "Mars research points" not in row["assistant_message"]
        for row in rows
    )

    trace = _load_trace(config, session.session_id)
    assert trace["generation"]["response_text"] == natural_answer
    assert '"findings"' not in trace["generation"]["response_text"]


async def test_second_raw_json_draft_uses_safe_markdown_fallback(
    stub_tools, config
):
    state = _make_state(
        {
            "main": [
                [("run_subagent", '{"task": "find the mars paper"}')]
            ],
            "sub": [FINAL_JSON],
            "final": [FINAL_JSON, FINAL_JSON],
        },
        config,
    )
    session = state.sessions.create_session("test")
    events = await _run_turn(state, session.session_id, "What did it find?")
    visible_text = "".join(
        data["text"] for name, data in events if name == "token"
    )

    assert visible_text.startswith("Mars research points one direction.")
    assert "- finding one ([source](https://arxiv.org/abs/1))" in visible_text
    assert '"findings"' not in visible_text
    assert "```json" not in visible_text

    rows = _episode_rows(state, session.session_id)
    assert rows[0]["assistant_message"] == visible_text


async def test_plain_turn_is_unchanged(stub_tools, config):
    state = _make_state(
        {
            "main": ["A direct answer with no delegation."],
            "sub": [],
            "final": [],
        },
        config,
    )
    session = state.sessions.create_session("test")
    events = await _run_turn(state, session.session_id, "hello")
    names = [name for name, _ in events]

    assert "subagent_start" not in names
    assert "subagent_step" not in names
    assert "subagent_done" not in names
    assert "done" in names
    # No subagent tool was ever executed.
    assert stub_tools == []

    rows = _episode_rows(state, session.session_id)
    assert [(r["user_message"], r["assistant_message"]) for r in rows] == [
        ("hello", "A direct answer with no delegation.")
    ]

    trace = _load_trace(config, session.session_id)
    assert trace["subagent"] is None
    # The recorded generation is the single main-model answer.
    assert (
        trace["generation"]["response_text"]
        == "A direct answer with no delegation."
    )
