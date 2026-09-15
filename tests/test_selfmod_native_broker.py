"""Deterministic host broker tests, not native/runtime qualification."""

import asyncio
import json
import threading
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest

from recollect.selfmod import native_broker
from recollect.selfmod.journal import IntegrityError, Journal, encode
from recollect.selfmod.native_broker import (
    TOKEN_CAP_FIELDS,
    BrokerIdentity,
    BrokerSettings,
    NativeModelBroker,
)

SETTINGS = BrokerSettings("http://127.0.0.1:8001/v1", "fixture-model")
IDENTITY = BrokerIdentity("run_host", "ses_host")
RAW = encode({"model": SETTINGS.model, "messages": [{"role": "user", "content": "Hi"}]})


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks=(b"data: first\n\n", b"data: [DONE]\n\n"), error=None):
        self.chunks = chunks
        self.error = error
        self.close_count = 0
        self.close_entered = asyncio.Event()
        self.close_release = None

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.close_count += 1
        self.close_entered.set()
        if self.close_release:
            await self.close_release.wait()


class Sink:
    def __init__(self):
        self.heads = []
        self.chunks = []

    async def head(self, value):
        self.heads.append(value)

    async def chunk(self, value):
        self.chunks.append(value)


@pytest.fixture
async def setup(tmp_path):
    brokers, journals = [], []

    def make(handler=None, *, settings=SETTINGS, fault=lambda _: None):
        stream = Stream()
        requests = []

        async def server(request):
            requests.append(request)
            assert all(v is None for v in request.extensions["timeout"].values())
            if handler:
                return await handler(request)
            return httpx.Response(
                200, stream=stream, headers={"content-type": "text/event-stream"},
            )

        journal = Journal.create(tmp_path / f"journal-{len(journals)}", fault=fault)
        journals.append(journal)
        broker = NativeModelBroker(
            settings, journal, transport=httpx.MockTransport(server),
        )
        brokers.append(broker)
        return broker, journal, requests, stream

    yield make
    for broker in brokers:
        await broker.close()
    for journal in journals:
        journal.close()


async def forward(
    broker, raw=RAW, *, guard=lambda: None, sink=None, identity=IDENTITY, **kwargs,
):
    sink = sink or Sink()
    return await broker.forward(
        raw, identity=identity, guard=guard,
        on_response=sink.head, on_chunk=sink.chunk, **kwargs,
    )


def records(journal, kind):
    return [r for r in journal.verify() if r.value["kind"] == "native_broker_" + kind]


def data(journal, kind):
    return [r.value["data"] for r in records(journal, kind)]


def archived(journal, kind):
    return b"".join(f.content for r in records(journal, kind) for f in r.files.files)


async def test_exact_original_and_only_top_level_cap_removal(setup):
    broker, journal, requests, _ = setup()
    payload = {
        "model": SETTINGS.model, "stream": True,
        "messages": [{"role": "user", "content": '{"max_tokens":7,"url":"text"}'}],
        "metadata": {"max_output_tokens": 9, "run_id": "forged"},
        "tools": [{"type": "function", "function": {
            "name": "edit", "parameters": {"max_completion_tokens": 12},
        }}],
        "stream_options": {"include_usage": True},
        "max_tokens": 1, "max_completion_tokens": 2, "max_output_tokens": 3,
    }
    raw = json.dumps(payload, indent=3).encode()
    sink = Sink()
    result = await forward(broker, raw, sink=sink)
    assert result is sink.heads[0] and result.status_code == 200
    assert requests[0].url == "http://127.0.0.1:8001/v1/chat/completions"
    assert requests[0].method == "POST"
    assert requests[0].headers["accept-encoding"] == "identity"
    assert "authorization" not in requests[0].headers
    assert archived(journal, "original") == raw
    assert archived(journal, "request") == requests[0].content
    sent = json.loads(requests[0].content)
    assert sent == {k: v for k, v in payload.items() if k not in TOKEN_CAP_FIELDS}
    assert json.loads(raw) == payload
    assert all(r.value["data"]["run_id"] == IDENTITY.run_id for r in journal.verify())
    assert all(r.value["data"]["session_id"] == IDENTITY.session_id
               for r in journal.verify())
    assert archived(journal, "chunk") == b"".join(sink.chunks)
    with pytest.raises(FrozenInstanceError):
        result.status_code = 503


@pytest.mark.parametrize("role", [
    "system", "developer", "user", "assistant", "tool", "function",
])
@pytest.mark.parametrize("part", [
    {"type": "image_url", "image_url": {"url": "https://media.invalid/pic"}},
    {"type": "image_url", "image_url": "http://127.0.0.1/private"},
    {"type": "input_image", "image_url": "file:///private/image"},
    {"type": "image", "url": "https://media.invalid/pic"},
    {"type": "input_audio", "input_audio": {"data": "YWJj", "format": "wav"}},
    {"type": "audio_url", "audio_url": {"url": "https://media.invalid/audio"}},
    {"type": "audio", "url": "https://media.invalid/audio"},
    {"type": "video_url", "video_url": {"url": "https://media.invalid/video"}},
    {"type": "input_video", "video_url": "https://media.invalid/video"},
    {"type": "video", "url": "https://media.invalid/video"},
    {"type": "file", "file": {"file_id": "remote-file"}},
    {"type": "file", "file": {"file_data": "data:application/pdf;base64,YWJj"}},
    {"type": "input_file", "file_url": "https://media.invalid/file"},
    {"type": "unknown", "url": "https://media.invalid/unknown"},
    {"type": "text", "text": "ok", "image_url": {"url": "https://media.invalid"}},
    {"type": "text", "text": "ok", "metadata": {"nested": {"url": "http://x"}}},
    {"type": "text", "text": {"url": "https://media.invalid"}},
    {"type": "text", "text": [{"type": "image_url", "url": "http://x"}]},
    {"type": "text", "text": None}, {"type": "text", "text": 1},
    {"type": "text"}, {"text": "missing type"}, "text", None,
])
async def test_rejects_media_and_unknown_parts_for_every_role(setup, role, part):
    message = {"role": role, "content": [{"type": "text", "text": "ok"}, part]}
    if role == "tool":
        message["tool_call_id"] = "call_1"
    if role == "function":
        message["name"] = "edit"
    broker, journal, requests, _ = setup()
    raw = encode({"model": SETTINGS.model, "messages": [message]})
    with pytest.raises(IntegrityError):
        await forward(broker, raw)
    assert not requests
    assert archived(journal, "original") == raw
    assert not records(journal, "request")
    assert data(journal, "end")[0]["dispatch_attempted"] is False


@pytest.mark.parametrize("message", [
    {"role": "user", "content": "text", "url": "https://media.invalid"},
    {"role": "user", "content": "text", "metadata": {"route": {"url": "http://x"}}},
    {"role": "assistant", "content": "text", "audio": {"url": "http://x"}},
    {"role": "user", "content": {"type": "text", "text": "not an array"}},
    {"role": "user", "content": None}, {"role": "assistant", "content": None},
    {"role": "user"}, {"role": "assistant"}, {"content": "text"},
    {"role": "unknown", "content": "text"}, {"role": {}, "content": "text"},
    {"role": "user", "content": 1},
    {"role": "tool", "content": "result"},
    {"role": "tool", "content": "result", "tool_call_id": {"url": "http://x"}},
    {"role": "function", "content": "result"},
    {"role": "assistant", "content": None, "tool_calls": []},
    {"role": "assistant", "content": None, "tool_calls": {"url": "http://x"}},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "url": "http://x",
         "function": {"name": "edit", "arguments": "{}"}},
    ]},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {
            "name": "edit", "arguments": "{}", "route": {"url": "http://x"},
        }},
    ]},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {
            "name": "edit", "arguments": {"url": "http://x"},
        }},
    ]},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "image_url", "function": {"name": "x", "arguments": ""}},
    ]},
    {"role": "assistant", "content": None, "function_call": {
        "name": "edit", "arguments": "{}", "url": "http://x",
    }},
    {"role": "user", "content": "text", "function_call": {
        "name": "edit", "arguments": "{}",
    }},
    {"role": "assistant", "content": None, "function_call": {
        "name": {"url": "http://x"}, "arguments": "{}",
    }},
])
async def test_rejects_unknown_message_routing_and_invalid_tool_forms(setup, message):
    broker, journal, requests, _ = setup()
    raw = encode({"model": SETTINGS.model, "messages": [message]})
    with pytest.raises(IntegrityError):
        await forward(broker, raw)
    assert not requests and archived(journal, "original") == raw


@pytest.mark.parametrize("options", [
    {"tools": [{"type": "image_url", "function": {"name": "edit"}}]},
    {"tools": [{"type": "function", "function": {"name": "edit"}, "url": "http://x"}]},
    {"tools": [{"type": "function", "function": {
        "name": "edit", "url": {"nested": "http://x"},
    }}]},
    {"functions": [{"name": "edit", "route": {"url": "http://x"}}]},
    {"tools": {"url": "http://x"}},
    {"tool_choice": {"type": "function", "function": {
        "name": "edit", "url": "http://x",
    }}},
    {"function_call": {"name": "edit", "url": "http://x"}},
])
async def test_tool_definitions_and_choices_reject_unknown_routing(setup, options):
    broker, _, requests, _ = setup()
    with pytest.raises(IntegrityError):
        await forward(broker, encode({**json.loads(RAW), **options}))
    assert not requests


@pytest.mark.parametrize("role", [
    "system", "developer", "user", "assistant", "tool", "function",
])
@pytest.mark.parametrize("as_parts", [False, True])
async def test_plain_text_url_and_code_content_preserved(setup, role, as_parts):
    text = '```json\n{"image_url":{"url":"https://media.invalid/a"}}\n```\n'
    text += 'http://127.0.0.1/private file:///C:/private data:image/png;base64,YWJj'
    content = [{"type": "text", "text": text}] if as_parts else text
    message = {"role": role, "content": content, "name": "author"}
    if role == "tool":
        message["tool_call_id"] = "call_1"
    broker, journal, requests, _ = setup()
    raw = encode({"model": SETTINGS.model, "messages": [message]})
    await forward(broker, raw)
    assert json.loads(requests[0].content)["messages"] == [message]
    assert archived(journal, "original") == raw


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("content", [None, "", "Tool call with https://media.invalid"])
async def test_assistant_tool_calls_keep_opaque_url_arguments(setup, legacy, content):
    function = {"name": "edit", "arguments": json.dumps({
        "url": "https://media.invalid", "nested": {"image_url": {"url": "http://x"}},
        "max_tokens": 7, "max_completion_tokens": 8, "max_output_tokens": 9,
        "code": "fetch('https://example.invalid/path')",
    })}
    assistant = {"role": "assistant", "content": content}
    if legacy:
        assistant["function_call"] = function
        reply = {"role": "function", "name": "edit", "content": "https://tool.invalid"}
        options = {"functions": [{"name": "edit"}], "function_call": {"name": "edit"}}
    else:
        assistant["tool_calls"] = [{"id": "call_1", "type": "function",
                                    "function": function}]
        reply = {"role": "tool", "tool_call_id": "call_1", "content": "https://tool.invalid"}
        options = {"tools": [{"type": "function", "function": {
            "name": "edit", "description": "See https://schema.invalid",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "https://schema.invalid"},
            }}, "strict": True,
        }}], "tool_choice": {"type": "function", "function": {"name": "edit"}}}
    broker, journal, requests, _ = setup()
    payload = {"model": SETTINGS.model, "messages": [assistant, reply], **options}
    raw = encode(payload)
    await forward(broker, raw)
    assert json.loads(requests[0].content) == payload
    assert archived(journal, "original") == raw


async def test_assistant_tool_call_may_omit_content(setup):
    message = {"role": "assistant", "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "edit", "arguments": "{}"},
    }]}
    broker, _, requests, _ = setup()
    await forward(broker, encode({"model": SETTINGS.model, "messages": [message]}))
    assert json.loads(requests[0].content)["messages"] == [message]


@pytest.mark.parametrize("reasoning", [
    "", "Consider the next edit.\nThen verify it.",
    '```json\n{"url":"https://text.invalid","max_tokens":3}\n```',
    "Qwen 思考: http://127.0.0.1/private file:///private data:image/png;base64,YWJj",
])
@pytest.mark.parametrize("with_tool", [False, True])
async def test_assistant_reasoning_content_text_roundtrip(setup, reasoning, with_tool):
    message = {"role": "assistant", "content": "Answer", "reasoning_content": reasoning}
    if with_tool:
        message.update(content=None, tool_calls=[{
            "id": "call_1", "type": "function",
            "function": {"name": "edit", "arguments": '{"url":"https://text.invalid"}'},
        }])
    payload = {"model": SETTINGS.model, "messages": [message]}
    raw = json.dumps(payload, indent=2, ensure_ascii=False).encode()
    broker, journal, requests, _ = setup()
    await forward(broker, raw)
    assert len(requests) == 1
    assert json.loads(requests[0].content) == payload
    assert archived(journal, "original") == raw
    assert archived(journal, "request") == requests[0].content


@pytest.mark.parametrize("reasoning", [
    None, True, False, 0, 1.5, [], ["text"],
    [{"type": "text", "text": "not a plain string"}],
    {"url": "https://media.invalid"},
    [{"type": "image_url", "image_url": {"url": "https://media.invalid"}}],
    {"nested": {"reasoning_content": {"url": "http://private.invalid"}}},
])
async def test_assistant_reasoning_content_rejects_nonstring(setup, reasoning):
    message = {"role": "assistant", "content": "text", "reasoning_content": reasoning}
    raw = encode({"model": SETTINGS.model, "messages": [message]})
    broker, journal, requests, _ = setup()
    with pytest.raises(IntegrityError, match="reasoning_content must be a string"):
        await forward(broker, raw)
    assert not requests and archived(journal, "original") == raw
    assert not records(journal, "request")


@pytest.mark.parametrize("role", ["system", "developer", "user", "tool", "function"])
async def test_reasoning_content_rejected_on_other_roles(setup, role):
    message = {"role": role, "content": "text", "reasoning_content": "Plain text"}
    if role == "tool":
        message["tool_call_id"] = "call_1"
    if role == "function":
        message["name"] = "edit"
    broker, journal, requests, _ = setup()
    raw = encode({"model": SETTINGS.model, "messages": [message]})
    with pytest.raises(IntegrityError, match="Unknown or missing"):
        await forward(broker, raw)
    assert not requests and archived(journal, "original") == raw


async def test_reasoning_response_alias_does_not_authorize_request_extension(setup):
    message = {"role": "assistant", "content": "text", "reasoning": "Plain text"}
    broker, _, requests, _ = setup()
    with pytest.raises(IntegrityError, match="Unknown or missing"):
        await forward(broker, encode({"model": SETTINGS.model, "messages": [message]}))
    assert not requests


@pytest.mark.parametrize("sequence,sha", [
    (None, "a" * 64), (1, None), (-1, "a" * 64),
    (True, "a" * 64), (1.0, "a" * 64), (1, "A" * 64), (1, "a" * 63),
    (1, "g" * 64), (1, b"a" * 64),
])
def test_history_watermark_requires_both_valid_fields(sequence, sha):
    with pytest.raises(ValueError, match="History|history"):
        BrokerIdentity("run", "session", sequence, sha)


async def test_history_watermark_is_frozen_per_request_not_global_or_model_owned(setup):
    broker, journal, _, _ = setup()
    first = BrokerIdentity("run", "ses", 0, "a" * 64)
    second = BrokerIdentity("run", "ses", 8, "b" * 64)
    with pytest.raises(FrozenInstanceError):
        first.history_sequence = 9
    raw = encode({**json.loads(RAW), "metadata": {
        "history_sequence": 999, "history_event_sha256": "c" * 64,
    }})
    for identity in (first, second, IDENTITY):
        head = await forward(broker, raw, identity=identity)
        context = [r.value["data"] for r in journal.verify()
                   if r.value["data"]["request_id"] == head.request_id]
        assert context
        assert all(c["history_sequence"] == identity.history_sequence for c in context)
        assert all(c["history_event_sha256"] == identity.history_event_sha256
                   for c in context)


async def test_model_cannot_supply_history_watermark_at_top_level(setup):
    broker, _, requests, _ = setup()
    raw = encode({**json.loads(RAW), "history_sequence": 1,
                  "history_event_sha256": "a" * 64})
    with pytest.raises(IntegrityError, match="Unknown"):
        await forward(broker, raw)
    assert not requests


async def test_each_chunk_is_durable_before_incremental_delivery(setup):
    next_read = asyncio.Event()

    class Incremental(Stream):
        async def __aiter__(self):
            yield b"prefix"
            assert next_read.is_set(), "Deliver first chunk before reading the next"
            yield b"suffix"

    stream = Incremental()

    async def handler(_):
        return httpx.Response(200, stream=stream)

    broker, journal, _, _ = setup(handler)
    sink = Sink()

    async def chunk(value):
        assert archived(journal, "chunk") == b"".join(sink.chunks) + value
        sink.chunks.append(value)
        next_read.set()

    sink.chunk = chunk
    await forward(broker, sink=sink)
    end = data(journal, "end")[0]
    assert end["http_body_complete"] and end["failure"] is None
    assert end["delivery_callbacks_completed"] == 2
    assert end["upstream_quiescence"] == "unknown"
    assert stream.close_count == 1


@pytest.mark.parametrize("extra", [
    {"base_url": "http://outside"}, {"url": "http://outside"},
    {"headers": {}}, {"extra_body": {}}, {"timeout": 1}, {"retries": 2},
    {"n_predict": 2}, {"max_calls": 2}, {"endpoint": "/responses"},
    {"run_id": "forged"}, {"session_id": "forged"}, {"guard": True},
    {"provider": "other"}, {"cache_prompt": True}, {"input": "Responses API"},
])
async def test_unknown_top_level_controls_rejected_without_dispatch(setup, extra):
    broker, journal, requests, _ = setup()
    raw = encode({**json.loads(RAW), **extra})
    with pytest.raises(IntegrityError, match="Unknown"):
        await forward(broker, raw)
    assert not requests
    assert archived(journal, "original") == raw
    assert data(journal, "end")[0]["failure"] == "IntegrityError"


@pytest.mark.parametrize("raw", [
    b'{"model":"fixture-model","model":"fixture-model","messages":[{}]}',
    b'{"model":"fixture-model","messages":[{"content":1,"content":2}]}',
    b'{"model":"fixture-model","messages":[{}],"metadata":{"x":NaN}}',
    b'{"model":"fixture-model","messages":[{}],"max_tokens":Infinity}',
    b'{"model":"fixture-model","messages":[{}],"temperature":-Infinity}',
    b'{"model":"fixture-model","messages":[{}],"temperature":1e999}',
    b'{"model":"other","messages":[{}]}', b'{"messages":[{}]}',
    b'{"model":"fixture-model","messages":[]}',
    b'{"model":"fixture-model","messages":[1]}',
    b'{"model":"fixture-model","messages":[{}],"stream":1}',
    b'[]', b'null', b'{', b'{} {}', b'"\xff"',
    b'{"model":"fixture-model","messages":[{}],"metadata":"\\ud800"}',
])
async def test_invalid_json_model_and_structure_rejected(setup, raw):
    broker, journal, requests, _ = setup()
    with pytest.raises(IntegrityError):
        await forward(broker, raw)
    assert not requests
    assert archived(journal, "original") == raw


@pytest.mark.parametrize("path", [
    "/completions", "/responses", "/embeddings", "/chat/completions?x=1",
    "http://outside/v1/chat/completions", "/v1/chat/completions",
])
async def test_only_exact_chat_route(setup, path):
    broker, _, requests, _ = setup()
    with pytest.raises(IntegrityError, match="Only chat"):
        await forward(broker, path=path)
    assert not requests


@pytest.mark.parametrize("url", [
    "http://localhost:8001/v1", "http://example.org:8001/v1",
    "http://192.168.1.1:8001/v1", "http://0.0.0.0:8001/v1",
    "http://127.0.0.1/v1", "http://127.0.0.1:0/v1",
    "http://user:secret@127.0.0.1:8001/v1", "http://127.0.0.1:8001/v1/",
    "http://127.0.0.1:8001/other/../v1", "http://127.0.0.1:8001/%76%31",
    "http://127.0.0.1:8001/v1?", "http://127.0.0.1:8001/v1#",
    "http://127.0.0.1:8001/v1?x=1", "https://127.0.0.1:8001/v1",
    " http://127.0.0.1:8001/v1", "http://[::1%eth0]:8001/v1",
])
def test_endpoint_is_frozen_literal_loopback(url):
    with pytest.raises(ValueError):
        replace(SETTINGS, base_url=url)


def test_frozen_settings_and_resource_bounds():
    assert replace(SETTINGS, base_url="http://[::1]:8001/v1").model == SETTINGS.model
    with pytest.raises(FrozenInstanceError):
        SETTINGS.model = "other"
    for kwargs in (
        {"model": "a/b"}, {"max_request_bytes": 0}, {"max_frame_bytes": True},
        {"max_header_bytes": 2**30}, {"max_json_depth": 65},
    ):
        with pytest.raises(ValueError):
            replace(SETTINGS, **kwargs)
    for value in ("", "forged/identity", "x" * 257):
        with pytest.raises(ValueError):
            BrokerIdentity(value, "session")


async def test_request_byte_and_depth_bounds(setup):
    for raw in (b"", b"x" * 129, bytearray(RAW)):
        broker, _, requests, _ = setup(settings=replace(
            SETTINGS, max_request_bytes=128,
        ))
        with pytest.raises(IntegrityError, match="resource byte"):
            await forward(broker, raw)
        assert not requests
    raw = encode({**json.loads(RAW), "metadata": {"x": [[[[1]]]]}})
    broker, _, requests, _ = setup(settings=replace(SETTINGS, max_json_depth=4))
    with pytest.raises(IntegrityError, match="depth"):
        await forward(broker, raw)
    assert not requests


async def test_original_and_forwarded_exact_byte_bound(setup):
    canonical = encode(json.loads(RAW))
    broker, _, requests, _ = setup(
        settings=replace(SETTINGS, max_request_bytes=len(canonical)),
    )
    await forward(broker, canonical)
    assert len(requests[0].content) == len(canonical)
    # Compact original grows by encode's final newline; bound applies to both.
    raw = canonical.rstrip(b"\n")
    broker, journal, requests, _ = setup(
        settings=replace(SETTINGS, max_request_bytes=len(raw)),
    )
    with pytest.raises(IntegrityError, match="Forwarded"):
        await forward(broker, raw)
    assert not requests and archived(journal, "original") == raw


async def test_no_total_response_or_call_cap(setup):
    async def handler(_):
        return httpx.Response(200, stream=Stream([b"12345678"] * 20))

    broker, journal, requests, _ = setup(handler, settings=replace(
        SETTINGS, max_frame_bytes=8, max_request_bytes=128,
    ))
    for _ in range(12):
        await forward(broker)
    assert len(requests) == 12
    assert len(archived(journal, "chunk")) == 12 * 20 * 8
    assert len({r["request_id"] for r in data(journal, "request")}) == 12


async def test_no_environment_routing_redirects_or_retries(setup, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://external.invalid:9999")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://external.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")

    async def handler(_):
        return httpx.Response(
            307, headers={"location": "http://external.invalid/v1"},
            stream=Stream([b"redirect evidence"]),
        )

    broker, journal, requests, _ = setup(handler)
    sink = Sink()
    assert (await forward(broker, sink=sink)).status_code == 307
    assert len(requests) == 1 and sink.chunks == [b"redirect evidence"]
    assert not broker._client.trust_env and not broker._client.follow_redirects
    assert requests[0].url.host == "127.0.0.1"
    assert "authorization" not in requests[0].headers
    assert data(journal, "response")[0]["status"] == 307

    async def unavailable(_):
        raise httpx.ConnectError("unavailable")

    broker, journal, requests, _ = setup(unavailable)
    with pytest.raises(httpx.ConnectError):
        await forward(broker)
    assert len(requests) == 1
    assert data(journal, "end")[0]["status"] is None
    with pytest.raises(IntegrityError, match="failed"):
        await forward(broker)


async def test_idle_work_has_no_timer_or_overlap_queue(setup, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(_):
        entered.set()
        await release.wait()
        return httpx.Response(200, stream=Stream())

    def forbidden(*args, **kwargs):
        raise AssertionError("Broker must not schedule a work timeout")

    broker, journal, requests, _ = setup(handler)
    monkeypatch.setattr(asyncio, "wait_for", forbidden)
    monkeypatch.setattr(asyncio, "timeout", forbidden)
    work = asyncio.create_task(forward(broker))
    await entered.wait()
    for _ in range(10):
        await asyncio.sleep(0)
        assert not work.done() and broker.busy
    with pytest.raises(IntegrityError, match="busy"):
        await forward(broker)
    assert len(requests) == 1
    release.set()
    await work
    assert not broker.busy and len(data(journal, "request")) == 1


@pytest.mark.parametrize("phase", ["dispatch", "headers", "chunk", "return"])
async def test_controller_revocation_at_every_delivery_fence(setup, phase):
    revoked = False

    def fault(point):
        nonlocal revoked
        if point == "journal.after_readback:native_broker_" + {
            "dispatch": "request", "headers": "response", "chunk": "chunk",
            "return": "end",
        }[phase]:
            revoked = True

    def guard():
        if revoked:
            raise PermissionError("revoked by controller")

    broker, journal, requests, _ = setup(fault=fault)
    sink = Sink()
    with pytest.raises(PermissionError):
        await forward(broker, guard=guard, sink=sink)
    assert len(requests) == (0 if phase == "dispatch" else 1)
    if phase in {"dispatch", "headers", "chunk"}:
        assert not sink.chunks
    if phase == "chunk":
        assert archived(journal, "chunk") == b"data: first\n\n"
    if phase == "return":
        assert data(journal, "delivery_blocked")[0]["failure"] == "PermissionError"
    else:
        assert data(journal, "end")[0]["failure"] == "PermissionError"


async def test_false_sync_and_async_guards_fail_closed(setup):
    async def revoked_async_guard():
        return False

    for guard in (lambda: False, revoked_async_guard):
        broker, _, requests, _ = setup()
        with pytest.raises(IntegrityError, match="guard"):
            await forward(broker, guard=guard)
        assert not requests


async def test_async_guard_awaited_and_sync_guard_off_loop(setup):
    loop_thread = threading.get_ident()
    calls = []

    def sync_guard():
        calls.append(threading.get_ident())
        assert threading.get_ident() != loop_thread

    async def async_guard():
        await asyncio.sleep(0)
        calls.append(threading.get_ident())
        assert threading.get_ident() == loop_thread

    for guard in (sync_guard, async_guard):
        broker, _, _, _ = setup()
        await forward(broker, guard=guard)
    assert len(calls) == 12


async def test_async_callable_guard_and_sync_coroutine_factory(setup):
    class Guard:
        async def __call__(self):
            await asyncio.sleep(0)

    broker, _, requests, _ = setup()
    await forward(broker, guard=Guard())
    assert len(requests) == 1
    broker, _, requests, _ = setup()
    with pytest.raises(IntegrityError, match="async callable"):
        await forward(broker, guard=lambda: Guard()())
    assert not requests


async def test_cancel_sync_guard_settles_before_releasing_ownership(setup):
    entered, release = threading.Event(), threading.Event()

    def guard():
        entered.set()
        assert release.wait(10), "test synchronization failed"

    broker, journal, requests, _ = setup()
    work = asyncio.create_task(forward(broker, guard=guard))
    await asyncio.to_thread(entered.wait)
    try:
        work.cancel()
        await asyncio.sleep(0)
        work.cancel()
        await asyncio.sleep(0)
        assert broker.busy and not work.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert not requests and not broker.busy
    assert data(journal, "end")[0]["failure"] == "CancelledError"


async def test_default_host_transport_explicitly_disables_retries_and_env(
    tmp_path, monkeypatch,
):
    options = []

    def transport(**kwargs):
        options.append(kwargs)
        return httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))

    monkeypatch.setattr(native_broker.httpx, "AsyncHTTPTransport", transport)
    with Journal.create(tmp_path / "host-default") as journal:
        broker = NativeModelBroker(SETTINGS, journal)
        try:
            await forward(broker)
        finally:
            await broker.close()
    assert options == [{"retries": 0, "trust_env": False}]


@pytest.mark.parametrize("status", [200, 400, 429, 500])
async def test_disconnect_keeps_status_prefix_and_error(setup, status):
    stream = Stream([b"partial error or SSE"], httpx.ReadError("connection lost"))

    async def handler(_):
        return httpx.Response(status, stream=stream)

    broker, journal, requests, _ = setup(handler)
    sink = Sink()
    with pytest.raises(httpx.ReadError):
        await forward(broker, sink=sink)
    assert len(requests) == 1 and sink.heads[0].status_code == status
    assert archived(journal, "chunk") == b"partial error or SSE"
    end = data(journal, "end")[0]
    assert end["status"] == status and not end["http_body_complete"]
    assert end["failure"] == "ReadError" and end["upstream_quiescence"] == "unknown"
    assert stream.close_count == 1


async def test_downstream_disconnect_preserves_undelivered_chunk(setup):
    broker, journal, _, stream = setup()
    sink = Sink()

    async def disconnected(_):
        raise BrokenPipeError("downstream disconnected")

    sink.chunk = disconnected
    with pytest.raises(BrokenPipeError):
        await forward(broker, sink=sink)
    end = data(journal, "end")[0]
    assert end["delivery_callbacks_completed"] == 0
    assert end["failure"] == "BrokenPipeError"
    assert archived(journal, "chunk") == stream.chunks[0]


async def test_oversized_frame_preserves_bounded_prefix(setup):
    stream = Stream([b"ok", b"123456789"])

    async def handler(_):
        return httpx.Response(200, stream=stream)

    broker, journal, _, _ = setup(handler, settings=replace(
        SETTINGS, max_frame_bytes=8,
    ))
    sink = Sink()
    with pytest.raises(IntegrityError, match="frame"):
        await forward(broker, sink=sink)
    assert sink.chunks == [b"ok"]
    assert archived(journal, "chunk") == b"ok12345678"
    assert data(journal, "chunk")[-1]["truncated"]
    assert data(journal, "chunk")[-1]["received_bytes"] == 9
    assert not data(journal, "end")[0]["http_body_complete"]


@pytest.mark.parametrize("headers,bound,match", [
    ({"content-encoding": "gzip"}, 1024, "Encoded"),
    ({"x-long": "x" * 100}, 20, "headers"),
])
async def test_response_resource_and_encoding_rejections(setup, headers, bound, match):
    async def handler(_):
        return httpx.Response(200, headers=headers, stream=Stream())

    broker, journal, _, _ = setup(handler, settings=replace(
        SETTINGS, max_header_bytes=bound,
    ))
    sink = Sink()
    with pytest.raises(IntegrityError, match=match):
        await forward(broker, sink=sink)
    assert not sink.heads and not sink.chunks
    assert data(journal, "end")[0]["status"] == 200
    assert len(archived(journal, "response")) <= bound


async def test_repeated_cancellation_retains_close_ownership_and_partial_capture(setup):
    waiting = asyncio.Event()

    class Paused(Stream):
        async def __aiter__(self):
            yield b"prefix"
            waiting.set()
            await asyncio.Event().wait()

    stream = Paused()
    stream.close_release = asyncio.Event()

    async def handler(_):
        return httpx.Response(200, stream=stream)

    broker, journal, _, _ = setup(handler)
    work = asyncio.create_task(forward(broker))
    await waiting.wait()
    work.cancel()
    await stream.close_entered.wait()
    work.cancel()
    await asyncio.sleep(0)
    work.cancel()
    await asyncio.sleep(0)
    assert not work.done() and broker.busy
    with pytest.raises(IntegrityError, match="busy"):
        await forward(broker)
    stream.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert stream.close_count == 1 and not broker.busy
    assert archived(journal, "chunk") == b"prefix"
    assert data(journal, "end")[0]["failure"] == "CancelledError"
    assert len(data(journal, "cancelled_during_settlement")) == 1


async def test_cancellation_during_durable_chunk_write(setup):
    entered, release = threading.Event(), threading.Event()

    def fault(point):
        if point == "journal.before_commit:native_broker_chunk":
            entered.set()
            assert release.wait(10), "test synchronization failed"

    broker, journal, _, _ = setup(fault=fault)
    sink = Sink()
    work = asyncio.create_task(forward(broker, sink=sink))
    await asyncio.to_thread(entered.wait)
    try:
        work.cancel()
        await asyncio.sleep(0)
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done() and broker.busy
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert not sink.chunks
    assert archived(journal, "chunk") == b"data: first\n\n"
    assert data(journal, "end")[0]["chunks"] == 1


async def test_broker_close_settles_active_work_and_repeated_close_cancellation(setup):
    entered = asyncio.Event()

    async def handler(_):
        entered.set()
        await asyncio.Event().wait()

    broker, journal, _, _ = setup(handler)
    close_entered, close_release = asyncio.Event(), asyncio.Event()
    actual_close = broker._client.aclose
    calls = []

    async def close_client():
        calls.append(1)
        close_entered.set()
        await close_release.wait()
        await actual_close()

    broker._client.aclose = close_client
    work = asyncio.create_task(forward(broker))
    await entered.wait()
    closing = asyncio.create_task(broker.close())
    await close_entered.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(asyncio.CancelledError):
        await work
    await broker.close()
    assert calls == [1]
    end = data(journal, "end")[0]
    assert end["broker_close_requested"] and end["status"] is None
    assert end["failure"] == "CancelledError"
    assert end["upstream_quiescence"] == "unknown"


async def test_cancel_at_eof_close_is_recorded_and_settled(setup):
    broker, journal, _, stream = setup()
    stream.close_release = asyncio.Event()
    work = asyncio.create_task(forward(broker))
    await stream.close_entered.wait()
    work.cancel()
    await asyncio.sleep(0)
    assert broker.busy and not work.done()
    stream.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert stream.close_count == 1
    assert data(journal, "end")[0]["http_body_complete"]
    assert data(journal, "cancelled_during_settlement")


async def test_close_failure_preserves_primary_error_evidence(setup):
    class BrokenClose(Stream):
        async def aclose(self):
            raise OSError("close failed")

    async def handler(_):
        return httpx.Response(
            503, stream=BrokenClose([b"prefix"], httpx.ReadError("lost")),
        )

    broker, journal, _, _ = setup(handler)
    with pytest.raises(OSError, match="close failed"):
        await forward(broker)
    end = data(journal, "end")[0]
    assert end["failure"] == "ReadError" and end["close_failure"] == "OSError"
    assert archived(journal, "chunk") == b"prefix"


async def test_journal_failure_prevents_dispatch_or_delivery(setup):
    for kind in ("request", "chunk"):
        def fault(point, kind=kind):
            if point == "journal.before_commit:native_broker_" + kind:
                raise OSError("journal failure")

        broker, journal, requests, stream = setup(fault=fault)
        sink = Sink()
        with pytest.raises(IntegrityError, match="poisoned"):
            await forward(broker, sink=sink)
        assert not sink.chunks and journal.poisoned
        assert len(requests) == (1 if kind == "chunk" else 0)
        assert stream.close_count == (1 if kind == "chunk" else 0)
        with pytest.raises(IntegrityError, match="failed"):
            await forward(broker)


async def test_self_close_from_callback_is_rejected_without_deadlock(setup):
    broker, journal, _, _ = setup()
    sink = Sink()

    async def close_inside(_):
        await broker.close()

    sink.chunk = close_inside
    with pytest.raises(IntegrityError, match="outside"):
        await forward(broker, sink=sink)
    assert not broker.busy
    assert data(journal, "end")[0]["failure"] == "IntegrityError"
