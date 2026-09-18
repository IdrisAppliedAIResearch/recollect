"""Subagent bundle images and which one serves each task.

A bundle is the exact subagent tree plus a required dependency lock, the pinned
base image and launch metadata, all bound by one digest. Images are produced by
create/copy/commit from the local base image (never pull or rebuild) and verified
by reading the bundle bytes back out of the image before serving.

A serves new work. While a build is proven, B serves only the resumed request.
When B finishes it, B becomes A; if it fails or is canceled, B is discarded.
The only bundle image kept is A's.
"""

import io
import json
import re
import tarfile
from dataclasses import dataclass

from .contracts import (
    File,
    IntegrityError,
    Snapshot,
    encode,
    require_digest,
    sha256,
    write_tree,
)
from .subagent_tree import BUNDLE_PYTHONPATH

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
        return VerifiedImage(bundle, image_id)

    async def serving_surface(self, image_id, tool_name, connected):
        """Does the built image expose the new tool when it actually serves?

        A tool can be registered behind a connection gate and still pass the
        frozen checks, which fake the connection. Only the serving environment -
        the bundle import path plus whatever is really connected - settles it,
        so B is never staged while its new tool would be unreachable.
        """
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise IntegrityError("serving-surface check needs an immutable image ID")
        # Each assignment needs its own -e: a bare KEY=VALUE after the flags
        # is parsed as the image reference, and every probe dies with
        # "docker: invalid reference format".
        env = ["-e", "PYTHONPATH=" + BUNDLE_PYTHONPATH]
        connected = tuple(connected)
        if connected:
            env += ["-e", "RECOLLECT_CONNECTED_CONNECTORS=" + ",".join(connected)]
        snippet = (
            "import json, recollect.engine.mcp_research as m\n"
            "names = sorted({t.name for t in m.mcp._tool_manager.list_tools()})\n"
            "print(json.dumps({'ok': " + repr(tool_name)
            + " in names, 'tools': names}))\n"
        )
        code, out, err = await self._docker.run(
            "run", "--rm", "--pull=never", "--network", "none",
            *env, image_id, "python", "-c", snippet)
        stdout = (out or b"").decode(errors="replace").strip()
        stderr = (err or b"").decode(errors="replace").strip()
        if code:
            return {"ok": False, "detail": "surface probe failed: "
                    + (stderr or stdout)[-512:]}
        try:
            payload = json.loads(stdout.splitlines()[-1])
        except (ValueError, IndexError):
            return {"ok": False, "detail": "surface probe was unreadable: "
                    + stdout[-512:]}
        tools = [str(t) for t in (payload.get("tools") or [])]
        if payload.get("ok"):
            return {"ok": True, "tools": tools, "detail": ""}
        note = ("; connected: " + ", ".join(connected) if connected
                else "; nothing is connected")
        return {"ok": False, "tools": tools,
                "detail": "the serving build does not expose " + repr(tool_name)
                          + note + "; it exposes: "
                          + (", ".join(tools) if tools else "nothing")}

    async def remove(self, image_id):
        """Delete a bundle image and any container still made from it."""
        containers = (await self._checked(
            "ps", "--all", "--quiet", "--no-trunc", "--filter", "ancestor=" + image_id,
        )).decode().split()
        if containers:
            await self._docker.run("rm", "--force", *containers)
        code, _, err = await self._docker.run("rmi", "--force", image_id)
        if code and b"No such image" not in err:
            raise IntegrityError("Docker could not remove bundle image: "
                                 + err.decode(errors="replace")[:512])

    async def sweep(self, keep):
        """Remove every bundle image except ``keep``: B never outlives its run."""
        images = (await self._checked(
            "images", "--all", "--quiet", "--no-trunc", "--filter",
            "label=recollect.bundle",
        )).decode().split()
        removed = []
        for image_id in dict.fromkeys(images):
            if image_id != keep and re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                await self.remove(image_id)
                removed.append(image_id)
        return removed


@dataclass(frozen=True)
class VerifiedImage:
    """An image whose served bundle bytes were read back and matched."""

    bundle: SubagentBundle
    image_id: str


def bundle_skills(bundle):
    return Snapshot(tuple(File(f.path[len("skills/"):], f.content)
                          for f in bundle.candidate.files
                          if f.path.startswith("skills/")))


def materialize_skills(bundle, destination):
    """Write a bundle's skills tree for its read-only config mount."""
    skills = bundle_skills(bundle)
    if not skills.files:
        raise IntegrityError("Deployment bundle has no skills tree")
    return write_tree(destination, skills)


@dataclass(eq=False)
class Deployment:
    role: str
    verified: VerifiedImage
    manager: object
    #: Host directories this deployment owns and leaves behind when discarded.
    paths: tuple = ()

    @property
    def image_id(self):
        return self.verified.image_id


class Deployments:
    """The coordinator's routing: bind, link and select a sandbox manager.

    New work binds to A. A resumed request is linked to the staged B. Work bound
    to a replaced or discarded deployment runs on the current A.
    """

    def __init__(self, a):
        self.a, self.b = a, None
        self._tasks = {}

    def stage_b(self, deployment):
        if self.b is not None:
            raise IntegrityError("A B deployment is already staged")
        self.b = deployment

    def is_bound(self, task_id):
        return task_id in self._tasks

    def bind(self, task_id):
        if task_id in self._tasks:
            raise IntegrityError("Task is already bound to a deployment")
        self._tasks[task_id] = self.a
        return self.a

    def link_continuation(self, task_id, parent_task_id):
        if self.b is None or task_id in self._tasks:
            raise IntegrityError("Link a continuation only to a staged B")
        self._tasks[task_id] = self.b

    def manager_for(self, task_id):
        deployment = self._tasks.get(task_id)
        if deployment is None or deployment not in (self.a, self.b):
            deployment = self._tasks[task_id] = self.a
        return deployment.manager

    def promote(self):
        """B becomes A; the replaced A is returned for retirement."""
        if self.b is None:
            raise IntegrityError("No B deployment to promote")
        old, self.a, self.b = self.a, self.b, None
        self.a.role = "A"
        return old

    def discard_b(self):
        old, self.b = self.b, None
        return old
