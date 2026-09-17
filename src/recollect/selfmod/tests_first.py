"""Tests first: an interface, requirements and checks, frozen before any code.

The original request is the source of truth. The implementation agent reads the
request, the capability gap and the codebase, defines the interface the new
capability must provide, and writes checks that call it with every external
service faked. An independent reviewer approves them against the request and
what the check environment makes possible. Malformed, cut-off or rejected tests
are revised with the recorded findings, without a cap. The frozen tests, plus a
fixed regression check, are reused unchanged by every attempt.
"""

import ast
import json
import re
from dataclasses import asdict, dataclass, field

import httpx

from .contracts import File, IntegrityError, Requirement, Snapshot, digest, encode
from .subagent_tree import PROTECTED

ANCHOR = "original_request"
RESERVED = {"interface", "unverified"}
MAX_AUTHORED = 15
HISTORY = 8
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,47}")
MODULE = re.compile(r"recollect\.engine\.subagent_tools\.[a-z_][a-z0-9_]{0,47}")
REGISTRY = "recollect/engine/mcp_research.py"

AUTHOR_PROMPT = """<role>
You write the acceptance tests for a new capability before anyone implements it. Your tests define what done means.
</role>

<steps>
1. Read the request, the capability gap, the codebase and the check environment.
2. Define the interface the implementation must provide: a module under recollect/engine/subagent_tools/, its async functions (name, parameters, return shape), and the tool name it will be registered under in recollect/engine/mcp_research.py.
3. Write requirements. One has id original_request: the complete outcome the request asks for. Add one for each other behavior worth checking.
4. Write checks. Each check imports the interface and calls it, replacing every external service with a fake such as httpx.MockTransport. Cover the request end to end, general cases, and edge cases: bad input, service errors, missing authorization.
5. Add one check that mcp_research registers the tool.
6. If the capability needs the user's authorization for an external service, check that the tool returns a ready message with the exact authentication steps instead of failing.
7. If there is history, fix every finding in it.
</steps>

<rules>
- A model calls the tool, so every parameter must accept what a model sends: a JSON value arrives as an object or a list, not as a string. Accept both where either is natural, and check that.
- Test the interface's behavior. Don't depend on files outside the check, /workspace, or a running tool server.
- Import only what check_environment allows.
- If a property can't be verified with what's importable, check the strongest thing you can and list the rest in unverified.
- Deterministic only: no clock, randomness or network.
- At most 15 checks. Keep each one short.
</rules>

<output>
Only a JSON object, no Markdown fences:
{"interface": {"module": "recollect.engine.subagent_tools.<name>", "tool_name": "...", "functions": [{"name": "...", "signature": "...", "returns": "..."}]},
 "requirements": [{"id": "...", "acceptance": "..."}],
 "checks": [{"name": "...", "requirement_ids": ["..."], "text": "..."}],
 "unverified": ["..."]}
ids and names use letters, digits and underscores.
</output>"""

REVIEW_PROMPT = """<role>
You review acceptance tests for a new capability before it is implemented. You did not write them.
</role>

<steps>
1. Read the request, the codebase and the check environment.
2. Interface: does it fit the codebase, and can it deliver the request?
3. Requirements: together they cover the whole request, and original_request states the full outcome.
4. Checks: each calls the interface, fakes every external service, is deterministic, and actually verifies the requirements it names.
5. Unverified: is each item truly impossible to verify with what's importable?
</steps>

<rules>
- Judge against check_environment. Don't demand what it makes impossible; that belongs in unverified.
- Approve the strongest feasible tests. Don't hold out for perfection.
- Each finding names one check or requirement and one concrete fix.
- Don't write code or change the request.
</rules>

<output>
Only a JSON object, no Markdown fences:
{"approved": true, "findings": [], "rationale": "..."}
findings is empty when approved.
</output>"""

CHECK_ENVIRONMENT = (
    "- Runs as python check.py from the tree root, in a container with no "
    "network and no credentials.\n"
    "- Importable: the standard library, the tree (import recollect...), and the "
    "pinned packages httpx, mcp and trafilatura with their dependencies. Nothing "
    "else can be installed, by the checks or by the implementation.\n"
    "- Exit 0 to pass, nonzero to fail. Print the reason on failure.\n"
    "- Nothing else exists: no /workspace, no user files, no running tool server."
)

REGRESSION = File("regression.py", b'''"""Every Python file in the tree compiles."""
import pathlib
import sys

failures = []
for path in sorted(pathlib.Path(".").rglob("*.py")):
    try:
        compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as error:
        failures.append(f"{path}: {error}")
print("\\n".join(failures))
sys.exit(1 if failures else 0)
''')


@dataclass(frozen=True)
class FrozenTests:
    interface: dict
    requirements: tuple[Requirement, ...]
    checks: tuple[File, ...]
    unverified: tuple[str, ...] = field(default=())

    @property
    def names(self):
        return tuple(f.path[:-3] for f in self.checks)

    @property
    def sha256(self):
        return digest({"interface": self.interface,
                       "requirements": [asdict(r) for r in self.requirements],
                       "checks_sha256": Snapshot(self.checks).sha256,
                       "unverified": list(self.unverified)})

    @property
    def contract_requirements(self):
        """Requirements for development: the interface binds every attempt."""
        extra = [Requirement(
            "interface",
            "Implement exactly this interface: "
            + json.dumps(self.interface, sort_keys=True),
            "checks: " + ", ".join(self.names),
        )]
        if self.unverified:
            extra.append(Requirement(
                "unverified",
                "Not verified by checks; confirm in code review: "
                + "; ".join(self.unverified),
                "code review",
            ))
        return (*self.requirements, *extra)


def _text(value):
    return type(value) is str and bool(value.strip())


def _interface(value):
    if (type(value) is not dict or set(value) != {"module", "tool_name", "functions"}
            or type(value["module"]) is not str
            or not MODULE.fullmatch(value["module"])
            or type(value["tool_name"]) is not str
            or not NAME.fullmatch(value["tool_name"])
            or type(value["functions"]) is not list or not value["functions"]
            or any(type(f) is not dict or set(f) != {"name", "signature", "returns"}
                   or type(f["name"]) is not str or not NAME.fullmatch(f["name"])
                   or not _text(f["signature"]) or not _text(f["returns"])
                   for f in value["functions"])):
        raise ValueError(
            "interface needs module (recollect.engine.subagent_tools.<name>), "
            "tool_name and functions with name, signature and returns")
    return value


def parse_tests(value):
    """Validate authored tests; the regression check is always appended."""
    if (type(value) is not dict
            or set(value) != {"interface", "requirements", "checks", "unverified"}):
        raise ValueError(
            "Tests need exactly interface, requirements, checks and unverified")
    interface = _interface(value["interface"])
    requirements, checks, unverified = (
        value["requirements"], value["checks"], value["unverified"])
    if (type(requirements) is not list or type(checks) is not list
            or type(unverified) is not list):
        raise ValueError("requirements, checks and unverified must be lists")
    if len(unverified) > 16 or not all(_text(item) for item in unverified):
        raise ValueError("unverified lists at most 16 non-empty strings")
    covered = {}
    for item in requirements:
        if (type(item) is not dict or set(item) != {"id", "acceptance"}
                or type(item["id"]) is not str or not NAME.fullmatch(item["id"])
                or item["id"] in covered or item["id"] in RESERVED
                or not _text(item["acceptance"])):
            raise ValueError("Each requirement needs a unique id and acceptance "
                             "(interface and unverified are reserved)")
        covered[item["id"]] = []
    if ANCHOR not in covered:
        raise ValueError("Tests must be anchored on the original_request requirement")
    if not 1 <= len(checks) <= MAX_AUTHORED:
        raise ValueError("Write 1-15 checks")
    files = []
    for item in checks:
        if (type(item) is not dict or set(item) != {"name", "requirement_ids", "text"}
                or type(item["name"]) is not str or not NAME.fullmatch(item["name"])
                or item["name"] == "regression"
                or any(f.path == item["name"] + ".py" for f in files)
                or type(item["requirement_ids"]) is not list
                or not item["requirement_ids"]
                or any(r not in covered for r in item["requirement_ids"])
                or not _text(item["text"])):
            raise ValueError("Invalid, duplicate or unanchored check")
        try:
            ast.parse(item["text"])
        except SyntaxError as error:
            raise ValueError(f"Check {item['name']} does not parse: {error}") from error
        for requirement in dict.fromkeys(item["requirement_ids"]):
            covered[requirement].append(item["name"])
        files.append(File(item["name"] + ".py", item["text"].encode()))
    missing = [r for r, names in covered.items() if not names]
    if missing:
        raise ValueError("Requirements without a check: " + ", ".join(missing))
    return FrozenTests(
        interface,
        tuple(Requirement(i["id"], i["acceptance"],
                          "checks: " + ", ".join(covered[i["id"]]))
              for i in requirements),
        (*files, REGRESSION),
        tuple(unverified),
    )


def parse_review(value):
    if (type(value) is not dict or set(value) != {"approved", "findings", "rationale"}
            or type(value["approved"]) is not bool
            or type(value["findings"]) is not list
            or not all(_text(f) for f in value["findings"])
            or not _text(value["rationale"])):
        raise ValueError("Invalid test review report")
    return value["approved"] and not value["findings"]


def parse_reply(raw):
    """A model's JSON object reply; duplicate keys and NaN are rejected."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("Nonfinite JSON value")

    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    if type(result) is not dict:
        raise ValueError("Expected a JSON object")
    return result


def _tag(name, body):
    return f"<{name}>\n{body}\n</{name}>"


def message(request, gap, baseline, policy, *, tests=None, history=(),
            connections=None):
    """The user message both roles receive; the reviewer also gets the tests."""
    registry = next(f for f in baseline.files if f.path == REGISTRY)
    files = "\n".join(f"{f.path} ({len(f.content)} bytes)"
                      for f in sorted(baseline.files, key=lambda f: f.path))
    codebase = "\n".join((
        "Files:", files, "",
        "Editable: " + ", ".join(policy.modify) + ".",
        "New files allowed under: "
        + ", ".join(path + "/" for path in policy.create_under) + ".",
        "Protected: " + ", ".join(PROTECTED) + ".", "",
        f'<file path="{REGISTRY}">\n{registry.content.decode("utf-8")}\n</file>',
    ))
    parts = [
        _tag("request", request),
        _tag("capability_gap", json.dumps(
            {key: gap.get(key) for key in (
                "missing_capability", "attempted", "modification_request")},
            ensure_ascii=False, indent=1)),
        _tag("codebase", codebase),
        _tag("check_environment", CHECK_ENVIRONMENT),
    ]
    if connections:
        parts.append(_tag("connected_accounts", connections))
    if tests is not None:
        parts.append(_tag("tests", json.dumps(tests, ensure_ascii=False, indent=1)))
    if history:
        parts.append(_tag("history", json.dumps(list(history), ensure_ascii=False,
                                                indent=1)))
    return "\n\n".join(parts)


def _rejected(error):
    return (f"Your last reply was rejected: {error}. Fix it; if it was cut off, "
            "write shorter checks.")[:2048]


async def freeze_tests(request, gap, *, baseline, policy, author, reviewer, record,
                       stopped, connections=None):
    """Author and review until approved; ``None`` only when the user stopped.

    ``author`` and ``reviewer`` are separate one-shot model calls taking
    ``(prompt, message)`` and returning parsed JSON. Transport errors propagate
    to the loop; malformed, cut-off and rejected tests become findings.
    """
    history = []
    while not stopped():
        recent = history[-HISTORY:]
        try:
            authored = await author(AUTHOR_PROMPT, message(
                request, gap, baseline, policy, history=recent,
                connections=connections))
            tests = parse_tests(authored)
        except ValueError as error:
            history.append({"stage": "authoring", "findings": [_rejected(error)]})
            await record("tests_rejected", history[-1])
            continue
        await record("tests_authored", {
            "tests_sha256": tests.sha256, "interface": tests.interface,
            "requirements": [asdict(r) for r in tests.requirements],
            "unverified": list(tests.unverified),
        })
        try:
            report = await reviewer(REVIEW_PROMPT, message(
                request, gap, baseline, policy, tests=authored, history=recent,
                connections=connections))
            approved = parse_review(report)
        except ValueError as error:
            approved, report = False, {"findings": [_rejected(error)],
                                       "rationale": ""}
        if approved:
            await record("tests_frozen", {"tests_sha256": tests.sha256,
                                          "tool_name": tests.interface["tool_name"],
                                          "checks": len(tests.checks),
                                          "rationale": report["rationale"]})
            return tests
        history.append({"stage": "review", "tests_sha256": tests.sha256,
                        "findings": report["findings"] or ["not approved"],
                        "rationale": report["rationale"]})
        await record("tests_rejected", history[-1])
    return None


def authoring(request, *, baseline, policy, author, reviewer, connections=None):
    """Loop port: ``(gap, stopped, record) -> FrozenTests | None``."""
    async def run(gap, stopped, record):
        return await freeze_tests(request, gap, baseline=baseline, policy=policy,
                                  author=author, reviewer=reviewer, record=record,
                                  stopped=stopped, connections=connections)
    return run


def model_completer(base_url, model, *, slot=None, transport=None,
                    admission=None, lane=None):
    """A fresh one-shot chat completion per call, without a timeout or token cap."""
    async def complete(prompt, content):
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": content if type(content) is str
                          else encode(content).decode()}],
            "temperature": 0, "stream": False,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
            **({"id_slot": slot} if slot is not None else {}),
        }
        if admission is not None:
            await admission.acquire(lane=lane)
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                         timeout=None, transport=transport) as client:
                response = await client.post(
                    base_url + "/chat/completions", content=encode(payload),
                    headers={"Content-Type": "application/json"})
                response.raise_for_status()
        finally:
            if admission is not None:
                admission.release(lane=lane)
        choices = response.json().get("choices")
        if (type(choices) is not list or len(choices) != 1
                or type(choices[0].get("message", {}).get("content")) is not str):
            raise IntegrityError("Incomplete test-authoring inference")
        if choices[0].get("finish_reason") == "length":
            raise ValueError("the reply was cut off at the length limit")
        if choices[0].get("finish_reason") != "stop":
            raise IntegrityError("Incomplete test-authoring inference")
        return parse_reply(choices[0]["message"]["content"].encode())
    return complete
