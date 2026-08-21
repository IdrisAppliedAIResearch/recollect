# OpenCode sandbox image

Build the pinned image from the repository root:

```powershell
docker build -f deploy/opencode-sandbox/Dockerfile `
  -t recollect-opencode-sandbox:1.18.18 .
```

The OpenCode backend fails closed when the configured image or container
runtime is unavailable. It has no host-process fallback. At runtime the
manager adds a read-only root filesystem, a non-root user, dropped Linux
capabilities, `no-new-privileges`, PID/CPU/memory limits, private IPC, two
bounded tmpfs mounts, and exactly two bind mounts:

- `/config` is the generated configuration, read-only.
- `/workspace` is an otherwise empty scratch directory, read-write and
  erased before and after every delegation.

The repository, user home, Recollect data, model files, credentials files,
and container-runtime socket are never mounted. The llama.cpp model remains
on the host and is reached through `host.docker.internal`; the model process
and the isolated OpenCode server can therefore stay warm while every
delegation receives fresh conversation and scratch state.

OpenCode stdout/stderr is discarded rather than written to the host because
provider errors may contain delegated text. This boundary protects against
model- or tool-directed filesystem/process access; like any container, it
still relies on the host container runtime and kernel being trustworthy.
