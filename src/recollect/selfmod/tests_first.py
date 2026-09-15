"""Tests first: requirements and deterministic checks, frozen before any code.

The original request is the source of truth. The implementation agent derives
requirements from it and writes stdlib-only check scripts for the request itself
plus general and edge cases. An independent reviewer approves them against the
request before planning starts; malformed or rejected tests are revised with the
recorded findings, without a cap. The frozen checks, plus a fixed regression
check, are reused unchanged by every attempt in the networkless role container.
"""

import ast
import re
from dataclasses import asdict, dataclass

import httpx

from . import role_worker
from .contracts import File, Requirement, Snapshot, digest
from .journal import IntegrityError, encode

ANCHOR = "original_request"
MAX_AUTHORED = 15
HISTORY = 8
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,47}")

AUTHOR_PROMPT = (
    "Write deterministic tests BEFORE any implementation exists. The original "
    "request is the source of truth. Return JSON with requirements (id, "
    "acceptance) and checks (name, requirement_ids, text). Include the "
    "requirement original_request, whose acceptance is the complete outcome the "
    "original request asks for, and at least one check exercising it end to end; "
    "add general and edge-case checks. Each check is a standalone Python 3 "
    "stdlib-only script run from the subagent source tree root with "
    "python -I -S -B, with no network and no credentials: put the tree root on "
    "sys.path yourself, fake every external service inside the script, and exit "
    "nonzero on failure. If the capability needs the user's authorization for an "
    "external service, test that the tool reports it is ready together with the "
    "exact authentication steps instead of failing. At most 15 checks; ids and "
    "names are identifiers. history holds earlier findings to resolve. "
    "Output only the raw JSON object, without Markdown fences or commentary."
)
REVIEW_PROMPT = (
    "Independently review these tests before any implementation exists. Approve "
    "only if they follow the original request exactly, exercise it end to end plus "
    "general and edge cases, are deterministic, and need no network or "
    "credentials. Do not write code or redefine the request. Return JSON: "
    "approved (boolean), findings (list of strings, empty when approved), "
    "rationale (string). "
    "Output only the raw JSON object, without Markdown fences or commentary."
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
    requirements: tuple[Requirement, ...]
    checks: tuple[File, ...]

    @property
    def names(self):
        return tuple(f.path[:-3] for f in self.checks)

    @property
    def sha256(self):
        return digest({"requirements": [asdict(r) for r in self.requirements],
                       "checks_sha256": Snapshot(self.checks).sha256})


def parse_tests(value):
    """Validate authored tests; the regression check is always appended."""
    if type(value) is not dict or set(value) != {"requirements", "checks"}:
        raise ValueError("Tests need exactly requirements and checks")
    requirements, checks = value["requirements"], value["checks"]
    if type(requirements) is not list or type(checks) is not list:
        raise ValueError("Requirements and checks must be lists")
    covered = {}
    for item in requirements:
        if (type(item) is not dict or set(item) != {"id", "acceptance"}
                or type(item["id"]) is not str or not NAME.fullmatch(item["id"])
                or item["id"] in covered
                or type(item["acceptance"]) is not str
                or not item["acceptance"].strip()):
            raise ValueError("Each requirement needs a unique id and acceptance")
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
                or type(item["text"]) is not str or not item["text"].strip()):
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
        tuple(Requirement(i["id"], i["acceptance"],
                          "checks: " + ", ".join(covered[i["id"]]))
              for i in requirements),
        (*files, REGRESSION),
    )


def parse_review(value):
    if (type(value) is not dict or set(value) != {"approved", "findings", "rationale"}
            or type(value["approved"]) is not bool
            or type(value["findings"]) is not list
            or any(type(f) is not str or not f.strip() for f in value["findings"])
            or type(value["rationale"]) is not str or not value["rationale"].strip()):
        raise ValueError("Invalid test review report")
    return value["approved"] and not value["findings"]


async def freeze_tests(request, gap, *, author, reviewer, record, stopped):
    """Author and review until approved; ``None`` only when the user stopped.

    ``author`` and ``reviewer`` are separate one-shot model calls taking
    ``(prompt, context)`` and returning parsed JSON. Transport errors propagate
    to the loop; malformed output and rejections become findings to revise.
    """
    history = []
    while not stopped():
        context = {"original_request": request, "capability_gap": gap,
                   "history": history[-HISTORY:]}
        try:
            tests = parse_tests(await author(AUTHOR_PROMPT, context))
        except ValueError as error:
            history.append({"stage": "authoring", "findings": [str(error)[:2048]]})
            await record("tests_rejected", history[-1])
            continue
        shown = {"requirements": [asdict(r) for r in tests.requirements],
                 "checks": [{"name": f.path[:-3], "text": f.content.decode()}
                            for f in tests.checks]}
        await record("tests_authored", {"tests_sha256": tests.sha256,
                                        "requirements": shown["requirements"]},
                     Snapshot(tests.checks))
        report = await reviewer(REVIEW_PROMPT, {
            "original_request": request, **shown, "history": history[-HISTORY:]})
        try:
            approved = parse_review(report)
        except ValueError as error:
            approved, report = False, {"findings": [str(error)], "rationale": ""}
        if approved:
            await record("tests_frozen", {"tests_sha256": tests.sha256,
                                          "rationale": report["rationale"]})
            return tests
        history.append({"stage": "review", "tests_sha256": tests.sha256,
                        "findings": report["findings"] or ["not approved"],
                        "rationale": report["rationale"]})
        await record("tests_rejected", history[-1])
    return None


def authoring(request, *, author, reviewer):
    """Loop port: ``(gap, stopped, record) -> FrozenTests | None``."""
    async def run(gap, stopped, record):
        return await freeze_tests(request, gap, author=author, reviewer=reviewer,
                                  record=record, stopped=stopped)
    return run


def model_completer(base_url, model, *, slot=None, transport=None):
    """A fresh one-shot chat completion per call, without a timeout."""
    async def complete(prompt, context):
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": encode(context).decode()}],
            "temperature": 0, "stream": False,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
            **({"id_slot": slot} if slot is not None else {}),
        }
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     timeout=None, transport=transport) as client:
            response = await client.post(
                base_url + "/chat/completions", content=encode(payload),
                headers={"Content-Type": "application/json"})
            response.raise_for_status()
        choices = response.json().get("choices")
        if (type(choices) is not list or len(choices) != 1
                or choices[0].get("finish_reason") != "stop"
                or type(choices[0].get("message", {}).get("content")) is not str):
            raise IntegrityError("Incomplete test-authoring inference")
        return role_worker.parse(choices[0]["message"]["content"].encode())
    return complete
