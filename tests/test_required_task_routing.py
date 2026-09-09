"""Continuous foreground routing is explicit; other model calls stay unchanged."""

import json

import httpx
import pytest

from recollect.engine.generator import (
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
        _ = [chunk async for chunk in generator.stream(
            [{"role": "user", "content": "hi"}], trace=trace, tools=tools,
        )]
    finally:
        await generator.aclose()
    if tools:
        assert calls[0]["tool_choice"] == ("required" if required else "auto")
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
        return httpx.Response(200, content=b"data: [DONE]\n\n")

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
    assert bodies[0]["tool_choice"] == "required"
