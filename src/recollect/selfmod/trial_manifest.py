"""Frozen runtime manifest for one primary attempt: schema, identities, comparison.

The user freezes a manifest at preregistration with ``current_identities`` plus
the private experiment inputs; the trial never writes one. At CP0 the trial
recomputes the same identities and compares them field by field. Any mismatch,
missing value or unreadable identity fails ``runtime_frozen`` rather than being
repaired. The manifest's own SHA-256 is the controller's ``runtime`` registration.
"""

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .contracts import require_digest
from .journal import IntegrityError

SCHEMA_VERSION = 1
REGISTRATION_DOCUMENTS = {
    "protocol": "SELF_MODIFICATION_PREREGISTRATION.md",
    "checkpoints": "SELF_MODIFICATION_CHECKPOINTS_V1.md",
    "amendment": "SELF_MODIFICATION_AMENDMENT_01.md",
    "timing_amendment": "SELF_MODIFICATION_AMENDMENT_02.md",
}
FROZEN_DOCUMENTS = ("docs/SELF_MODIFICATION_ISSUE15_REQUIREMENTS.md",)
SECTIONS = {
    "schema_version", "attempt_id", "registrations", "source", "subagent",
    "model", "native", "evaluator", "calendar", "experiment", "documents",
}
REQUEST_TEMPLATE = (
    'On my connected Google test calendar, create a 30-minute event called '
    '"Self-modification review {experiment_id}" on {event_date} at 3:00 p.m. '
    "America/Chicago. Do not invite anyone. If the subagent lacks this capability, "
    "have it request its own modification, test the candidate, switch to the "
    "working version, and finish this same request. Tell me when the event is "
    "verified and give me its link."
)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def registered_event_date(started_utc):
    """The protocol's date: the next America/Chicago calendar day after start."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    if started_utc.utcoffset() is None:
        raise ValueError("Trial start must be an aware UTC instant")
    local = started_utc.astimezone(ZoneInfo("America/Chicago")).date()
    return (local + timedelta(days=1)).isoformat()


def render_request(experiment_id, event_date):
    """The registered outcome-story request with frozen substitutions only."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", experiment_id):
        raise ValueError("Freeze a stable experiment ID")
    date.fromisoformat(event_date)
    return REQUEST_TEMPLATE.format(experiment_id=experiment_id, event_date=event_date)


@dataclass(frozen=True)
class RuntimeManifest:
    raw: bytes
    value: dict

    @classmethod
    def load(cls, path):
        raw = Path(path).read_bytes()
        value = json.loads(raw)
        if (type(value) is not dict or set(value) != SECTIONS
                or value["schema_version"] != SCHEMA_VERSION):
            raise IntegrityError("Runtime manifest schema mismatch")
        registrations = value["registrations"]
        if set(registrations) != set(REGISTRATION_DOCUMENTS):
            raise IntegrityError("Runtime manifest must pin every registration")
        for digest in registrations.values():
            require_digest(digest)
        experiment = value["experiment"]
        if experiment.get("request") != render_request(
                experiment.get("id", ""), experiment.get("event_date", "")):
            raise IntegrityError("Frozen request differs from the registered template")
        return cls(raw, value)

    @property
    def sha256(self):
        return hashlib.sha256(self.raw).hexdigest()

    def registrations(self):
        """ControllerConfig registrations: documents plus this manifest's digest."""
        return tuple(sorted({**self.value["registrations"],
                             "runtime": self.sha256}.items()))


def _git(repository, *args):
    result = subprocess.run(["git", *args], cwd=repository, capture_output=True,
                            check=False, timeout=120)
    if result.returncode:
        raise IntegrityError("git " + args[0] + " failed")
    return result.stdout


def source_identity(repository):
    """Exact committed source; any tracked change or untracked file is recorded."""
    head = _git(repository, "rev-parse", "HEAD").decode().strip()
    status = _git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    return {"git_head": head, "clean": not status.strip(),
            "status_sha256": hashlib.sha256(status).hexdigest()}


def document_identities(repository):
    docs = Path(repository) / "docs"
    return ({name: file_sha256(docs / document)
             for name, document in REGISTRATION_DOCUMENTS.items()},
            {path: file_sha256(Path(repository) / path) for path in FROZEN_DOCUMENTS})


def evaluator_identity(repository):
    """Digest of the frozen evaluator and its fixtures, in path order."""
    root = Path(repository) / "src" / "recollect" / "selfmod"
    names = ("candidate_evaluator.py", "provider_fixture.py", "acceptance.py",
             "provider_relay.py", "provider_broker.py", "calendar_evaluator.py")
    return hashlib.sha256(json.dumps(
        {name: file_sha256(root / name) for name in names},
        sort_keys=True).encode()).hexdigest()


async def collect_identities(repository, *, base_url, model, weight_path,
                             base_image_id, calendar, credential_store,
                             transport=None):
    """Every identity the freeze records and CP0 recomputes, by one procedure."""
    import asyncio

    import httpx

    from . import subagent_tree
    from .deployment import SubagentBundle
    from .google_auth import token_path

    registrations, documents = document_identities(repository)
    async with httpx.AsyncClient(transport=transport, trust_env=False,
                                 timeout=None) as client:
        props = (await client.get(base_url.rstrip("/").removesuffix("/v1")
                                  + "/props")).json()
    tree = subagent_tree.baseline(repository)
    scopes = {role: json.loads(token_path(credential_store, role).read_text(
        encoding="utf-8")).get("scope") for role in ("worker", "verifier")}
    return {
        "registrations": registrations, "documents": documents,
        "source": source_identity(repository),
        "subagent": {"baseline_sha256": tree.sha256,
                     "policy_sha256": subagent_tree.change_policy(tree).sha256,
                     "bundle_digest": SubagentBundle(tree, base_image_id,
                                                     subagent_tree.LAUNCH).digest,
                     "base_image_id": base_image_id},
        "model": {"model": model, "base_url": base_url,
                  "weight_path": str(weight_path),
                  "weight_sha256": await asyncio.to_thread(file_sha256,
                                                           Path(weight_path)),
                  "total_slots": props.get("total_slots"),
                  "n_ctx": (props.get("default_generation_settings") or {}
                            ).get("n_ctx")},
        "evaluator": {"sha256": evaluator_identity(repository)},
        "calendar": {**calendar, "scopes": scopes},
    }


def freeze(identities, inputs):
    """Assemble a manifest from freshly collected identities and private inputs.

    Run only by the user at preregistration; it refuses an unclean source tree.
    """
    from .candidate_evaluator import EVALUATION_CHECKS

    if identities["source"]["clean"] is not True:
        raise IntegrityError("Freeze requires a committed, clean source tree")
    experiment = dict(inputs["experiment"])
    experiment["request"] = render_request(experiment["id"],
                                           experiment["event_date"])
    value = {
        "schema_version": SCHEMA_VERSION, "attempt_id": inputs["attempt_id"],
        "registrations": identities["registrations"],
        "documents": identities["documents"], "source": identities["source"],
        "subagent": identities["subagent"],
        "model": {**identities["model"], "lane_probes": inputs["lane_probes"]},
        "native": inputs["native"],
        "evaluator": {**identities["evaluator"], "checks": list(EVALUATION_CHECKS)},
        "calendar": identities["calendar"], "experiment": experiment,
    }
    return json.dumps(value, indent=1, sort_keys=True).encode() + b"\n"


def compare(frozen, current, prefix=""):
    """Every frozen leaf must be present and equal; returns the mismatch paths."""
    mismatches = []
    if isinstance(frozen, dict):
        if not isinstance(current, dict):
            return [prefix or "/"]
        for key, value in frozen.items():
            mismatches += compare(value, current.get(key, _MISSING),
                                  f"{prefix}/{key}")
        return mismatches
    return [] if frozen == current else [prefix]


_MISSING = object()
