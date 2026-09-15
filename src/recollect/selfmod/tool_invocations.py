"""Host-observed native tool invocations for provider-operation attribution.

The task coordinator's runner reports every native part event it authenticates
for a task's session tree. Only tools served by the subagent's MCP tool server
can reach the provider relay; built-in native tools have no route to it. A relay
request is attributed to the task's single in-flight MCP tool call. When none or
several are in flight the attribution is ``None``, which later fails the frozen
attribution check rather than guessing.
"""

import threading

MCP_PREFIX = "recollect_research_"
IN_FLIGHT = frozenset({"pending", "running"})


class ToolInvocations:
    def __init__(self):
        self._lock = threading.Lock()
        self._status = {}
        self._tools = {}
        self._inputs = {}

    def observe(self, task_id, event, session_id, children):
        if not isinstance(event, dict) or event.get("type") != "message.part.updated":
            return
        part = (event.get("properties") or {}).get("part") or {}
        session, call = part.get("sessionID"), part.get("callID")
        tool = part.get("tool")
        status = (part.get("state") or {}).get("status")
        if (part.get("type") != "tool" or session not in {session_id, *children}
                or not isinstance(call, str) or not call
                or not isinstance(tool, str) or not tool.startswith(MCP_PREFIX)
                or not isinstance(status, str)):
            return
        identity = f"{task_id}:{session}:{call}"
        arguments = (part.get("state") or {}).get("input")
        with self._lock:
            self._tools[identity] = tool[len(MCP_PREFIX):]
            self._status[identity] = (task_id, status)
            # Authenticated native history is the source for an exact replay.
            if isinstance(arguments, dict):
                self._inputs[identity] = arguments

    def running(self, task_id):
        with self._lock:
            live = [identity for identity, (owner, status) in self._status.items()
                    if owner == task_id and status in IN_FLIGHT]
        return live[0] if len(live) == 1 else None

    def tool(self, invocation_id):
        with self._lock:
            return self._tools.get(invocation_id)

    def arguments(self, invocation_id):
        with self._lock:
            value = self._inputs.get(invocation_id)
            return dict(value) if value is not None else None
