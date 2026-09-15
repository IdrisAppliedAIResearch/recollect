"""Read-only collection of runtime/evaluator manifest inputs before registration.

This gathers identities and receipts and lists every unresolved input. It does
not freeze, publish, register, seal CP0 or run a target trial, and it never
changes services, models, containers, credentials or calendars. Output belongs
in a gitignored evidence directory for human review before any registration.
"""

import hashlib
import json
import re
import subprocess
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict
from pathlib import Path

IMMUTABLE_RECEIPTS = (
    "SELF_MODIFICATION_PREREGISTRATION.sha256",
    "SELF_MODIFICATION_CHECKPOINTS_V1.sha256",
    "SELF_MODIFICATION_AMENDMENT_01.sha256",
    "SELF_MODIFICATION_AMENDMENT_02.sha256",
    "SELF_MODIFICATION_PLANNING_CONVERSATION.sha256",
)
USER_GATED = (
    "cp0_concurrency_evidence_under_frozen_three_slot_profile",
    "model_weight_file_sha256_at_freeze",
    "google_authorization_performed_by_user",
    "private_test_calendar_id_alias_and_time_zone",
    "experiment_id_event_date_and_exact_request_bytes",
    "provider_deduplication_and_transient_error_contract_confirmation",
    "registered_main_chat_probes",
    "model_ingress_inference_cap_adaptation_per_amendment_02",
)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run(argv, *, cwd=None, runner=subprocess.run):
    """Observation command; its timeout bounds collection, not agent work."""
    result = runner(argv, cwd=cwd, capture_output=True, timeout=120, check=False)
    return result.returncode, result.stdout, result.stderr


def immutable_hashes(docs):
    """Check each detached receipt line against the current bytes."""
    results = []
    for receipt in IMMUTABLE_RECEIPTS:
        path = Path(docs) / receipt
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"([0-9a-f]{64}) [ *]?(.+)", line.strip())
            if not match:
                continue
            target = Path(docs) / match[2]
            actual = file_sha256(target) if target.is_file() else None
            results.append({"receipt": receipt, "file": match[2],
                            "recorded": match[1], "actual": actual,
                            "matches": actual == match[1]})
    return results


def junit_receipts(directory):
    receipts = []
    for path in sorted(Path(directory).glob("*.xml")):
        try:
            root = ElementTree.parse(path).getroot()
        except ElementTree.ParseError:
            receipts.append({"file": path.name, "parse_error": True})
            continue
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        totals = {key: sum(int(s.get(key, 0)) for s in suites)
                  for key in ("tests", "failures", "errors", "skipped")}
        receipts.append({"file": path.name, **totals,
                         "passed": totals["tests"] - totals["failures"]
                         - totals["errors"] - totals["skipped"],
                         "clean": totals["failures"] == totals["errors"] == 0})
    return receipts


def git_state(repo, output, *, runner=subprocess.run):
    head = run(["git", "rev-parse", "HEAD"], cwd=repo,
               runner=runner)[1].decode().strip()
    branch = run(["git", "branch", "--show-current"], cwd=repo,
                 runner=runner)[1].decode().strip()
    _, status, _ = run(["git", "status", "--porcelain=v1", "-z"], cwd=repo,
                       runner=runner)
    _, diff, _ = run(["git", "diff", "--binary", "HEAD"], cwd=repo, runner=runner)
    _, untracked, _ = run(["git", "ls-files", "--others", "--exclude-standard", "-z"],
                          cwd=repo, runner=runner)
    (output / "tracked.diff").write_bytes(diff)
    inventory = []
    for name in sorted(filter(None, untracked.decode().split("\0"))):
        path = Path(repo) / name
        if path.is_file():
            inventory.append({"path": name, "sha256": file_sha256(path),
                              "bytes": path.stat().st_size})
    (output / "untracked-inventory.json").write_text(
        json.dumps(inventory, indent=1, sort_keys=True))
    return {"head": head, "branch": branch, "clean": not status,
            "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
            "untracked_files": len(inventory),
            "untracked_inventory_sha256": hashlib.sha256(json.dumps(
                inventory, sort_keys=True).encode()).hexdigest()}


def frozen_profiles():
    """Deterministic runtime policy inputs already frozen in code."""
    from .native import VERSION, NativeSettings
    from .native_broker import CHAT_FIELDS, TOKEN_CAP_FIELDS, BrokerSettings
    from .native_containment import CAPABILITIES, ENTRYPOINT, TMPFS, _isolation
    from .provider_broker import MAX_MUTATION_ATTEMPTS, TRANSIENT_STATUS

    helpers = {}
    root = Path(__file__).parent
    for name in ("native_supervisor.py", "native_model_proxy.py",
                 "native_http_relay.py", "native_capture_worker.py",
                 "native_history_reader.py"):
        helpers[name] = file_sha256(root / name)
    return {
        "native_version": VERSION, "native_helpers_sha256": helpers,
        "native_isolation": _isolation(), "native_capabilities": list(CAPABILITIES),
        "native_entrypoint": ENTRYPOINT, "native_tmpfs": TMPFS,
        "broker_chat_fields": sorted(CHAT_FIELDS),
        "broker_stripped_token_caps": sorted(TOKEN_CAP_FIELDS),
        "broker_defaults": asdict(BrokerSettings("http://127.0.0.1:8001/v1", "model")),
        "native_config_template_keys": sorted(NativeSettings.__dataclass_fields__),
        "provider_max_mutation_attempts": MAX_MUTATION_ATTEMPTS,
        "provider_transient_status": sorted(TRANSIENT_STATUS),
    }


def collect(repo, output, *, docker_argv=None, model_url=None, runner=subprocess.run,
            fetch=None):
    """Write manifest inputs and return them; unresolved inputs stay explicit."""
    repo, output = Path(repo), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    inputs = {"collected_unix": int(time.time()), "registration": "not_performed",
              "cp0": "not_sealed", "git": git_state(repo, output, runner=runner),
              "immutable_documents": immutable_hashes(repo / "docs"),
              "profiles": frozen_profiles(),
              "receipts": junit_receipts(repo / ".agent")}
    if docker_argv:
        code, raw, _ = run([*docker_argv, "image", "inspect",
                            "recollect-opencode-sandbox:1.18.18"], runner=runner)
        image = json.loads(raw)[0] if code == 0 else {}
        config = image.get("Config") or {}
        inputs["sandbox_image"] = {"id": image.get("Id"),
                                   "environment": config.get("Env")}
    if model_url and fetch is not None:
        props = fetch(model_url.rstrip("/").removesuffix("/v1") + "/props")
        inputs["model_server"] = {
            "total_slots": props.get("total_slots"), "model_path": props.get(
                "model_path"),
            "n_ctx": (props.get("default_generation_settings") or {}).get("n_ctx"),
            "chat_template_sha256": hashlib.sha256(
                (props.get("chat_template") or "").encode()).hexdigest(),
        }
    code, gpu, _ = run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                        "--format=csv,noheader"], runner=runner)
    inputs["hardware"] = {"gpu": gpu.decode(errors="replace").strip() if code == 0
                          else None}
    unresolved = list(USER_GATED)
    if not all(item["matches"] for item in inputs["immutable_documents"]):
        unresolved.append("immutable_document_hash_mismatch")
    if any(not r.get("clean", False) for r in inputs["receipts"]):
        unresolved.append("failed_receipts_present_review_required")
    if (inputs.get("model_server") or {}).get("total_slots") != 3:
        unresolved.append("model_server_not_three_slot")
    inputs["unresolved"] = unresolved
    inputs["ready_for_registration"] = False
    (output / "manifest-inputs.json").write_text(
        json.dumps(inputs, indent=1, sort_keys=True))
    return inputs
