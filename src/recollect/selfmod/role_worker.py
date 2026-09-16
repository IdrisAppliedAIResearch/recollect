"""Stdlib-only role driver, executed only as an unprivileged container worker.

Model replies are data. Generated source is written only in the modifier's
provisioned subtree; only the separately frozen check scripts execute source.
"""

import base64
import ctypes
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_CHECK_LOG_BYTES = 32 * 1024
# Checks import the candidate tree and the image's pinned packages, nothing
# ambient: isolated mode ignores PYTHONPATH, so the path is set explicitly.
CHECK_RUNNER = (
    "import runpy, sys; path = sys.argv[1]; sys.argv = [path]; "
    "sys.path[:0] = ['/work/source', '/opt/python']; "
    "runpy.run_path(path, run_name='__main__')"
)


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def parse(raw):
    def invalid(value):
        raise ValueError("Nonfinite JSON value")

    if len(raw) > 128 * 1024:
        raise ValueError("Role reply exceeds bound")
    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    if type(result) is not dict:
        raise ValueError("Expected a JSON object")
    return result


def run_checks(context):
    # Candidate children share the worker UID. Make the reporting interpreter
    # non-dumpable so they cannot open its /proc descriptors or alter its memory.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(3, 0, 0, 0, 0) != 0:
        raise RuntimeError("Cannot protect the check result channel")
    results, total = [], 0
    for check in context["checks"]:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-c", CHECK_RUNNER,
                 "/work/checks/" + check + ".py"],
                cwd="/work/source", stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, check=False,
            )
            logs = []
            for stream in (stdout, stderr):
                stream.seek(0)
                data = stream.read(MAX_CHECK_LOG_BYTES + 1)
                total += len(data)
                # Base64 and per-check JSON must fit the 64 KiB supervisor stream.
                if total > MAX_CHECK_LOG_BYTES:
                    raise ValueError("Check output exceeds bound")
                logs.append(base64.b64encode(data).decode())
        results.append({"name": check, "passed": process.returncode == 0,
                        "exitcode": process.returncode,
                        "stdout": logs[0], "stderr": logs[1]})
    return {"checks": results}


def _staged_edits(context, reply):
    if type(reply) is not dict or set(reply) != {"edits"} or type(
            reply["edits"]) is not list:
        raise ValueError("the reply needs only edits, a list")
    expected = {item["path"]: item["operation"] for item in context["plan"]["changes"]}
    staged = {}
    for edit in reply["edits"]:
        if type(edit) is not dict or type(edit.get("path")) is not str:
            raise ValueError("each edit needs a path")
        path = edit["path"]
        if path not in expected:
            raise ValueError(f"{path} is not in the plan")
        if path in staged:
            raise ValueError(f"{path} is edited more than once")
        operation = expected[path]
        target = Path("source") / path
        if operation not in {"modify", "create"}:
            raise ValueError(f"{path}: deletion is not supported")
        if operation == "create" and target.exists():
            raise ValueError(f"{path} already exists; the plan says create")
        if operation == "modify" and not target.is_file():
            raise ValueError(f"{path} does not exist; the plan says modify")
        if set(edit) == {"path", "text"} and type(edit["text"]) is str:
            data = edit["text"].encode("utf-8")
        elif (set(edit) == {"path", "replace"} and operation == "modify"
              and type(edit["replace"]) is list and edit["replace"]):
            current = target.read_text(encoding="utf-8")
            for item in edit["replace"]:
                if (type(item) is not dict or set(item) != {"old", "new"}
                        or type(item["old"]) is not str or not item["old"]
                        or type(item["new"]) is not str):
                    raise ValueError(f"{path}: each replacement needs old and new")
                count = current.count(item["old"])
                if count != 1:
                    raise ValueError(f"{path}: old text appears {count} times, not "
                                     f"exactly once: {item['old'][:120]!r}")
                current = current.replace(item["old"], item["new"], 1)
            data = current.encode("utf-8")
        else:
            raise ValueError(f"{path}: give text for a new file, or replace for an "
                             "existing one")
        if len(data) > 256 * 1024:
            raise ValueError(f"{path} exceeds 256 KiB")
        staged[path] = data
    missing = sorted(expected.keys() - staged.keys())
    if missing:
        raise ValueError("missing planned edits: " + ", ".join(missing))
    return expected, staged


def apply_edits(context, reply):
    """Apply every planned edit, or none; a bad reply is reported, not raised."""
    if type(reply) is dict and set(reply) == {"invalid"}:
        return {"invalid": str(reply["invalid"])[:2048]}
    try:
        expected, staged = _staged_edits(context, reply)
    except ValueError as error:
        return {"invalid": str(error)[:2048]}
    for path, data in staged.items():
        target = Path("source") / path
        if expected[path] == "create":
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(data)
        else:
            with target.open("r+b") as output:
                output.write(data)
                output.truncate()
    return {"edited": sorted(staged)}


def main():
    if Path.cwd() != Path("/work") or os.geteuid() != 65532:
        raise RuntimeError("Role driver must run in the qualified container")
    context = parse(Path("request.json").read_bytes())
    role = context["role"]
    if role == "checks":
        result = run_checks(context)
    else:
        reply = parse(Path("reply.json").read_bytes())
        result = apply_edits(context, reply) if role == "execute" else reply
    report = {"request_id": context["request_id"], "role": role, "result": result}
    encoded = (json.dumps(report, sort_keys=True, separators=(",", ":"),
                          allow_nan=False, ensure_ascii=False) + "\n").encode()
    if len(encoded) > 65536:
        raise ValueError("Role report exceeds bound")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
