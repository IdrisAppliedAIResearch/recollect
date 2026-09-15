"""Bounded model brokering and frozen, networkless role-process inputs.

These are one-shot structured actors, not an OpenCode shell or an interactive
tool loop. The controller owns their context, identities, limits and receipts.
"""

import asyncio
import base64
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import role_worker
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
from .controller import current_stamp
from .journal import IntegrityError, encode, sha256

PROMPTS = {
    "plan": (
        "Plan only the original task within the frozen change policy. Return JSON "
        "with changes (path, operation, requirement_ids, reason) and verification "
        "(requirement_id, method). Cover every requirement. No other keys. "
        "Output only the raw JSON object, without Markdown fences or commentary."
    ),
    "forward_review": (
        "Independently challenge this prospective plan against the original task. "
        "This is before implementation: baseline is the unchanged starting source, "
        "not a failed candidate. Do not require planned code or completed test "
        "results to exist yet; evaluate whether the proposed changes and checks "
        "would satisfy the task. Check "
        "necessity, bloat, unnecessary abstractions, regressions, failure handling "
        "and tests. Return JSON: approved (boolean), findings (list), rationale "
        "(string). Each finding has id (stable identifier), severity (blocking or "
        "advisory), status (open or resolved), requirement_id, path (or empty for "
        "a global issue), detail (original issue), resolution (empty while open). "
        "Keep each ID's original issue/target; record its resolution separately. "
        "Retain previously open blocking IDs with their "
        "current disposition. Do not implement or redefine done. "
        "Output only the raw JSON object, without Markdown fences or commentary."
    ),
    "execute": (
        "Implement the reviewed plan and original task, considering recorded "
        "findings. Return JSON with only edits: a list of path and text fields. "
        "Provide the complete UTF-8 contents of every planned file. No shell "
        "commands or extra files. Changes are applied to the ORIGINAL baseline. "
        "Output only the raw JSON object, without Markdown fences or commentary."
    ),
    "code_review": (
        "Independently review the complete candidate against the original task, "
        "reviewed plan, baseline and test results. Challenge missing planned work, "
        "unplanned additions, bloat, regressions and failure handling. Return JSON: "
        "approved (boolean), findings (list), rationale (string). Each finding "
        "has id (stable identifier), severity (blocking or advisory), status "
        "(open or resolved), requirement_id, path (or empty for a global issue), "
        "detail (original issue), resolution (empty while open). Keep each ID's "
        "original issue/target; record its resolution separately. "
        "Retain previously open blocking IDs with their current "
        "disposition. Do not implement or redefine done. "
        "Output only the raw JSON object, without Markdown fences or commentary."
    ),
}


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


def model_payload(context, settings):
    prompt = context["stage"] if context["role"] == "review" else context["role"]
    payload = {
        "model": settings.model,
        "messages": [{"role": "system", "content": dict(settings.prompts)[prompt]},
                     {"role": "user", "content": encode(context).decode()}],
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

    def __init__(self, settings: RoleSettings, *, transport=None):
        self.settings, self.transport = settings, transport
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
        reply = encode(role_worker.parse(content.encode()))
        return reply, bytes(raw)


def source_context(source):
    return [{"path": file.path, "text": file.content.decode("utf-8")}
            for file in sorted(source.files, key=lambda f: f.path)]


def make_context(dev, grant, settings):
    history = [r.value["data"] for r in dev._controller.journal.verify()
               if r.value["kind"] == "development_input"
               and r.value["data"].get("cycle_id") == dev._id
               and r.value["data"].get("kind") in {"plan", "review", "checks"}]
    return {
        "version": 1, "request_id": uuid.uuid4().hex,
        "role": grant.action, "stage": str(grant.stage),
        "actor_id": grant.actor_id, "grant_id": grant.grant_id,
        "controller_instance": grant.controller_instance,
        "cycle_id": grant.cycle_id, "generation": grant.generation,
        "binding": asdict(grant.binding) if grant.binding else None,
        "contract": asdict(dev._controller.config.contract),
        "policy": asdict(dev._policy),
        "baseline": source_context(dev._baseline),
        "candidate": (source_context(dev._development._artifact)
                      if dev._development._artifact is not None else []),
        "candidate_sha256": (dev._development._artifact.sha256
                             if dev._development._artifact is not None else None),
        "plan": asdict(dev._development._plan) if dev._development._plan else None,
        "history": history,
        "checks": [f.path[:-3] for f in settings.checks],
        "runner_sha256": settings.identity,
    }


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


def plan_result(value, contract, policy):
    if set(value) != {"changes", "verification"}:
        raise IntegrityError("Unexpected plan report fields")
    plan = Plan(
        contract.sha256,
        tuple(PlannedChange(item["path"], item["operation"],
                            tuple(item["requirement_ids"]), item["reason"])
              for item in value["changes"]),
        tuple(Verification(item["requirement_id"], item["method"])
              for item in value["verification"]),
    )
    plan.validate(contract, policy)
    return plan


def review_result(value, context):
    if (
        set(value) != {"approved", "findings", "rationale"}
        or type(value["approved"]) is not bool
        or type(value["findings"]) is not list or len(value["findings"]) > 32
        or type(value["rationale"]) is not str or not value["rationale"].strip()
    ):
        raise IntegrityError("Invalid independent review report")
    ids = {r["id"] for r in context["contract"]["requirements"]}
    paths = {f["path"] for f in context["baseline"] + context["candidate"]}
    paths.update(c["path"] for c in context["plan"]["changes"])
    previous = {}
    for record in context["history"]:
        for finding in record.get("role_report", {}).get("findings", []):
            previous[finding["id"]] = finding
    findings = {}
    for finding in value["findings"]:
        if (
            type(finding) is not dict or set(finding) != {
                "id", "severity", "status", "requirement_id", "path", "detail",
                "resolution",
            }
            or type(finding["id"]) is not str
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", finding["id"])
            or finding["id"] in findings
            or finding["severity"] not in {"blocking", "advisory"}
            or finding["status"] not in {"open", "resolved"}
            or finding["requirement_id"] not in ids
            or (finding["path"] not in paths | {""}
                and finding["id"] not in previous)
            or type(finding["detail"]) is not str or not finding["detail"].strip()
            or type(finding["resolution"]) is not str
            or (finding["status"] == "resolved" and not finding["resolution"].strip())
            or (finding["status"] == "open" and finding["resolution"] != "")
        ):
            raise IntegrityError("Invalid or duplicate review finding")
        prior = previous.get(finding["id"])
        if prior and any(finding[k] != prior[k] for k in (
            "path", "requirement_id", "severity", "detail"
        )):
            raise IntegrityError("Review finding identity changed its target")
        findings[finding["id"]] = finding
    pending = {key for key, f in previous.items()
               if f["severity"] == "blocking" and f["status"] == "open"}
    if not pending <= findings.keys():
        raise IntegrityError("Review silently dropped a blocking finding")
    for key in pending:
        if findings[key]["severity"] != "blocking":
            raise IntegrityError("Resolve a blocking finding, do not downgrade it")
    return tuple(key + ": " + f["detail"] for key, f in findings.items()
                 if f["severity"] == "blocking" and f["status"] == "open")


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
