"""Immutable subagent bundles and journaled A/B routing gated by CP4.

A bundle is the exact accepted candidate tree plus a required dependency lock,
the pinned base image and launch metadata, all bound by one digest. Images are
produced by create/copy/commit from the local base image (never pull or rebuild)
and verified by reading the bundle bytes back out of the image before serving.

The router is harness state, not a worker capability. Existing tasks keep the
deployment they were bound to; new tasks bind to the serving deployment. B is
routed only during a CP4 activation whose original-task continuation stays
blocked until the controller seals CP4. Failure or recovery restores A.
Blocking methods belong off the event loop.
"""

import io
import json
import re
import tarfile
from dataclasses import dataclass

from .contracts import File, Snapshot, require_digest
from .controller import ACTIVATION_CHECKS
from .journal import IntegrityError, encode, sha256

BUNDLE_ROOT = "recollect-bundle"
BUNDLE_PARENT = "/opt"
LOCK_PATH = "dependencies.lock"


@dataclass(frozen=True)
class SubagentBundle:
    candidate: Snapshot
    base_image_id: str
    launch: tuple[tuple[str, str], ...]

    def __post_init__(self):
        if type(self.candidate) is not Snapshot or not self.candidate.files:
            raise ValueError("Freeze the exact accepted candidate tree")
        if not self.base_image_id.startswith("sha256:"):
            raise ValueError("Base image must be an immutable local image ID")
        require_digest(self.base_image_id[7:])
        paths = {f.path for f in self.candidate.files}
        if LOCK_PATH not in paths:
            raise ValueError("Bundle dependencies must be pinned by a lock file")
        if (type(self.launch) is not tuple
                or any(type(p) is not tuple or len(p) != 2
                       or any(type(v) is not str for v in p) for p in self.launch)
                or len({k for k, _ in self.launch}) != len(self.launch)):
            raise ValueError("Freeze launch metadata as unique string pairs")

    @property
    def manifest(self):
        return {
            "version": 1, "candidate_sha256": self.candidate.sha256,
            "base_image_id": self.base_image_id,
            "dependency_lock_sha256": sha256(next(
                f.content for f in self.candidate.files if f.path == LOCK_PATH)),
            "launch": dict(self.launch),
            "files": [{"path": f.path, "sha256": sha256(f.content),
                       "bytes": len(f.content)}
                      for f in sorted(self.candidate.files, key=lambda f: f.path)],
        }

    @property
    def digest(self):
        return sha256(encode(self.manifest))


def bundle_tar(bundle):
    """Deterministic archive of exactly the bundle tree under BUNDLE_ROOT."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        directories = {BUNDLE_ROOT}
        for file in bundle.candidate.files:
            parts = file.path.split("/")
            for index in range(1, len(parts)):
                directories.add("/".join([BUNDLE_ROOT, *parts[:index]]))
        for name in sorted(directories):
            info = tarfile.TarInfo(name)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o555, 0
            archive.addfile(info)
        for file in sorted(bundle.candidate.files, key=lambda f: f.path):
            info = tarfile.TarInfo(BUNDLE_ROOT + "/" + file.path)
            info.size, info.mode, info.mtime = len(file.content), 0o444, 0
            archive.addfile(info, io.BytesIO(file.content))
        manifest = encode(bundle.manifest)
        info = tarfile.TarInfo(BUNDLE_ROOT + "/.bundle-manifest.json")
        info.size, info.mode, info.mtime = len(manifest), 0o444, 0
        archive.addfile(info, io.BytesIO(manifest))
    return output.getvalue()


def read_bundle_tar(raw):
    """Parse a copied-out tree; links, specials and escapes are rejected."""
    files, manifest = {}, None
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive.getmembers():
            name = member.name.rstrip("/")
            if name != BUNDLE_ROOT and not name.startswith(BUNDLE_ROOT + "/"):
                raise IntegrityError("Unexpected path outside the bundle tree")
            if member.isdir():
                continue
            if not member.isfile() or ".." in name.split("/"):
                raise IntegrityError("Linked or special bundle entry")
            data = archive.extractfile(member).read()
            relative = name[len(BUNDLE_ROOT) + 1:]
            if relative == ".bundle-manifest.json":
                manifest = data
            elif relative in files:
                raise IntegrityError("Duplicate bundle entry")
            else:
                files[relative] = data
    return Snapshot(tuple(File(p, d) for p, d in sorted(files.items()))), manifest


class BundleImages:
    """Local image build/verification through a pinned host Docker CLI."""

    def __init__(self, docker):
        self._docker = docker

    async def _checked(self, *args, data=None):
        code, out, err = await self._docker.run(*args, data=data)
        if code:
            raise IntegrityError("Docker bundle operation failed: " + args[0] + " "
                                 + err.decode(errors="replace")[:512])
        return out

    async def build(self, bundle):
        container = (await self._checked(
            "create", "--pull=never", "--network", "none", bundle.base_image_id,
        )).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container):
            raise IntegrityError("Docker did not return a full container identity")
        try:
            await self._checked("cp", "-", container + ":" + BUNDLE_PARENT,
                                data=bundle_tar(bundle))
            image = (await self._checked(
                "commit", "--change", "LABEL recollect.bundle=" + bundle.digest,
                "--change", "LABEL recollect.candidate=" + bundle.candidate.sha256,
                container,
            )).decode().strip()
        finally:
            await self._checked("rm", container)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise IntegrityError("Docker did not return an immutable image ID")
        await self.verify(bundle, image)
        return image

    async def verify(self, bundle, image_id):
        """Only an image whose labels and copied-out bytes match may serve."""
        value = json.loads(await self._checked("image", "inspect", image_id))
        labels = (value[0].get("Config") or {}).get("Labels") or {} if value else {}
        if (len(value) != 1 or value[0].get("Id") != image_id
                or labels.get("recollect.bundle") != bundle.digest
                or labels.get("recollect.candidate") != bundle.candidate.sha256):
            raise IntegrityError("Bundle image identity or labels mismatch")
        container = (await self._checked("create", "--pull=never", "--network",
                                         "none", image_id)).decode().strip()
        try:
            raw = await self._checked("cp", container + ":" + BUNDLE_PARENT + "/"
                                      + BUNDLE_ROOT, "-")
        finally:
            await self._checked("rm", container)
        files, manifest = read_bundle_tar(raw)
        if files != bundle.candidate or manifest != encode(bundle.manifest):
            raise IntegrityError("Served bundle bytes differ from the accepted digest")
        return True


@dataclass(frozen=True)
class Deployment:
    role: str
    bundle_digest: str
    image_id: str
    candidate_sha256: str | None
    epoch: int


class DeploymentRouter:
    """Durable A/B routing receipts; state is replayed from the journal."""

    def __init__(self, journal):
        self._journal = journal
        self._deployments = {}
        self._serving = None
        self._epoch = 0
        self._tasks = {}
        self._finished = set()
        self._activation = None
        self._committed = False
        self._continuations = {}
        self._released = set()
        for record in journal.verify():
            self._apply(record.value["kind"], record.value["data"])

    def _record(self, kind, data):
        self._journal.append("deployment_" + kind, data)
        self._apply("deployment_" + kind, data)

    def _apply(self, kind, data):
        kind = kind.removeprefix("deployment_")
        if kind == "registered":
            self._deployments[data["role"]] = Deployment(
                data["role"], data["bundle_digest"], data["image_id"],
                data["candidate_sha256"], data["epoch"])
            if data["role"] == "A":
                self._serving, self._epoch = "A", data["epoch"]
        elif kind == "bound":
            self._tasks[data["task_id"]] = (data["role"], data["epoch"])
        elif kind == "activation_started":
            self._activation, self._serving = data["epoch"], "B"
            self._epoch, self._committed = data["epoch"], False
        elif kind == "activation_sealed":
            self._committed, self._activation = True, None
        elif kind == "rolled_back":
            self._serving, self._epoch = "A", data["epoch"]
            self._activation, self._committed = None, False
        elif kind == "continuation_linked":
            self._continuations[data["task_id"]] = data["parent_task_id"]
        elif kind == "continuation_released":
            self._released.add(data["task_id"])
            self._tasks[data["task_id"]] = ("B", data["epoch"])
        elif kind == "task_finished":
            self._finished.add(data["task_id"])

    @property
    def serving(self):
        return self._deployments.get(self._serving)

    @property
    def epoch(self):
        return self._epoch

    def register_a(self, bundle, image_id):
        if "A" in self._deployments:
            raise IntegrityError("Deployment A is registered once")
        self._record("registered", {"role": "A", "bundle_digest": bundle.digest,
                                    "image_id": image_id, "candidate_sha256": None,
                                    "epoch": 1})

    def stage_b(self, bundle, image_id):
        if "A" not in self._deployments or "B" in self._deployments:
            raise IntegrityError("Stage exactly one B after registering A")
        self._record("registered", {"role": "B", "bundle_digest": bundle.digest,
                                    "image_id": image_id,
                                    "candidate_sha256": bundle.candidate.sha256,
                                    "epoch": self._epoch})

    def bind(self, task_id):
        """Bind new work to the serving deployment; existing bindings never move."""
        if task_id in self._tasks:
            raise IntegrityError("Task is already bound to a deployment")
        if self.serving is None:
            raise IntegrityError("No serving deployment")
        self._record("bound", {"task_id": task_id, "role": self._serving,
                               "epoch": self._epoch})
        return self.route(task_id)

    def route(self, task_id):
        role, _ = self._tasks[task_id]
        if (role == "B" and task_id in self._continuations
                and task_id not in self._released):
            raise IntegrityError("Original-task continuation is blocked before CP4")
        return self._deployments[role]

    def link_continuation(self, task_id, parent_task_id):
        """Bind the original task's continuation to B, blocked until release."""
        if (self._activation is None or parent_task_id not in self._tasks
                or task_id in self._tasks):
            raise IntegrityError("Link a continuation during an open activation")
        self._record("continuation_linked", {"task_id": task_id,
                                             "parent_task_id": parent_task_id})
        self._record("bound", {"task_id": task_id, "role": "B", "epoch": self._epoch})

    def begin_activation(self):
        if ("B" not in self._deployments or self._serving != "A"
                or self._activation is not None or self._committed):
            raise IntegrityError("Activation requires staged B while A serves")
        self._record("activation_started", {
            "epoch": self._epoch + 1,
            "from": self._deployments["A"].bundle_digest,
            "to": self._deployments["B"].bundle_digest,
        })

    def activation_checks(self, *, b_healthy, a_available, target_quiescent,
                          baseline_empty):
        """Router-derived facts plus trusted host observations, exactly CP4's set."""
        linked = [t for t, parent in self._continuations.items()
                  if self._tasks.get(t, (None,))[0] == "B"
                  and self._tasks.get(parent, (None,))[0] == "A"]
        checks = {
            "route_changed": self._serving == "B" and self._activation is not None,
            "health_passed": b_healthy is True,
            "target_gate_closed": bool(linked) and not (set(linked) & self._released),
            "target_quiescent": target_quiescent is True,
            "baseline_empty": baseline_empty is True,
            "continuation_linked": len(linked) == 1,
            "a_available": a_available is True and "A" in self._deployments,
        }
        if set(checks) != ACTIVATION_CHECKS:
            raise IntegrityError("Router checks diverged from the frozen CP4 set")
        return checks

    def evidence(self):
        records = self._journal.verify()
        return Snapshot((File("routing.jsonl", b"".join(r.body for r in records)),))

    def seal(self, controller, checks):
        """Seal CP4 for the exact staged candidate; failure rolls routing back."""
        if self._activation is None:
            raise IntegrityError("No open activation to seal")
        b = self._deployments["B"]
        controller.dispatch("activate")
        try:
            controller.activated(b.candidate_sha256, checks, self.evidence())
        except BaseException:
            self.rollback("cp4_seal_failed")
            raise
        if controller._phase != "continue":
            self.rollback("cp4_failed")
            return False
        self._record("activation_sealed", {"epoch": self._epoch,
                                           "bundle_digest": b.bundle_digest})
        return True

    def release_continuation(self, task_id):
        if not self._committed or task_id not in self._continuations:
            raise IntegrityError("Continuation release requires sealed CP4")
        if task_id in self._released:
            raise IntegrityError("Continuation already released")
        self._record("continuation_released", {"task_id": task_id,
                                               "epoch": self._epoch})
        return self.route(task_id)

    def finish(self, task_id):
        if task_id not in self._tasks or task_id in self._finished:
            raise IntegrityError("Unknown or already finished task")
        self._record("task_finished", {"task_id": task_id})

    def drained(self, role):
        return not any(r == role and t not in self._finished
                       for t, (r, _) in self._tasks.items())

    def rollback(self, reason):
        if "A" not in self._deployments:
            raise IntegrityError("No A deployment to restore")
        self._record("rolled_back", {"reason": reason, "epoch": self._epoch + 1,
                                     "restored": self._deployments["A"].bundle_digest})

    @classmethod
    def recover(cls, journal):
        """An activation interrupted before its CP4 seal restores A for new work."""
        router = cls(journal)
        if router._activation is not None and not router._committed:
            router.rollback("recovered_unsealed_activation")
        return router
