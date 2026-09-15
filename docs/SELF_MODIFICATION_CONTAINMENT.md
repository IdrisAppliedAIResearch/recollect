# Offline containment slice

Implementation work log, not an amendment or a target trial. Registered hashes
remain unchanged. The fixture runner has no model, credentials, calendar access,
or network. It must not be exposed directly as an agent-facing execution API.

## Boundary being implemented

A disposable Linux container holds a minimal trusted watchdog as namespace PID 1,
a separate trusted supervisor child, and an unprivileged fixture worker. The host
recorder is outside the container.
The supervisor receives only a frozen byte snapshot, policy and command, never
the repository, home, archive, Docker socket, or credentials. Its input mount and
root filesystem are read-only; writable work and temporary paths are bounded
tmpfs. The image is selected by immutable local image ID, not a moving tag.

The host must attest effective Docker settings before releasing the worker.
No extra container flags, host namespaces, network, devices, or writable host
mounts are accepted. CPU, memory/no-swap, PID, file-size and output limits bound
the disposable environment. Root supervisor capabilities are limited to assigning
file ownership, dropping worker credentials, and killing worker processes.
The worker must have empty effective/permitted/inheritable/ambient capabilities,
no supplementary groups, no-new-privileges, and the default seccomp filter.

Protected source files and directories are root-owned. Only explicitly allowed
existing files and entirely new creation subtrees become worker-writable. The
initial profile rejects deletion grants, creation subtrees containing baseline
files, and overlapping creation roots. It does not silently broaden these policies.
Exact-file modification requires in-place writes; replacement/rename-based editor
saves are intentionally unsupported. No agent-produced restoration is trusted.

The supervisor has an independent root watchdog. It terminates
the complete namespace workload, including detached descendants, before capturing
files. Worker output cannot access the supervisor's result channel. Missing,
oversized, linked, special, or inaccessible source entries fail capture; available
evidence and explicit failure metadata must remain distinguishable from a complete
snapshot. The host must durably archive evidence before claiming a usable result.

Under [amendment 02](SELF_MODIFICATION_AMENDMENT_02.md), active agent startup and
execution have explicit null deadlines. PID 1 retains stdin only to observe
hangup/error without consuming release bytes, independently of the supervisor.
A disconnect that Docker does not propagate cannot be inferred from elapsed time;
there is no orphan-age guarantee while an attachment appears open. Explicit host
cancellation still reconciles and terminates the owned container independently.

A private root-only control pipe announces release and then trusted settling
after worker completion/failure/cancellation. Only settling or observed supervisor
exit arms the three-second cleanup/capture/report/final-drain watchdog. Success
requires complete framing, settling, control EOF, clean supervisor exit and no
namespace peers. PID 1 does no traversal, capture or reporting; it keeps polling
without a working deadline. Low-level finite laboratory profiles retain their
old startup/execution timers for fault qualification, not actual agent use.
These mechanisms depend on a functioning scheduler/runtime/kernel and do not
protect against a privileged host operator pausing PID 1 itself.
Modify files remain root-owned with worker-group write permission, so the worker
cannot chmod them. Unreadable new entries fail capture; no permission repair is
performed. Image healthchecks and declared volumes are disabled/rejected, and
worker writes to any runtime `/dev/shm` directory are denied. Standard private
device nodes such as `/dev/null` are not source-code storage or host mounts.
The trusted supervisor uses Python `-I -S -u -B`, never imports from the work tree, and
hashes its loaded input for a pre-release identity receipt. Host input ownership
and byte verification are required; a read-only container mount is not a claim
that another privileged host process cannot modify the source.

Refresh is not an automatic retry: a worker/controller failure remains terminal
for that primary attempt. A future privileged grant layer must confirm termination,
seal failure accounting and authorize a separate replacement attempt with fresh
identity and baseline. This slice cannot turn a failed run into experiment PASS.

## Required acceptance cases

- Allowed file writes and new subtree creation succeed; protected writes,
  chmod, unlink, rename and parent-directory replacement fail mechanically.
- Worker cannot signal PID 1, inspect its private descriptors, gain root, access
  a Docker socket or host files, or reach the network.
- Explicit cancellation/disconnect, output floods, detached descendants and
  supervisor death cannot produce a usable receipt or leave authorized work.
  A silent running worker is not terminated merely for taking time.
- Wrong image/identity/flags, extra mounts, bad or duplicate release, expired
  deadline and ambiguous termination cannot yield a usable snapshot.
- Capture preserves exact bytes only after quiescence; symlinks, hard links,
  special files, mode changes and resource overflows are rejected, not repaired.
- Real Docker tests are opt-in and must be reported separately from fake-runtime
  unit tests. Neither is a physical resource-exhaustion or kernel-escape proof.

## Platform assumptions

The trusted computing base includes Docker, its Linux kernel, the pinned Python
image, host-side admission/recording and the supervisor. Container root is not
claimed to be a kernel escape boundary by itself. No host-process fallback exists.
Namespace PID 1 death kills the remaining namespace processes under the
[Linux PID namespace contract](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html).
Capability transitions follow the
[Linux capability contract](https://man7.org/linux/man-pages/man7/capabilities.7.html).
The fixed resource/mount profile uses the documented
[Docker runtime controls](https://docs.docker.com/engine/containers/run/).
Runtime/image qualification and measured watchdog behavior remain required before
CP0; settings inspection alone is not proof of effective kernel enforcement.

## Current integration boundary

The production host executor, privileged single-use grants, controller/development
wiring and replacement-attempt admission are not implemented here. The Docker
lifecycle in the opt-in test module is a qualification harness, not an agent API.
The host must require complete output, successful supervisor/container exit, actual
namespace termination and durable archive readback; a JSON report alone is not
sufficient. The parsing functions do not themselves consume a grant or stop replay.
Never use them as deployment or continuation authority.

Real tests opt in with `RECOLLECT_RUN_SELFMOD_DOCKER_TESTS=1` and reuse the installed
`recollect-opencode-sandbox:1.18.18` image after resolving its immutable image ID.
The create command explicitly forbids pulls. No model or network is involved.
The profile requires `runc`, OOM killing enabled, private bind propagation, and
the frozen proc/sys masks/read-only paths. CPU-specific thermal-throttle masks are
the only permitted additional mask paths. These requirements may reject an older
or differently configured engine; do not weaken them merely to make a test pass.

## Qualification status

Real Docker qualification was attempted on 2026-09-12 against the user's already
running stack. Docker was reachable directly from the agent; no startup or repair
was performed. Engine 29.7.2 reported Linux kernel `7.0.12-linuxkit`; the installed
image resolved to
`sha256:a6f4bc8e8d17018436caeba85e4ac13e26605c504cc67c378ab81fe8a35e8bae`.

The selfmod suite collected 13 tests and stopped at its first failure (`-x`):
`1 failed in 3.26s`; the other 12 were not executed. Initial stopped-container
attestation passed, but running-container attestation failed before worker release
with `Container resource/namespace policy mismatch`. The archived cleanup
inspection's only mismatch among these resource-policy fields was
`OomKillDisable: null`, where the guard requires `false`. This does not establish
that effective OOM enforcement is disabled or that the containment profile is
qualified. Verify the effective kernel controls before proposing any compatibility
change; the guard and tests were left unchanged.

Separately, all three existing sandbox Docker tests passed in 13.12s: lifecycle
and escape barriers, restart after container termination, and pinned OpenCode skill
discovery. Those results do not qualify the distinct selfmod worker profile or
the production fixture adapter's daemon-race/cancellation behavior.

Test-owned containers and temporary shared inputs were removed by their test
fixtures; the post-test container inventory was empty. Recollect and Qwen remained
healthy on their original PIDs (640 and 17688), with the research sentinel matching.
No generation, Calendar task, runtime configuration change, or guard relaxation
occurred. JUnit reports and retained in-repository evidence are under
`.agent/docker-live-*`. Earlier startup failures remain undiagnosed; the successful
connection in this session does not establish their cause.

The ordinary regression run (Docker opt-ins unset) separately reported
`1598 passed, 17 skipped, 2 warnings in 162.79s (0:02:42)`; Ruff reported
`All checks passed!`. Its skipped Docker cases do not erase the live failure above.

### OOM compatibility correction

The authorized follow-up changes only the interpretation of the explicit Docker
`OomKillDisable` field: exact `false` or JSON `null` are accepted; `true`, missing
fields, numeric lookalikes and other malformed values are rejected. Creation still
explicitly sends `--oom-kill-disable=false`; memory, swap, CPU, PID, mount and
capability requirements are unchanged. Docker documents that this flag is
[discarded on cgroup v2](https://docs.docker.com/engine/containers/runmetrics/).
An unset flag is not permission to disable OOM killing.

The new cgroup-v2 qualification probe independently reads `memory.max=268435456`
and `memory.swap.max=0`, verifies the worker cannot open those controls for writing,
and exceeds the container's aggregate limit using two bounded allocation children.
It requires SIGKILL plus increases in `memory.events` counters `max`, `oom` and
`oom_kill`, distinguishing local memory-limit pressure from an unattributed kill.
These counters and the hard-limit behavior follow the
[kernel memory-controller contract](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files).
Docker marks this deliberate OOM run `OOMKilled=true`, even when the supervising
probe exits normally. The normal result gate must reject it; the test inspects
archived failure evidence instead of treating it as an acceptable candidate.

Reaching the previously blocked watchdog test exposed a test-only PID assumption:
the supervisor need not be namespace PID 2. Fault injection now locates the unique
direct PID1 child with the exact supervisor entrypoint and root UID, then polls
briefly for its stopped state after SIGSTOP. The six-second observation deadline
includes injection and polling; the actual watchdog budget and required exit 124
remain unchanged. No supervisor or production execution behavior was modified.

Final verification on 2026-09-12 enabled both Docker opt-ins for the full suite:
`1627 passed, 1 skipped, 2 warnings in 204.60s (0:03:24)`; Ruff reported
`All checks passed!`. All 14 selfmod containment cases (including the new OOM
case) and all three existing sandbox Docker cases passed. Twelve new unit cases
cover exact nullable/boolean interpretation and rejection of missing/malformed
OOM fields. Independent review cleared the guard, OOM attribution and corrected
watchdog fault injection. The final report is `.agent/oom-fix-full.xml`; supporting
test archives are retained under `.agent/oom-fix-*`, including earlier failed
probes rather than replacing them with the successful result.

All test-owned containers and external shared input directories were cleaned up.
Recollect/Qwen remained healthy on PIDs 640/17688 with the matching research
sentinel; no service restart, image rebuild, Calendar call or model generation
was performed. All three registration and both planning-capture hashes are
unchanged. These cases qualify their stated behavior on this engine/image; they
do not complete production-adapter daemon-race/cancellation qualification,
physical power-loss testing or the live self-modification experiment.
