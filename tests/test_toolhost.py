"""Tool host: calls run off the protocol loop with progress liveness while running."""

import asyncio
import contextvars
import threading
import time

import pytest
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from recollect.engine import toolhost


@pytest.fixture
def hosted():
    app = FastMCP("toolhost-test")

    @app.tool()
    async def slow(seconds: float) -> str:
        await asyncio.sleep(seconds)
        return "slow done"

    @app.tool()
    def blocking(seconds: float) -> str:
        # Blocks its own thread; the protocol loop must still send liveness.
        time.sleep(seconds)
        return "blocking done"

    @app.tool()
    async def request_identity(ctx: Context) -> str:
        return str(ctx.request_id)

    execution = toolhost.install_keepalive(app, interval=0.02)
    yield app
    execution.close()


async def call(app, name, arguments, *, progress=True):
    beats = []

    async def record(value, total, message):
        beats.append(value)

    async with create_connected_server_and_client_session(app) as client:
        result = await client.call_tool(name, arguments,
                                        progress_callback=record if progress else None)
    return result, beats


@pytest.mark.parametrize(("name", "text"), [("slow", "slow done"),
                                            ("blocking", "blocking done")])
async def test_running_call_sends_increasing_progress_until_its_result(
    hosted, name, text,
):
    result, beats = await call(hosted, name, {"seconds": 0.25})
    assert not result.isError and result.content[0].text == text
    assert len(beats) >= 3
    assert beats == sorted(set(beats))


async def test_call_without_progress_token_still_completes(hosted):
    result, beats = await call(hosted, "slow", {"seconds": 0.08}, progress=False)
    assert result.content[0].text == "slow done" and beats == []


async def test_execution_thread_sees_the_protocol_request_context(hosted):
    result, _ = await call(hosted, "request_identity", {})
    assert not result.isError and result.content[0].text not in {"", "None"}


def test_cadence_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        toolhost.install_keepalive(FastMCP("x"), toolhost.ToolExecution(), interval=0)


def test_cancelling_the_protocol_side_cancels_the_running_tool():
    execution = toolhost.ToolExecution()
    started, cancelled = threading.Event(), threading.Event()

    async def tool():
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    try:
        future = execution.submit(tool(), contextvars.copy_context())
        assert started.wait(5)
        future.cancel()
        assert cancelled.wait(5)
    finally:
        execution.close()


def test_frozen_cadence_is_well_inside_the_configured_request_timeout():
    from recollect.engine.sandbox import configgen

    config = configgen.build_config(".", base_url="http://m/v1", model="m",
                                    api_key="k", steps=1)
    timeout_s = config["mcp"][configgen.MCP_SERVER]["timeout"] / 1000
    assert 0 < toolhost.KEEPALIVE_SECONDS <= timeout_s / 4
