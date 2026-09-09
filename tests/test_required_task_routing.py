"""Continuous foreground routing is explicit; other model calls stay unchanged."""

import json

import httpx
import pytest

from recollect.engine.generator import (
    GenerationError,
    Generator,
    GeneratorSettings,
    new_generation_trace,
)

TASK_REPLY = {
    "type": "function",
    "function": {
        "name": "task_reply",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "status_only": {"type": "boolean"},
            },
            "required": ["text", "status_only"],
        },
    },
}


@pytest.mark.parametrize("required,tools", [
    (False, [TASK_REPLY]), (True, [TASK_REPLY]), (True, None),
])
async def test_required_routing_only_applies_to_advertised_tools(required, tools):
    calls = []
    arguments = json.dumps({"text": "The task is still running.", "status_only": True})
    frames = [
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "routing-call", "type": "function",
            "function": {"name": "task_reply", "arguments": arguments[:20]},
        }]}}]},
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": arguments[20:]},
        }]}, "finish_reason": "tool_calls"}]},
    ] if tools else [{"choices": [{"delta": {"content": "A natural update."}}]}]
    if required and tools:
        selection = json.dumps({
            "name": "task_reply", "arguments": json.loads(arguments),
        })
        frames = [
            {"choices": [{"delta": {"content": selection[:17]}}]},
            {"choices": [{"delta": {"content": selection[17:]},
                          "finish_reason": "stop"}]},
        ]

    def server(request):
        calls.append(json.loads(request.content))
        data = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        return httpx.Response(200, content=(data + "data: [DONE]\n\n").encode())

    settings = GeneratorSettings(
        base_url="http://model/v1", model="test", require_tools=required,
    )
    generator = Generator(settings)
    await generator._client.aclose()
    generator._client = httpx.AsyncClient(
        base_url=settings.base_url, transport=httpx.MockTransport(server),
    )
    trace = new_generation_trace(
        settings=settings, system_prompt="system", context_block="", user_message="hi",
    )
    try:
        chunks = [chunk async for chunk in generator.stream(
            [{"role": "user", "content": "hi"}], trace=trace, tools=tools,
        )]
    finally:
        await generator.aclose()
    if tools:
        if required:
            assert "tool_choice" not in calls[0] and "tools" not in calls[0]
            assert calls[0]["response_format"]["type"] == "json_schema"
            assert chunks == []
            assert trace.response_text == ""
        else:
            assert calls[0]["tool_choice"] == "auto"
        assert trace.tool_calls[0].name == "task_reply"
        assert json.loads(trace.tool_calls[0].arguments)["status_only"] is True
    else:
        assert "tool_choice" not in calls[0] and "tools" not in calls[0]
        assert trace.response_text == "A natural update."


async def test_context_guard_sees_the_same_required_tool_request_as_inference():
    bodies = []

    def server(request):
        body = json.loads(request.content)
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3]})
        bodies.append(body)
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "required tool template"})
        selection = json.dumps({"name": "task_reply", "arguments": {
            "text": "Still working.", "status_only": True,
        }})
        frame = {"choices": [{
            "delta": {"content": selection}, "finish_reason": "stop",
        }]}
        return httpx.Response(200, content=f"data: {json.dumps(frame)}\n\n".encode())

    settings = GeneratorSettings(
        base_url="http://model/v1", model="test", require_tools=True,
        context_tokens=4096,
    )
    generator = Generator(settings)
    await generator._client.aclose()
    generator._client = httpx.AsyncClient(
        base_url=settings.base_url, transport=httpx.MockTransport(server),
    )
    trace = new_generation_trace(
        settings=settings, system_prompt="system", context_block="", user_message="hi",
    )
    try:
        _ = [chunk async for chunk in generator.stream(
            [{"role": "user", "content": "hi"}], trace=trace, tools=[TASK_REPLY],
        )]
    finally:
        await generator.aclose()
    assert bodies[0] == bodies[1]
    assert bodies[0]["response_format"]["type"] == "json_schema"
    assert trace.total_prompt_chars > len("systemhi")


@pytest.mark.parametrize("content,finish", [
    ('Hello! Hello! Hello!', "length"),
    ('{"name":"task_reply","arguments":', "length"),
    ('{"name":"unknown","arguments":{}}', "stop"),
    ('{"name":[],"arguments":{}}', "stop"),
    ('{"name":"task_reply","arguments":[]}', "stop"),
    ('{"name":"task_reply","arguments":{},"extra":true}', "stop"),
    ('[{"name":"task_reply","arguments":{}}]', "stop"),
    ('{"name":"task_reply","arguments":{}}', "length"),
])
async def test_invalid_structured_reply_never_emits_prose_or_an_operation(
    content, finish,
):
    def server(request):
        frame = {"choices": [{"delta": {"content": content}, "finish_reason": finish}]}
        return httpx.Response(200, content=f"data: {json.dumps(frame)}\n\n".encode())

    settings = GeneratorSettings(
        base_url="http://model/v1", model="test", require_tools=True,
    )
    generator = Generator(settings)
    await generator._client.aclose()
    generator._client = httpx.AsyncClient(
        base_url=settings.base_url, transport=httpx.MockTransport(server),
    )
    trace = new_generation_trace(
        settings=settings, system_prompt="system", context_block="", user_message="hi",
    )
    chunks = []
    messages = [{"role": "system", "content": "system"},
                {"role": "user", "content": "hi"}]
    try:
        with pytest.raises(GenerationError):
            async for chunk in generator.stream(
                messages, trace=trace, tools=[TASK_REPLY],
            ):
                chunks.append(chunk)
    finally:
        await generator.aclose()
    assert chunks == []
    assert trace.error and trace.tool_calls == [] and trace.response_text == ""
    assert messages[0]["content"] == "system"
    assert not generator._model_slot.locked()
