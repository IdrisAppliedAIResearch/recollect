"""Harness-owned host for the subagent's MCP tool server.

The pinned OpenCode binary gives every MCP tool call a per-request timeout that
no setting disables, but it passes ``resetTimeoutOnProgress`` and no total cap.
This host therefore runs each tool call on a separate execution thread and event
loop, while the protocol loop sends an MCP progress notification at a fixed
cadence for as long as that call is actually running. A slow or quiet tool never
reaches the timeout; only a host that stops delivering protocol messages can.
The cadence is transport liveness, never a work deadline (draft amendment 03).

The served tools are whatever the subagent's ``recollect.engine.mcp_research``
module registers, including modifier-generated tools. This file sits outside the
modifier's writable scope. Tools that use the MCP request context from the
execution thread must not await session I/O; results return through the host.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading

from mcp.server.fastmcp import FastMCP

#: Well below the configured 120 s per-request timeout, so one delayed message
#: cannot expire a live call. Frozen with the tool host in the runtime manifest.
KEEPALIVE_SECONDS = 10.0


class ToolExecution:
    """One dedicated event loop thread that runs tool calls off the protocol loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve, name="recollect-tool-execution", daemon=True,
        )
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def close(self) -> None:
        """Stop the execution loop; the served process otherwise exits with it."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join()
        self.loop.close()

    def submit(self, coroutine, context: contextvars.Context):
        """Run ``coroutine`` on the execution loop under the caller's context."""

        async def run():
            # The inner task carries the protocol request context, so FastMCP's
            # Context sees the request; cancelling the outer task cancels it.
            return await asyncio.get_running_loop().create_task(
                coroutine, context=context,
            )

        return asyncio.run_coroutine_threadsafe(run(), self.loop)


def install_keepalive(server: FastMCP, execution: ToolExecution | None = None,
                      *, interval: float | None = None) -> ToolExecution:
    """Replace the server's tool handler with an off-loop, kept-alive one."""
    execution = execution or ToolExecution()
    cadence = KEEPALIVE_SECONDS if interval is None else interval
    if not cadence > 0:
        raise ValueError("Keepalive cadence must be positive")

    async def call(name: str, arguments: dict):
        request = server.get_context().request_context
        token = getattr(request.meta, "progressToken", None) if request else None
        future = execution.submit(server.call_tool(name, arguments),
                                  contextvars.copy_context())
        pending = asyncio.wrap_future(future)
        beats = 0
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=cadence)
                if done:
                    return pending.result()
                if token is not None:
                    beats += 1
                    await request.session.send_progress_notification(
                        token, float(beats), None,
                        related_request_id=request.request_id,
                    )
        except BaseException:
            future.cancel()
            raise

    server._mcp_server.call_tool(validate_input=False)(call)
    return execution


def main() -> None:
    from . import mcp_research

    install_keepalive(mcp_research.mcp)
    mcp_research.mcp.run()


if __name__ == "__main__":
    main()
