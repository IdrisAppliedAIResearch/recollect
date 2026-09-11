# OpenCode sandbox image

Docker is a production dependency for the OpenCode backend. Use Docker Engine
on a Linux edge device; Docker Desktop is sufficient for Windows development.
The multi-stage image keeps npm and Node out of the runtime image and copies
only the pinned OpenCode executable. The locked image built on 2026-08-21 was
133,422,172 bytes, versus 395,016,295 bytes for the original single-stage
build.

Build the pinned image from the repository root:

```powershell
docker build -f deploy/opencode-sandbox/Dockerfile `
  -t recollect-opencode-sandbox:1.18.18 .
```

On Docker VMM for Windows, bind mounts are not shared automatically. In Docker
Desktop, add only `%LOCALAPPDATA%\recollect\sandboxes` under **Settings >
Resources > File sharing**. Do not share the repository, home directory, or
entire drive. Linux Docker Engine needs no equivalent Desktop setting.

Set `RECOLLECT_SUBAGENT_BACKEND=opencode` after building the image. Recollect
keeps one global OpenCode container warm, not one per chat. Calls queue behind
one model lease; each receives a new OpenCode conversation and a scrubbed
workspace. Native OpenCode subagents and compaction remain available within
that lease, matching a one-slot llama.cpp server.

The OpenCode backend fails closed when the configured image or container
runtime is unavailable. It has no host-process fallback. At runtime the
manager adds a read-only root filesystem, a non-root user, dropped Linux
capabilities, `no-new-privileges`, PID/CPU/memory/file-descriptor limits,
no swap, private IPC, two bounded tmpfs mounts, and exactly two bind mounts:

- `/config` is the generated configuration and bundled Recollect skills, read-only.
- `/workspace` is an otherwise empty scratch directory, read-write and
  erased before and after every delegation.

Continuous tasks use OpenCode's native skill discovery: only the names and
descriptions are exposed until the worker loads a skill. `recollect-reporting`
describes revision acknowledgments and evidence reports; `recollect-files` applies
only when the user requests a saved TXT, Markdown, CSV, or JSON file.
`recollect-research` covers source identity, evidence checks, and adapting when
article extraction omits factual cards. Research
returns its findings in conversation by default. Both native agents may load
these three skills; other skills remain denied. Skill changes are copied on the
next sandbox startup and do not require rebuilding the image.
The image must include the checksum-pinned ripgrep 15.1.0 executable: the native
skill tool enumerates supporting files with it. Rebuild older images that lack
this dependency. Downloading it at runtime is incompatible with the noexec scratch
mounts; the executable belongs in the read-only image instead.

Workspace checkpointing preserves supported files for recovery. Only paths
explicitly reported as deliverables are copied to Downloads and appended to
completion replies. The main agent receives the reported answer, findings, and
selected sources so it can explain the result aloud, including when the user
also requested a file. Progress updates remain brief; substantive findings and
completion replies have a larger synthesis budget.

The web-fetch tool supports a `page` view to retain factual cards that article
extraction can discard. Native checkpoints compare returned research content;
changing output limits or reloading a skill does not count as new evidence.

Docker Desktop or Docker Engine must be running before a research request.
Recollect checks that the engine responds and runs Linux containers before
starting the subagent; an unavailable engine produces an actionable research
error. On Windows, start Docker Desktop and retry once its engine is ready.
The generated research-tool command uses `/usr/local/bin/python` inside the
pinned Linux image, independently of the Windows interpreter running Recollect.
Neither check changes the container boundary or starts a host OpenCode process.

The repository, user home, Recollect data, model files, credentials files,
and container-runtime socket are never mounted. The llama.cpp model remains
on the host and is reached through `host.docker.internal`; the model process
and the isolated OpenCode server can therefore stay warm while every
delegation receives fresh conversation and scratch state.

OpenCode stdout/stderr is discarded rather than written to the host because
provider errors may contain delegated text. This boundary protects against
model- or tool-directed filesystem/process access; like any container, it
still relies on the host container runtime and kernel being trustworthy. The
bridge network and `host.docker.internal` route are intentional: research needs
internet access and OpenCode needs the host model. They are not a network
air-gap.

The default container limit is 1 GiB. A live focused delegation sampled
801.3-910.4 MiB, so validate a representative workload before lowering it.
The global singleton is the primary memory saving. Docker Desktop's VM was
limited to 4 CPUs and 4 GiB for development; use Docker Engine directly on an
edge Linux deployment to avoid Desktop's VM/UI overhead.

Run the real lifecycle and escape-barrier checks explicitly:

```powershell
$env:RECOLLECT_RUN_DOCKER_TESTS = "1"
uv run pytest tests/test_sandbox_docker_e2e.py -v
```

These tests are skipped by the normal unit suite because they require the
locally built image. They verify warm reuse, fresh session/scratch state,
effective resource controls, non-root/capability/seccomp policy, read-only
paths, absence of the Docker socket, and recovery after a forced container
kill.
