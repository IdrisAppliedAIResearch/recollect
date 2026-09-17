"""Bounded model brokering and frozen, networkless role-process inputs.

These are one-shot structured actors, not an OpenCode shell or an interactive
tool loop. The controller owns their context, identities, limits and receipts.
"""

import asyncio
import base64
import difflib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import role_worker
from .clock import current_stamp
from .containment import FixtureSpec
from .contracts import (
    ChangePolicy,
    File,
    Plan,
    PlannedChange,
    Snapshot,
    Verification,
    require_tuple,
)
from .journal import IntegrityError, encode, sha256

PROMPTS = {
    "plan": """<role>
You plan how to implement a new capability. Its tests are already written and frozen; the plan must make them pass.
</role>

<steps>
1. Read the request, the requirements, the checks and the codebase.
2. List each file to change: path, operation (create or modify), the requirement ids it serves, and why.
3. For every requirement, say how it will be verified: the check names that cover it, or code review.
4. If there is history or prior_attempts, don't repeat what failed.
</steps>

<rules>
- Only editable paths, or new files under the allowed folders. No deletions.
- Implement the interface exactly as the interface requirement states, and register the tool in recollect/engine/mcp_research.py.
- Change as little as possible.
</rules>

<output>
Only a JSON object, no Markdown fences:
{"changes": [{"path": "...", "operation": "create", "requirement_ids": ["..."], "reason": "..."}],
 "verification": [{"requirement_id": "...", "method": "..."}]}
Every requirement appears in verification exactly once.
</output>""",
    "forward_review": """<role>
You review an implementation plan before any code is written. You did not write it.
</role>

<steps>
1. Read the request, the requirements, the checks, the codebase and the plan.
2. Would these changes make every check pass and meet every requirement?
3. Look for missing changes, unnecessary changes, and anything that could break existing tools.
</steps>

<rules>
- The code doesn't exist yet. Judge the plan, not results.
- Blocking: the plan can't meet a requirement or would break something. Everything else is advisory.
- Approve when there are no blocking findings.
- Don't write code or change the requirements.
</rules>

<output>
Only a JSON object, no Markdown fences:
{"approved": true, "findings": [{"severity": "blocking", "target": "<path or requirement id>", "issue": "...", "fix": "..."}], "rationale": "..."}
</output>""",
    "execute": """<role>
You implement a reviewed plan. Frozen checks will run on your result.
</role>

<steps>
1. Read the plan, the checks, the files you will change, and the history.
2. Write each planned change. For a new file, give its full text. For an existing file, give replacements; each old text must appear exactly once in that file.
3. If the history has failed checks or findings, fix their cause.
</steps>

<rules>
- Only the planned paths. No other files, no shell.
- Edits always apply to the original files shown. On a revision, resend every edit, starting from previous_edits.
- Import only the standard library, the tree, and httpx, mcp and trafilatura.
</rules>

<output>
Only a JSON object, no Markdown fences:
{"edits": [{"path": "...", "text": "..."},
           {"path": "...", "replace": [{"old": "...", "new": "..."}]}]}
</output>""",
    "code_review": """<role>
You review a finished implementation. You did not write it. All checks passed.
</role>

<steps>
1. Read the request, the requirements, the plan, the checks and the diff.
2. Does the diff meet every requirement? Confirm the interface, and check anything listed as unverified by reading the code.
3. Look for missing planned work, unplanned changes, weak error handling, and breakage in existing tools.
</steps>

<rules>
- Blocking: a requirement isn't met or something breaks. Everything else is advisory.
- Approve when there are no blocking findings.
- Don't write code or change the requirements.
</rules>

<output>
Only a JSON object, no Markdown fences:
{"approved": true, "findings": [{"severity": "blocking", "target": "<path or requirement id>", "issue": "...", "fix": "..."}], "rationale": "..."}
</output>""",
}

REGISTRY = "recollect/engine/mcp_research.py"
CHECK_LOG_CHARS = 4000


class InvalidRoleReply(ValueError):
    """A model reply the step can retry with feedback, not an integrity failure."""


@dataclass(frozen=True)
class RoleSettings:
    base_url: str
    model: str
    checks: tuple[File, ...]
    driver: bytes = field(default_factory=lambda: driver_bytes(), init=False,
                          repr=False)
    prompts: tuple[tuple[str, str], ...] = field(
        default_factory=lambda: tuple(PROMPTS.items()), init=False,
    )
    #: Three-lane profiles pin role inference to the modifier's server slot so
    #: planning and review can never occupy the conversation or worker lane.
    slot: int | None = None

    def __post_init__(self):
        if self.slot is not None and (type(self.slot) is not int
                                      or not 0 <= self.slot < 64):
            raise ValueError("Freeze a valid pinned model slot")
        url = urlsplit(self.base_url)
        if (
            url.scheme != "http" or url.hostname != "127.0.0.1"
            or url.port is None or url.path != "/v1"
            or url.username or url.password or url.query or url.fragment
        ):
            raise ValueError("Freeze an explicit loopback chat-model endpoint")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("Freeze the served model identity")
        require_tuple(self.checks)
        if not self.checks or len(self.checks) > 16:
            raise ValueError("Freeze 1-16 independent development checks")
        Snapshot(self.checks)
        for check in self.checks:
            if not re.fullmatch(r"[A-Za-z0-9_-]+\.py", check.path):
                raise ValueError("Check scripts need simple, unique Python names")

    @property
    def identity(self):
        return sha256(encode({
            "base_url": self.base_url, "model": self.model,
            "timing_policy": "observational",
            "checks_sha256": Snapshot(self.checks).sha256,
            "prompts": self.prompts, "driver_sha256": sha256(self.driver),
            # Unpinned profiles keep their historical identity.
            **({"slot": self.slot} if self.slot is not None else {}),
        }))


def driver_bytes():
    return Path(role_worker.__file__).read_bytes()


def model_payload(context, settings, message):
    prompt = context["stage"] if context["role"] == "review" else context["role"]
    payload = {
        "model": settings.model,
        "messages": [{"role": "system", "content": dict(settings.prompts)[prompt]},
                     {"role": "user", "content": message}],
        "temperature": 0, "stream": False,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if settings.slot is not None:
        payload["id_slot"] = settings.slot
    return payload


class LocalRoleModel:
    """No redirects, environment proxy, retries, tools or model-chosen URL.

    Cancelling HTTP abandons the response; it does not attest GPU quiescence.
    An interrupted request is terminal, never silently retried.
    """

    def __init__(self, settings: RoleSettings, *, transport=None, admission=None,
                 lane=None):
        self.settings, self.transport = settings, transport
        # The app's model admission queue: on one slot, conversation goes first.
        self.admission, self.lane = admission, lane
        self._used = False
        self._request = self._response = b""
        self.response_complete = False
        self.local_closed = False

    def evidence(self):
        return Snapshot((File("model-request.json", self._request),
                         File("model-response.bin", self._response)))

    async def complete(self, payload, deadline, *, clock=current_stamp):
        if self._used:
            raise IntegrityError("Model request is single-use")
        self._used = True
        now = clock()
        remaining = ((deadline.monotonic_ns - now.monotonic_ns) / 1e9
                     if deadline.monotonic_ns is not None else None)
        if now.boot_id != deadline.boot_id or (
            remaining is not None and remaining <= 0
        ):
            raise IntegrityError("Role inference deadline exhausted")
        body = encode(payload)
        if len(body) > 128 * 1024:
            raise IntegrityError("Role model context exceeds byte budget")
        self._request = body
        if self.admission is not None:
            await self.admission.acquire(lane=self.lane)
        try:
            async with asyncio.timeout(remaining):
                async with httpx.AsyncClient(
                    trust_env=False, follow_redirects=False, timeout=remaining,
                    transport=self.transport,
                ) as client:
                    async with client.stream(
                        "POST", self.settings.base_url + "/chat/completions",
                        content=body, headers={"Content-Type": "application/json",
                                               "Accept-Encoding": "identity"},
                    ) as response:
                        response.raise_for_status()
                        if response.headers.get("content-encoding", "identity") != (
                            "identity"
                        ):
                            raise IntegrityError("Encoded role response is forbidden")
                        raw = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=8192):
                            room = 128 * 1024 - len(raw)
                            raw.extend(chunk[:room])
                            self._response = bytes(raw)
                            if len(chunk) > room:
                                raise IntegrityError("Role model response exceeds bound")
        finally:
            if self.admission is not None:
                self.admission.release(lane=self.lane)
        self.response_complete = True
        self.local_closed = True
        now = clock()
        if now.boot_id != deadline.boot_id or (
            deadline.monotonic_ns is not None
            and now.monotonic_ns >= deadline.monotonic_ns
        ):
            raise IntegrityError("Late role model response")
        value = role_worker.parse(bytes(raw))
        choices = value.get("choices")
        usage = value.get("usage", {})
        if (type(choices) is list and len(choices) == 1
                and choices[0].get("finish_reason") == "length"):
            raise InvalidRoleReply("the reply was cut off at the length limit")
        if (
            type(choices) is not list or len(choices) != 1
            or choices[0].get("finish_reason") != "stop"
            or choices[0].get("message", {}).get("tool_calls")
            or type(usage) is not dict
            or type(usage.get("completion_tokens")) is not int
            or usage["completion_tokens"] < 0
        ):
            raise IntegrityError("Incomplete or unaccounted role inference")
        content = choices[0]["message"].get("content")
        if type(content) is not str:
            raise IntegrityError("Role inference needs explicit JSON content")
        try:
            reply = encode(role_worker.parse(content.encode()))
        except ValueError as error:
            raise InvalidRoleReply(f"the reply was not a valid JSON object: {error}") from error
        return reply, bytes(raw)


def source_context(source):
    return [{"path": file.path, "text": file.content.decode("utf-8")}
            for file in sorted(source.files, key=lambda f: f.path)]


def make_context(dev, grant, settings):
    """The worker envelope. Models see render_message, not this bookkeeping."""
    history = [r.value["data"] for r in dev._controller.journal.verify()
               if r.value["kind"] == "development_input"
               and r.value["data"].get("cycle_id") == dev._id
               and r.value["data"].get("kind") in {"plan", "review", "checks",
                                                   "invalid"}]
    artifact = dev._development._artifact
    return {
        "version": 1, "request_id": uuid.uuid4().hex,
        "role": grant.action, "stage": str(grant.stage),
        "actor_id": grant.actor_id, "grant_id": grant.grant_id,
        "controller_instance": grant.controller_instance,
        "cycle_id": grant.cycle_id, "generation": grant.generation,
        "binding": asdict(grant.binding) if grant.binding else None,
        "contract": asdict(dev._controller.config.contract),
        "prior_attempts": list(dev._controller.config.feedback),
        "policy": asdict(dev._policy),
        "baseline": sorted(f.path for f in dev._baseline.files),
        "candidate": (sorted(f.path for f in artifact.files)
                      if artifact is not None else []),
        "candidate_sha256": artifact.sha256 if artifact is not None else None,
        "plan": asdict(dev._development._plan) if dev._development._plan else None,
        "history": history,
        "checks": [f.path[:-3] for f in settings.checks],
        "runner_sha256": settings.identity,
    }


def _tag(name, body):
    return f"<{name}>\n{body}\n</{name}>"


def _text(content):
    return content.decode("utf-8", errors="replace")


def _codebase(dev):
    files = sorted(dev._baseline.files, key=lambda f: f.path)
    policy = dev._policy
    lines = ["Files:", *(f"{f.path} ({len(f.content)} bytes)" for f in files), "",
             "Editable: " + ", ".join(policy.modify) + ".",
             "New files allowed under: "
             + ", ".join(p + "/" for p in policy.create_under) + ".",
             "Protected: " + ", ".join(f.path for f in files
                                       if f.path not in policy.modify) + "."]
    registry = next((f for f in files if f.path == REGISTRY), None)
    if registry is not None:
        lines += ["", f'<file path="{REGISTRY}">\n{_text(registry.content)}\n</file>']
    return "\n".join(lines)


def _planned_files(dev, plan):
    originals = {f.path: f.content for f in dev._baseline.files}
    shown = [f'<file path="{c.path}">\n{_text(originals[c.path])}\n</file>'
             for c in plan.changes
             if c.operation == "modify" and c.path in originals]
    return "\n\n".join(shown) or "No existing files change; every planned file is new."


def _diff(dev):
    originals = {f.path: f.content for f in dev._baseline.files}
    chunks = []
    for file in sorted(dev._development._artifact.files, key=lambda f: f.path):
        before = originals.get(file.path)
        if before == file.content:
            continue
        chunks.extend(difflib.unified_diff(
            _text(before).splitlines(keepends=True) if before is not None else [],
            _text(file.content).splitlines(keepends=True),
            fromfile="a/" + file.path if before is not None else "/dev/null",
            tofile="b/" + file.path))
    return "".join(chunks).rstrip("\n") or "No changes."


def _log(value):
    try:
        text = base64.b64decode(value, validate=True).decode("utf-8", "replace")
    except ValueError:
        return ""
    return text


def render_history(history):
    lines = []
    for item in history:
        kind = item.get("kind")
        report = item.get("role_report") or {}
        if kind == "review":
            code = (item.get("report", {}).get("binding") or {}).get("artifact_sha256")
            stage = "code review" if code else "forward review"
            accepted = (item.get("report", {}).get("approved")
                        and not item.get("report", {}).get("unresolved_blockers"))
            block = [f"{stage} {'approved' if accepted else 'rejected'}"]
            block += [f"[{finding.get('severity')}] {finding.get('target')}: "
                      f"{finding.get('issue')}. Fix: {finding.get('fix')}"
                      for finding in report.get("findings", [])]
            lines.append("\n".join(block))
        elif kind == "checks":
            failed = [c for c in report.get("checks", []) if not c.get("passed")]
            if not failed:
                lines.append("checks passed")
            for check in failed:
                output = (_log(check.get("stdout", "")) + _log(check.get("stderr", "")))
                lines.append(f"check failed: {check.get('name')} "
                             f"(exit {check.get('exitcode')})\n"
                             + output[-CHECK_LOG_CHARS:].rstrip())
        elif kind == "invalid":
            lines.append(f"invalid reply from {item.get('role')}: {item.get('error')}")
    return "\n\n".join(lines)


def render_message(dev, context, settings):
    """The model's user message: tagged sections, only what this step needs."""
    role, stage = context["role"], context["stage"]
    step = stage if role == "review" else role
    contract = dev._controller.config.contract
    plan = dev._development._plan
    parts = [
        _tag("request", contract.original_request),
        _tag("requirements", "\n".join(f"- {r.id}: {r.acceptance}"
                                        for r in contract.requirements)),
        _tag("checks", "\n\n".join(
            f'<check name="{f.path[:-3]}">\n{_text(f.content)}\n</check>'
            for f in settings.checks)),
    ]
    if step in {"plan", "forward_review"}:
        parts.append(_tag("codebase", _codebase(dev)))
    if step != "plan" and plan is not None:
        parts.append(_tag("plan", json.dumps(asdict(plan), ensure_ascii=False,
                                            indent=1)))
    if step == "execute":
        parts.append(_tag("files", _planned_files(dev, plan)))
        previous = dev._previous_edits
        if previous is not None and previous[0] == plan.sha256:
            parts.append(_tag("previous_edits", json.dumps(
                previous[1], ensure_ascii=False, indent=1)))
    if step == "code_review":
        parts.append(_tag("diff", _diff(dev)))
    feedback = dev._controller.config.feedback
    if step in {"plan", "execute"} and feedback:
        parts.append(_tag("prior_attempts", "\n".join("- " + f for f in feedback)))
    history = render_history(context["history"])
    if history:
        parts.append(_tag("history", history))
    return "\n\n".join(parts)


def role_spec(dev, context, reply, settings, timeout_ms):
    source = (dev._baseline if context["role"] == "execute"
              else dev._development._artifact or dev._baseline)
    baseline = Snapshot((
        File("driver.py", settings.driver), File("request.json", encode(context)),
        File("reply.json", reply),
        *(File("source/" + f.path, f.content) for f in source.files),
        *(File("checks/" + f.path, f.content) for f in settings.checks),
    ))
    modifier = context["role"] == "execute"
    policy = ChangePolicy(
        baseline.sha256,
        modify=tuple("source/" + p for p in dev._policy.modify if modifier),
        create_under=tuple("source/" + p for p in dev._policy.create_under
                           if modifier),
    )
    binding = (dev.binding if context["binding"]
               else dev._initial_binding(dev._controller))
    return FixtureSpec(
        uuid.uuid4().hex, dev._settings.image_id, dev._settings.image_environment,
        baseline, policy, "driver.py", timeout_ms,
        replace(binding, baseline_sha256=baseline.sha256),
    )


def _invalid_reply(value):
    if type(value) is dict and set(value) == {"invalid"}:
        raise InvalidRoleReply(str(value["invalid"]))


def plan_result(value, contract, policy):
    _invalid_reply(value)
    if type(value) is not dict or set(value) != {"changes", "verification"}:
        raise InvalidRoleReply("A plan needs exactly changes and verification")
    try:
        plan = Plan(
            contract.sha256,
            tuple(PlannedChange(item["path"], item["operation"],
                                tuple(item["requirement_ids"]), item["reason"])
                  for item in value["changes"]),
            tuple(Verification(item["requirement_id"], item["method"])
                  for item in value["verification"]),
        )
        plan.validate(contract, policy)
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidRoleReply(f"invalid plan: {error}") from error
    return plan


def review_result(value, context=None):
    _invalid_reply(value)
    if (
        type(value) is not dict
        or set(value) != {"approved", "findings", "rationale"}
        or type(value["approved"]) is not bool
        or type(value["findings"]) is not list or len(value["findings"]) > 32
        or type(value["rationale"]) is not str or not value["rationale"].strip()
    ):
        raise InvalidRoleReply(
            "A review needs approved (boolean), findings (list) and rationale")
    blockers = []
    for finding in value["findings"]:
        if (type(finding) is not dict
                or set(finding) != {"severity", "target", "issue", "fix"}
                or finding["severity"] not in {"blocking", "advisory"}
                or not all(type(finding[k]) is str for k in ("target", "issue", "fix"))
                or not finding["issue"].strip()):
            raise InvalidRoleReply(
                "Each finding needs severity (blocking or advisory), target, "
                "issue and fix")
        if finding["severity"] == "blocking":
            blockers.append(f"{finding['target']}: {finding['issue']}")
    return tuple(blockers)


def extract_source(snapshot):
    return Snapshot(tuple(File(f.path[7:], f.content) for f in snapshot.files
                          if f.path.startswith("source/")))


def check_result(value, settings):
    if set(value) != {"checks"} or type(value["checks"]) is not list:
        raise IntegrityError("Unexpected check report")
    results = []
    for expected, result in zip(settings.checks, value["checks"], strict=True):
        if (
            set(result) != {"name", "passed", "exitcode", "stdout", "stderr"}
            or result["name"] != expected.path[:-3]
            or type(result["passed"]) is not bool
            or type(result["exitcode"]) is not int
            or result["passed"] != (result["exitcode"] == 0)
        ):
            raise IntegrityError("Check outcome differs from frozen inventory")
        for key in ("stdout", "stderr"):
            base64.b64decode(result[key], validate=True)
        results.append((result["name"], result["passed"]))
    return tuple(results)
