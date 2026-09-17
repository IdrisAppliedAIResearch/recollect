"""The MCP research server over stdio: protocol shape plus tool behaviour.

The server is spawned as a real subprocess speaking newline-delimited
JSON-RPC (the MCP stdio transport), which is exactly how opencode will
drive it. web_search is exercised for argument validation only - its six
legs are real external hosts. web_fetch is exercised for its refusal
paths, the same guard behaviour test_webtools.py pins directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys

import pytest
from mcp.shared.version import LATEST_PROTOCOL_VERSION


class Rpc:
    """Minimal JSON-RPC client for the MCP stdio transport."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self._next_id = 0

    async def request(
        self, method: str, params: dict | None = None, timeout: float = 60.0
    ) -> dict:
        self._next_id += 1
        message: dict = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self.process.stdin.drain()
        while True:
            raw = await asyncio.wait_for(self.process.stdout.readline(), timeout)
            if not raw:
                raise AssertionError(f"{method}: server closed stdout")
            raw = raw.strip()
            if not raw:
                continue
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                continue  # log noise on stdout - not protocol
            if decoded.get("id") != self._next_id:
                continue
            if "error" in decoded:
                raise AssertionError(f"{method}: {decoded['error']}")
            return decoded["result"]

    async def notify(self, method: str, params: dict | None = None) -> None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self.process.stdin.drain()


@pytest.fixture
async def mcp() -> Rpc:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "recollect.engine.mcp_research",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    rpc = Rpc(process)
    init = await rpc.request(
        "initialize",
        {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "recollect-test", "version": "0"},
        },
    )
    assert init["serverInfo"]["name"] == "recollect_research"
    await rpc.notify("notifications/initialized")
    try:
        yield rpc
    finally:
        with contextlib.suppress(Exception):
            process.stdin.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5.0)
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_tools_are_the_research_tools(mcp: Rpc) -> None:
    result = await mcp.request("tools/list")
    tools = {tool["name"]: tool for tool in result["tools"]}
    # Self-modification adds tools to this server, so the research pair must be
    # present and well-formed rather than the whole list being frozen.
    assert {"web_search", "web_fetch"} <= set(tools)
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools.values())
    assert tools["web_search"]["inputSchema"]["required"] == ["query"]
    assert tools["web_fetch"]["inputSchema"]["required"] == ["url"]


async def call(mcp: Rpc, name: str, arguments: dict) -> dict:
    result = await mcp.request("tools/call", {"name": name, "arguments": arguments})
    content = result["content"]
    assert content and content[0]["type"] == "text"
    document = json.loads(content[0]["text"])
    assert document["tool"] == name
    return document


async def test_tools_refuse_bad_arguments_and_private_hosts(mcp: Rpc) -> None:
    # One server for every refusal: each spawn costs most of this file's time.
    assert (await call(mcp, "web_search", {"query": "   "}))["error"] == (
        "missing 'query'")
    # Present but blank: an absent param is rejected by the JSON schema
    # before the tool runs, the tool itself guards the blank case.
    assert (await call(mcp, "web_fetch", {"url": "   "}))["error"] == "missing 'url'"
    assert (await call(mcp, "web_fetch", {"url": "ftp://example.com/x"}))[
        "error"] == "only http/https URLs"
    refused = (await call(mcp, "web_fetch",
                          {"url": "http://127.0.0.1:8080/api/health"}))["error"]
    assert refused.startswith("refused")
    assert "only public http/https pages may be fetched" in refused
