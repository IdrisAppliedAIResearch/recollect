"""Immutable subagent bundles and journaled A/B routing with rollback to A.

A bundle is the exact accepted candidate tree plus a required dependency lock,
the pinned base image and launch metadata, all bound by one digest. Images are
produced by create/copy/commit from the local base image (never pull or rebuild)
and verified by reading the bundle bytes back out of the image before serving.

The router is harness state, not a worker capability. Existing tasks keep the
deployment they were bound to; new tasks bind to the serving deployment. B
serves only after its activation is committed, and the original request's
continuation stays blocked until it is released. Failure, rollback or recovery
restores A, which is never modified. A retry may then stage a fresh B; work bound
to a voided B never serves. Blocking methods belong off the event loop.
"""

import io
import json
import re
import tarfile
from dataclasses import dataclass

from .contracts import File, Snapshot, require_digest
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
        """An image serving exactly this bundle: reuse a verified one, else build it.

        Rebuilding identical bytes on every start only piles up stale images.
        The label is a lookup hint; reuse still requires full verification.
        """
        # Committed bundle images are untagged, which only --all lists.
        existing = (await self._checked(
            "images", "--all", "--quiet", "--no-trunc", "--filter",
            "label=recollect.bundle=" + bundle.digest,
        )).decode().split()
        for image_id in dict.fromkeys(existing):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                continue
            try:
                await self.verify(bundle, image_id)
            except IntegrityError:
                continue
            return image_id
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

    async def _inspect(self, image_id):
        value = json.loads(await self._checked("image", "inspect", image_id))
        if len(value) != 1 or value[0].get("Id") != image_id:
            raise IntegrityError("Bundle image identity mismatch")
        return value[0]

    async def verify(self, bundle, image_id):
        """Only an image with exact labels, base layers and bundle bytes may serve.

        Labels are settable by anyone, so they are only a first filter: the base
        image's layers must be an exact prefix of this image's layers, and the
        copied-out bundle tree must equal the accepted digest byte for byte.
        """
        image = await self._inspect(image_id)
        base = await self._inspect(bundle.base_image_id)
        labels = (image.get("Config") or {}).get("Labels") or {}
        layers = (image.get("RootFS") or {}).get("Layers") or []
        base_layers = (base.get("RootFS") or {}).get("Layers") or []
        if (labels.get("recollect.bundle") != bundle.digest
                or labels.get("recollect.candidate") != bundle.candidate.sha256):
            raise IntegrityError("Bundle image labels mismatch")
        if (not base_layers or len(layers) <= len(base_layers)
                or layers[:len(base_layers)] != base_layers):
            raise IntegrityError("Bundle image is not built on the pinned base image")
        container = (await self._checked("create", "--pull=never", "--network",
                                         "none", image_id)).decode().strip()
        try:
            raw = await self._checked("cp", container + ":" + BUNDLE_PARENT + "/"
                                      + BUNDLE_ROOT, "-")
        finally:
            await self._checked("rm", container)
        files, manifest = read_bundle_tar(raw)
        # Compare exact path -> bytes; snapshot tuples may differ only in order.
        served = {f.path: f.content for f in files.files}
        accepted = {f.path: f.content for f in bundle.candidate.files}
        if served != accepted or manifest != encode(bundle.manifest):
            raise IntegrityError("Served bundle bytes differ from the accepted digest")
        return VerifiedImage(bundle, image_id, _VERIFIED)


_VERIFIED = object()


@dataclass(frozen=True)
class VerifiedImage:
    """Minted only by BundleImages.verify; routing accepts nothing else."""

    bundle: SubagentBundle
    image_id: str
    token: object

    def __post_init__(self):
        if self.token is not _VERIFIED:
            raise IntegrityError("Image verification receipts come only from verify")


def bundle_skills(bundle):
    return Snapshot(tuple(File(f.path[len("skills/"):], f.content)
                          for f in bundle.candidate.files
                          if f.path.startswith("skills/")))


def materialize_skills(bundle, destination):
    """Write a verified bundle's skills tree for its read-only config mount."""
    from .files import materialize

    skills = bundle_skills(bundle)
    if not skills.files:
        raise IntegrityError("Deployment bundle has no skills tree")
    materialize(destination, skills)
    return destination


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
        self._rolled_back = False
        self._b_epoch = None
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
            else:
                self._rolled_back = False
        elif kind == "bound":
            self._tasks[data["task_id"]] = (data["role"], data["epoch"])
        elif kind == "activation_started":
            self._activation, self._serving = data["epoch"], "B"
            self._b_epoch = data["epoch"]
            self._epoch, self._committed = data["epoch"], False
        elif kind == "activation_committed":
            self._committed, self._activation = True, None
        elif kind == "rolled_back":
            self._serving, self._epoch = "A", data["epoch"]
            self._activation, self._committed = None, False
            self._rolled_back = True
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

    @property
    def live_b(self):
        """A B is staged, activating or serving and has not been rolled back."""
        return "B" in self._deployments and not self._rolled_back

    @staticmethod
    def _verified(verified):
        if type(verified) is not VerifiedImage:
            raise IntegrityError("Register only BundleImages.verify receipts")
        return verified

    def register_a(self, verified):
        verified = self._verified(verified)
        if "A" in self._deployments:
            raise IntegrityError("Deployment A is registered once")
        self._record("registered", {"role": "A",
                                    "bundle_digest": verified.bundle.digest,
                                    "image_id": verified.image_id,
                                    "candidate_sha256": None, "epoch": 1})

    def stage_b(self, verified):
        verified = self._verified(verified)
        if ("A" not in self._deployments or self._serving != "A"
                or self._activation is not None or self.live_b):
            raise IntegrityError("Stage B only while A serves and no B is live")
        self._record("registered", {
            "role": "B", "bundle_digest": verified.bundle.digest,
            "image_id": verified.image_id,
            "candidate_sha256": verified.bundle.candidate.sha256,
            "epoch": self._epoch,
        })

    def bind(self, task_id):
        """Record new work's deployment; binding never starts it and never moves.

        During an open activation new work binds to B, but ``route`` refuses to
        serve any B task until the activation is committed. After a rollback
        that work stays unserved: an uncommitted B never runs and nothing is
        silently moved to A.
        """
        if task_id in self._tasks:
            raise IntegrityError("Task is already bound to a deployment")
        if self.serving is None:
            raise IntegrityError("No serving deployment")
        self._record("bound", {"task_id": task_id, "role": self._serving,
                               "epoch": self._epoch})
        return self._deployments[self._serving]

    def route(self, task_id):
        role, epoch = self._tasks[task_id]
        # Work bound to an earlier, rolled-back B stays voided after a retry.
        if role == "B" and (not self._committed or epoch != self._b_epoch):
            raise IntegrityError(
                "B serves only after activation commit (blocked or voided)")
        if (role == "B" and task_id in self._continuations
                and task_id not in self._released):
            raise IntegrityError("Original-task continuation awaits release")
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
                or self._activation is not None or self._committed
                or self._rolled_back):
            raise IntegrityError("Activation requires a freshly staged B")
        self._record("activation_started", {
            "epoch": self._epoch + 1,
            "from": self._deployments["A"].bundle_digest,
            "to": self._deployments["B"].bundle_digest,
        })

    def evidence(self):
        records = self._journal.verify()
        return Snapshot((File("routing.jsonl", b"".join(r.body for r in records)),))

    def commit(self):
        """Serve the staged B after it passed its checks and started healthy."""
        if self._activation is None:
            raise IntegrityError("No open activation to commit")
        self._record("activation_committed", {
            "epoch": self._epoch,
            "bundle_digest": self._deployments["B"].bundle_digest})

    def release_continuation(self, task_id):
        if not self._committed or task_id not in self._continuations:
            raise IntegrityError("Continuation release requires a committed B")
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

    def deployment(self, role):
        return self._deployments.get(role)

    @classmethod
    def recover(cls, journal):
        """An activation interrupted before its commit restores A.

        Work bound to that uncommitted B stays unserved and the continuation
        stays blocked: recovery fails closed, never dispatching.
        """
        router = cls(journal)
        if router._activation is not None and not router._committed:
            router.rollback("recovered_uncommitted_activation")
        return router


class DeploymentSandboxes:
    """Hand each task the sandbox manager of its pinned deployment only.

    A manager is accepted for a role only when it launches exactly the image ID
    the router recorded for that role. Selection always goes through router
    routing, so existing tasks keep A and a blocked continuation cannot start.
    """

    def __init__(self, router):
        self._router, self._managers = router, {}

    def register(self, role, manager, verified):
        """Accept a manager only for the verified image and exact bundle skills."""
        from .files import verify_materialized

        recorded = self._router.deployment(role)
        pinned = getattr(manager, "deployment", None)
        current = self._managers.get(role)
        # A retried B has a new image; the same recorded image never re-registers.
        if current is not None and (recorded is None or getattr(
                current, "deployment", None) is None
                or current.deployment.image_id == recorded.image_id):
            raise IntegrityError("A deployment's sandbox manager is registered once")
        if (type(verified) is not VerifiedImage or recorded is None or pinned is None
                or not (pinned.image_id == recorded.image_id == verified.image_id)
                or recorded.bundle_digest != verified.bundle.digest):
            raise IntegrityError("Sandbox manager does not launch the recorded image")
        try:
            verify_materialized(pinned.skills_source, bundle_skills(verified.bundle))
        except (ValueError, OSError) as error:
            raise IntegrityError("Sandbox skills differ from verified bundle") from (
                error)
        self._managers[role] = manager

    def manager_for(self, task_id):
        deployment = self._router.route(task_id)
        manager = self._managers.get(deployment.role)
        if manager is None:
            raise IntegrityError("No verified sandbox manager for this deployment")
        return manager


class TaskDeployments:
    """The task coordinator's view of routing: bind, link and select only.

    Binding happens once when the coordinator durably creates a task; a linked
    continuation is bound to B by the router and is never served before release.
    Blocking journal writes belong off the event loop.
    """

    def __init__(self, router, sandboxes):
        if type(router) is not DeploymentRouter or type(sandboxes) is not (
                DeploymentSandboxes) or sandboxes._router is not router:
            raise IntegrityError("Task deployments need one router and its selector")
        self._router, self._sandboxes = router, sandboxes

    def is_bound(self, task_id):
        return task_id in self._router._tasks

    def bind(self, task_id):
        return self._router.bind(task_id)

    def link_continuation(self, task_id, parent_task_id):
        self._router.link_continuation(task_id, parent_task_id)

    def manager_for(self, task_id):
        return self._sandboxes.manager_for(task_id)
