"""Agentic development: OpenCode agents plan, review and implement on their own.

Each role is a stock OpenCode session in its own container: the tree under
development is its workspace, and it has every native tool, bash and the
network, exactly as if a person were prompting it. The router talks to agents
only at the ends of their work. A plan and each review come back as one JSON
object in the agent's final message. The implementing agent continues the
planning session and is never interrupted. When it replies, the harness
captures its source tree, enforces the change policy, runs the frozen checks
in a networkless container and asks a fresh agent for a code review. Any
failure goes back into that same session as the next message.
"""

import asyncio
import base64
import contextlib
import difflib
import io
import json
import tarfile
import uuid
from pathlib import Path

from ..engine.sandbox.configgen import AGENT_NAME
from ..engine.sandbox.runner import _last_text
from .contracts import File, Snapshot
from .journal import Journal
from .role_worker import CHECK_RUNNER, MAX_CHECK_LOG_BYTES

SOURCE, CHECKS = "source", "checks"
#: Interpreter and build caches never count as changes to the tree.
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
JOURNAL_TEXT = 16 * 1024
VERDICT = ('{"approved": true, "findings": '
           '[{"severity": "blocking", "issue": "...", "fix": "..."}]}')
#: Unpack the stdin tar into /work, then run one check exactly as role workers do.
UNPACK_AND_CHECK = (
    "import os, sys, tarfile; "
    "tarfile.open(fileobj=sys.stdin.buffer, mode='r|').extractall('/work', "
    "filter='data'); os.chdir('/work/source'); "
    "os.execv(sys.executable, [sys.executable, '-I', '-S', '-B', '-c', "
    + repr(CHECK_RUNNER) + ", sys.argv[1]])"
)
PLAN = ('{"summary": "...", "changes": [{"path": "recollect/...", '
        '"operation": "modify", "reason": "..."}]}')


def _tag(name, body):
    return f"<{name}>\n{body}\n</{name}>"


def final_json(text, keys):
    """The last JSON object in a message that has every expected key."""
    decoder, found = json.JSONDecoder(), None
    for index, char in enumerate(text):
        if char != "{":
            continue
        with contextlib.suppress(ValueError):
            value, _ = decoder.raw_decode(text, index)
            if type(value) is dict and keys <= set(value):
                found = value
    return found


def valid_verdict(value):
    return (value is not None and type(value.get("approved")) is bool
            and type(value.get("findings")) is list)


def valid_plan(value):
    return (value is not None and type(value.get("changes")) is list
            and bool(value["changes"])
            and all(type(c) is dict and type(c.get("path")) is str
                    and c.get("operation") in {"modify", "create"}
                    for c in value["changes"]))


def read_tree(root):
    """The files an agent left under ``root``, caches aside."""
    files = []
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root)
        if (not path.is_file() or path.is_symlink()
                or IGNORED_PARTS & set(relative.parts) or path.suffix == ".pyc"):
            continue
        files.append((relative.as_posix(), path.read_bytes()))
    return files


def changes(baseline, files):
    """(path, operation) for every difference from the baseline tree."""
    before = {f.path: f.content for f in baseline.files}
    after = dict(files)
    result = [(p, "create") for p in after if p not in before]
    result += [(p, "modify") for p in after if p in before and after[p] != before[p]]
    result += [(p, "delete") for p in before if p not in after]
    return sorted(result)


def violations(policy, changed):
    found = []
    for path, operation in changed:
        try:
            permitted = policy.permits(path, operation)
        except ValueError:
            permitted = False
        if not permitted:
            found.append(f"{operation} {path}")
    return found


def unified_diff(baseline, files):
    before = {f.path: f.content for f in baseline.files}
    after = dict(files)
    parts = []
    for path, _ in changes(baseline, files):
        old = before.get(path, b"").decode(errors="replace").splitlines(keepends=True)
        new = after.get(path, b"").decode(errors="replace").splitlines(keepends=True)
        parts.extend(difflib.unified_diff(old, new, "a/" + path, "b/" + path))
    return "".join(parts)


def check_summary(results):
    lines = []
    for result in results:
        if result["passed"]:
            lines.append(f"{result['name']}: passed")
            continue
        output = "".join(base64.b64decode(result[s]).decode(errors="replace")
                         for s in ("stdout", "stderr")).strip()
        lines.append(f"{result['name']}: failed (exit {result['exitcode']})\n"
                     + output[-4000:])
    return "\n\n".join(lines)


class AgentSession:
    """One OpenCode session on a development sandbox, prompted like a person would."""

    def __init__(self, manager, name):
        self._manager, self._name = manager, name
        self._invocation = None

    @property
    def workspace(self):
        return self._invocation.handle.workdir

    async def __aenter__(self):
        # Continuous: model calls queue through the admitted ingress per request.
        self._invocation = await self._manager.begin_invocation(
            self._name, continuous=True)
        return self

    async def __aexit__(self, kind, error, trace):
        if kind is not None:
            # Revoke the agent before its sandbox state is discarded.
            with contextlib.suppress(Exception):
                await asyncio.shield(self._manager.quiesce_invocation(self._invocation))
        await asyncio.shield(self._manager.finish_invocation(self._invocation))

    async def send(self, text):
        """Post one message and wait, however long the agent works, for its reply."""
        handle = self._invocation.handle
        response = await handle.client.post(
            f"/session/{self._invocation.oc_session_id}/message",
            json={"agent": AGENT_NAME, "parts": [{"type": "text", "text": text}]},
            timeout=None,
        )
        response.raise_for_status()
        payload = response.json()
        return _last_text(payload.get("parts") if isinstance(payload, dict) else None)


class DockerChecks:
    """Frozen checks against a captured tree, in a networkless container."""

    def __init__(self, docker, image_id, *, memory_mb=1024, pids=256):
        self._docker, self._image = docker, image_id
        self._memory, self._pids = memory_mb, pids

    async def __call__(self, tree, checks):
        # The tree travels over stdin: no host path is mounted into the check.
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for prefix, files in (("source/", tree.files), ("checks/", checks)):
                for file in files:
                    info = tarfile.TarInfo(prefix + file.path)
                    info.size, info.mode = len(file.content), 0o644
                    tar.addfile(info, io.BytesIO(file.content))
        data = archive.getvalue()
        return [await self._run(data, check) for check in checks]

    async def _run(self, data, check):
        code, out, err = await self._docker.run(
            "run", "--rm", "-i", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--user", "65532:65532", "--pids-limit", str(self._pids),
            "--memory", f"{self._memory}m",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs",
            "/work:rw,noexec,nosuid,nodev,size=256m,uid=65532,gid=65532,mode=0700",
            "--workdir", "/work", "--entrypoint", "python", self._image,
            "-I", "-S", "-B", "-c", UNPACK_AND_CHECK, "/work/checks/" + check.path,
            data=data)
        return {"name": check.path[:-3], "passed": code == 0, "exitcode": code,
                "stdout": base64.b64encode(out[-MAX_CHECK_LOG_BYTES:]).decode(),
                "stderr": base64.b64encode(err[-MAX_CHECK_LOG_BYTES:]).decode()}


class AgentDeveloper:
    """One attempt: plan, review, implement, then verify, all agentic.

    ``manager_factory(name)`` returns a development SandboxManager;
    ``run_checks(tree, checks)`` returns one result per frozen check.
    """

    def __init__(self, root, *, request, gap, baseline, policy, protected,
                 manager_factory, run_checks, on_event=None):
        self._root, self._request, self._gap = Path(root), request, gap
        self._baseline, self._policy, self._protected = baseline, policy, protected
        self._manager_factory, self._run_checks = manager_factory, run_checks
        self._on_event = on_event

    async def __call__(self, attempt, tests, feedback):
        journal = await asyncio.to_thread(
            Journal.create, self._root / f"attempt-{attempt}-{uuid.uuid4().hex[:8]}")
        builder = self._manager_factory(f"build-{attempt}")
        reviewer = self._manager_factory(f"review-{attempt}")
        run = _Attempt(self, journal, attempt, tests, feedback, reviewer)
        try:
            async with AgentSession(builder, f"selfmod-build-{attempt}") as session:
                return await run.develop(session)
        finally:
            for manager in (builder, reviewer):
                with contextlib.suppress(Exception):
                    await asyncio.shield(manager.teardown())
            with contextlib.suppress(Exception):
                await asyncio.to_thread(journal.close)


class _Attempt:
    def __init__(self, owner, journal, attempt, tests, feedback, reviewer):
        self._owner, self._journal, self._attempt = owner, journal, attempt
        self._tests, self._feedback, self._reviewer = tests, feedback, reviewer

    async def _record(self, kind, data, files=None):
        data = {"attempt": self._attempt, **data}
        extra = () if files is None else (files,)
        await asyncio.to_thread(self._journal.append, kind, data, *extra)
        if self._owner._on_event is not None:
            await self._owner._on_event(kind, data)

    # -- shared context --------------------------------------------------

    def _context(self):
        tests, owner = self._tests, self._owner
        requirements = "\n".join(f"- {r.id}: {r.acceptance} (evidence: {r.evidence})"
                                 for r in tests.contract_requirements)
        parts = [
            _tag("request", owner._request),
            _tag("missing_capability", json.dumps(owner._gap, indent=1)),
            _tag("interface", json.dumps(tests.interface, indent=1)),
            _tag("requirements", requirements),
        ]
        return "\n\n".join(parts)

    def _workspace_notes(self):
        names = ", ".join(self._tests.names)
        protected = ", ".join(self._owner._protected)
        creatable = ", ".join(p + "/" for p in self._owner._policy.create_under)
        return _tag("workspace", (
            f"- {SOURCE}/ is the codebase. Only changes inside {SOURCE}/ are kept.\n"
            f"- {CHECKS}/ holds the frozen tests: {names}. Run one with:\n"
            f"  cd /workspace/{SOURCE} && PYTHONPATH=/workspace/{SOURCE}:/opt/python "
            f"python ../{CHECKS}/NAME.py\n"
            "  Exit code 0 is a pass. Your edits to checks/ are ignored; the frozen "
            "copies run again, with no network, after you finish.\n"
            "- Put scratch files outside source/.\n"
            f"- In {SOURCE}/ you may modify any existing file except: {protected}. "
            f"You may create files under: {creatable}. Don't delete files.\n"
            "- Import only the standard library, the codebase, and the packages "
            f"pinned in {SOURCE}/dependencies.lock."))

    async def _materialize(self, workspace, tree, extra=()):
        files = Snapshot((*(File(f"{SOURCE}/{f.path}", f.content) for f in tree.files),
                          *(File(f"{CHECKS}/{f.path}", f.content)
                            for f in self._tests.checks), *extra))

        def write():
            for file in files.files:
                target = workspace.joinpath(*file.path.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(file.content)

        await asyncio.to_thread(write)

    # -- the attempt -----------------------------------------------------

    async def develop(self, session):
        owner = self._owner
        await self._materialize(session.workspace, owner._baseline)
        plan = await self._plan(session)
        await self._record("plan_approved", {"plan": plan})
        message = "\n\n".join([
            _tag("approved_plan", json.dumps(plan, indent=1)),
            _tag("instructions", (
                "Your job is to make the solution work.\n"
                "1. Implement the approved plan in source/.\n"
                "2. Run every check and fix the code until all of them pass.\n"
                "3. Reply with a short summary of what you changed."))])
        while True:
            reply = await session.send(message)
            await self._record("implementation_turn", {"reply": reply[-JOURNAL_TEXT:]})
            files = await asyncio.to_thread(read_tree, session.workspace / SOURCE)
            changed = changes(owner._baseline, files)
            broken = violations(owner._policy, changed)
            if not changed:
                message = _tag("feedback", (
                    "source/ has no changes. Implement the approved plan, run the "
                    "checks, then reply with a short summary."))
                await self._record("no_changes", {})
                continue
            if broken:
                await self._record("policy_violations", {"changes": broken})
                message = _tag("feedback", (
                    "These changes in source/ are not allowed:\n"
                    + "\n".join(f"- {v}" for v in broken)
                    + "\nUndo them (restore deleted or protected files, move scratch "
                    "files out of source/), keep the checks passing, then reply with "
                    "a short summary."))
                continue
            candidate = Snapshot(tuple(File(p, c) for p, c in files))
            results = await owner._run_checks(candidate, self._tests.checks)
            passed = all(r["passed"] for r in results)
            await self._record("checks", {"passed": passed, "results": [
                {"name": r["name"], "passed": r["passed"]} for r in results]})
            if not passed:
                message = _tag("feedback", (
                    "The frozen checks ran on source/ with no network and failed:\n\n"
                    + check_summary(results)
                    + "\n\nFix the cause, rerun the checks, then reply with a short "
                    "summary."))
                continue
            verdict = await self._code_review(plan, files, results)
            await self._record("code_review", verdict)
            if verdict["approved"]:
                await self._record("candidate", {"sha256": candidate.sha256,
                                                 "changes": [f"{o} {p}" for p, o
                                                             in changed]}, candidate)
                return candidate
            message = _tag("feedback", (
                "A reviewer rejected the change:\n"
                + json.dumps(verdict["findings"], indent=1)
                + "\n\nFix the findings, rerun the checks, then reply with a short "
                "summary."))

    async def _plan(self, session):
        owner = self._owner
        history = ""
        if self._feedback:
            history = "\n\n" + _tag("earlier_attempts", (
                "Earlier attempts failed. Avoid the same failures:\n"
                + "\n".join(f"- {f}" for f in self._feedback)))
        message = "\n\n".join([
            "<role>You are a developer adding a capability to this codebase. The "
            "capability is missing; your job is to build it, not to report that it "
            "is missing.</role>",
            self._context() + history, self._workspace_notes(),
            _tag("instructions", (
                "1. Explore source/ and read every check.\n"
                "2. Plan the smallest change that makes every check pass and meets "
                "every requirement. Don't edit source/ yet.\n"
                "3. End your final message with the plan as one JSON object:\n"
                + PLAN))])
        while True:
            reply = await session.send(message)
            plan = final_json(reply, {"changes"})
            if not valid_plan(plan):
                await self._record("plan_unreadable", {"reply": reply[-JOURNAL_TEXT:]})
                message = _tag("feedback", (
                    "Your final message needs the plan as one JSON object, with "
                    "operation modify or create for each change:\n" + PLAN))
                continue
            broken = violations(owner._policy,
                                [(c["path"], c["operation"]) for c in plan["changes"]])
            if broken:
                await self._record("plan_outside_policy", {"changes": broken})
                message = _tag("feedback", (
                    "The plan has changes that are not allowed:\n"
                    + "\n".join(f"- {b}" for b in broken)
                    + "\nRevise it and end with the full plan as one JSON object."))
                continue
            verdict = await self._review("plan_review", self._plan_review_message(plan),
                                         owner._baseline,
                                         (File("plan.json", json.dumps(
                                             plan, indent=1).encode()),))
            await self._record("plan_review", verdict)
            if verdict["approved"]:
                return plan
            message = _tag("feedback", (
                "A reviewer rejected the plan:\n"
                + json.dumps(verdict["findings"], indent=1)
                + "\n\nRevise the plan and end with the full plan as one JSON object."))

    def _plan_review_message(self, plan):
        return "\n\n".join([
            "<role>You review another developer's plan before any code is written."
            "</role>",
            self._context(), self._workspace_notes(),
            _tag("plan", json.dumps(plan, indent=1)),
            _tag("instructions", (
                "1. Read the plan (also in plan.json), every check, and the source "
                "files the plan touches.\n"
                "2. Would this plan make every check pass and meet every requirement, "
                "within the workspace rules?\n"
                "3. Approve unless something is wrong or missing. Don't reject for "
                "style. Don't edit source/.\n"
                "4. End your final message with one JSON object:\n" + VERDICT))])

    async def _code_review(self, plan, files, results):
        owner = self._owner
        tree = Snapshot(tuple(File(p, c) for p, c in files))
        diff = unified_diff(owner._baseline, files)
        unverified = "\n".join(f"- {u}" for u in self._tests.unverified) or "- none"
        message = "\n\n".join([
            "<role>You review another developer's finished change. You did not write "
            "it.</role>",
            self._context(), self._workspace_notes(),
            _tag("plan", json.dumps(plan, indent=1)),
            _tag("check_results", check_summary(results)),
            _tag("not_covered_by_checks", unverified),
            _tag("instructions", (
                "1. Read changes.diff and the changed files in source/.\n"
                "2. Does the change meet every requirement, including what the checks "
                "don't cover? You may run the checks and your own experiments.\n"
                "3. Approve unless something is wrong. Don't reject for style. Don't "
                "edit source/.\n"
                "4. End your final message with one JSON object:\n" + VERDICT))])
        return await self._review("code_review", message, tree,
                                  (File("changes.diff", diff.encode()),))

    async def _review(self, kind, message, tree, extra):
        """A fresh reviewer session; its verdict is the JSON in its final message."""
        async with AgentSession(self._reviewer,
                                f"selfmod-{kind}-{self._attempt}") as session:
            await self._materialize(session.workspace, tree, extra)
            while True:
                reply = await session.send(message)
                verdict = final_json(reply, {"approved", "findings"})
                if valid_verdict(verdict):
                    return {"approved": verdict["approved"],
                            "findings": verdict["findings"]}
                await self._record(kind + "_unreadable",
                                   {"reply": reply[-JOURNAL_TEXT:]})
                message = _tag("feedback", (
                    "Your final message needs the verdict as one JSON object:\n"
                    + VERDICT))
