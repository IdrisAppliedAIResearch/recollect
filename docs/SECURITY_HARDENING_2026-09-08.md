# Security hardening record — 2026-09-08

This records the implementation following the
[original security review](SECURITY_REVIEW_2026-09-08.md). The working tree includes
both the preceding deployment implementation and these fixes; it is not a
published release.

The ordinary commands remain `recollect`, `recollect-host`, and
`recollect-deploy`. The browser UI, saved history, streaming chat, voice controls,
wake phrase, speech timing, model placement, and strict retrieval verification
keep their existing behavior. The Surface still streams audio to desktop Whisper.
No inference-quality or hardware-performance claim follows from these tests.

## Implemented controls

| Finding | Change | Verification boundary |
|---|---|---|
| F01: Windows command lookup | Isolated host Python bootstrap and absolute PowerShell path obtained from Windows | Hostile working-directory fixtures, literal argument forwarding, installer ownership checks |
| F02: standalone browser boundary | Loopback-only standalone/client binds; shared Host/Origin validation before HTTP/WS admission; explicit Vite development origins | Foreign origins, misleading hosts, duplicate headers and cross-site requests rejected; ordinary local requests accepted |
| F03: session traversal | Validate identifiers and resolved storage paths before access; verify metadata identity; unknown/empty episode reads cannot create stores | Windows traversal, glob/device names, redirected file resolution, malformed metadata and ordinary history/episode lifecycle |
| F04: clear LAN transport | Host launcher provisions a private certificate/key and copied token/public-certificate bundle; client pins TLS for HTTP and WS | Actual temporary TLS servers exercise startup, HTTP relay and voice PCM; wrong certificate/name rejected. Firewall narrowing remains outstanding |
| F05: research SSRF | Resolve and validate all addresses, connect to the selected vetted literal IP, retain original HTTP Host and TLS verification name, revalidate redirects | DNS rebinding, mixed public/private answers, special ranges, redirect hops, TLS identity forwarding and environment proxy bypass |
| F06: resource pressure | Upload/field limits, bounded HTTP/WS admission, async speech admission, inactive-lock reclamation, research event backpressure and bounded identity-encoded web responses | Oversized/chunked/slow uploads stop before route effects; overload/cancellation recovery; speech workers leave capacity for memory operations; hostile response and queue fixtures |
| F07: native embedder concurrency | Serialize model initialization, native inference and cache publication; keep per-call trace metrics local to the calling worker | Instrumented fake native model and existing strict shadow suite; real GPU/CPU runtime identity is checked during launch |
| F08: pairing-file permissions | Private creation before writing, ownership/link checks, existing-file permission repair and atomic legacy-token migration | Real Windows ACL checks before first write and during reuse; actual Ubuntu permission validation remains pending |

The sandbox verifier also rejects added capabilities, false/conflicting
no-new-privileges settings and drifted memory limits. A restrictive `.dockerignore`
admits only the sandbox's required build inputs. The Ubuntu client now uses a
generated transitive lock with `uv run --locked --script`; it still installs no
models, research library or desktop GPU dependencies.

## Pairing and compatibility

The Windows host launcher creates `var/deployment-token.txt` and adjacent
`.tls.crt`/`.tls.key` files using owner-only permissions. The retained token filename
now holds a JSON pairing bundle. Only that bundle is copied to the Surface; the
private key stays on Windows. An existing plain token is migrated without changing
the secret. An already paired Surface needs the upgraded bundle copied once.
The next launch reads it from the saved location and promotes an existing saved
HTTP desktop URL to HTTPS automatically. See [deployment setup](DEPLOYMENT.md).

HTTPS/WSS certificate verification uses the fixed name `recollect-desktop`,
regardless of the LAN routing address. The copied certificate is the trust anchor;
the browser needs no certificate exception. Normal launch checks reuse the
existing certificate/key and repair permissions. Identity mismatch, partial TLS
material, or expiration fails visibly rather than silently replacing client trust.

Legacy token files remain supported with system-trusted HTTPS. An explicit
`--allow-http-loopback` option supports a separately configured encrypted tunnel
whose local endpoint is loopback. Clear LAN HTTP is refused.

The new ingress limits are 2 MiB per HTTP body, 30 seconds to upload, 1,048,576
characters per chat message, 512 per title and 128 safe ASCII characters per
session/turn ID. Each process allows 64 active HTTP requests and 8 WebSockets;
the existing single-listener voice rule remains. Uvicorn's incoming WS frame
limit is 64 KiB with one queued frame and compression disabled. Excess requests
receive an error rather than creating unbounded queued work.

Research requests ask for identity encoding and reject servers that nevertheless
send compressed bodies before HTTPX decodes them. This closes the demonstrated
decompression allocation issue; such servers can now return a tool error. Public
web responses have a 2 MB byte cap. The transport does not reuse TLS connections
across hosts sharing a vetted IP. Research event queues hold at most 32 items and
apply backpressure. Speech waiters consume async admission slots instead of native
worker threads; disconnected waiters cannot start synthesis, and cancellation
does not release a slot while its native call is still running.

## Remaining operational boundaries

- **Firewall and actual devices:** no firewall rules were changed. The audit
  observed broad Public-profile rules for the shared Python executable. Scoping
  Recollect to the Surface requires its actual address/interface and review of
  those existing rules so other applications are not disrupted. TLS prevents
  cleartext interception; it does not make the listening port unreachable to
  other devices. No Surface connection or Ubuntu hardware validation was performed.
- **Single-owner authority:** the token still authorizes the whole application.
  Other local programs on the Surface can use its local proxy. No login prompts,
  per-device permissions, independent revocation or multi-user isolation were
  added. Use a trusted account/device for this workflow.
- **Retention and storage:** no conversations, keys, model files or environment
  were rewritten. Encryption at rest, backups, retention and cumulative storage
  quotas remain operational decisions. Request bounds do not limit total data
  accumulated over repeated authorized requests. Multiple serving processes must
  not concurrently write the same store.
- **Research and supply chain:** research can send delegated queries externally
  and consume untrusted content. Container restrictions do not eliminate prompt
  injection. Full history secret scanning, native/model parser audits, image OS
  scanning, immutable CI action pins and additional Windows CI remain outstanding.
  The prior DiskCache advisory remains tracked: Recollect does not enable its
  vulnerable disk-cache path, and the pinned native environment was preserved.
- **Live readiness:** production Recollect, Qwen and Docker were not started for
  this hardening task. Cold launch, the actual Surface microphone/speakers,
  network loss/reconnect, GPU concurrency and the opt-in Docker boundary tests
  still need a live deployment check. The next desktop launch must rebuild the
  sandbox image because its research source inputs changed; the launcher detects
  those stale inputs.

## Validation

Final required checks used the existing environment, with a task-owned temporary
pytest base directory that was removed on exit:

```text
uv run --no-sync ruff check .
All checks passed!

uv run --no-sync pytest --basetemp <task-owned temporary directory>
786 passed, 3 skipped, 1 warning in 52.86s
```

The skips are the two opt-in Docker tests and one Windows symlink-creation test
that needs unavailable privileges. Existing Starlette TestClient/httpx
deprecation accounts for the warning. No original test was deleted, rewritten,
skipped or weakened to obtain this result.

```text
npm test
# tests 106
# pass 106
# fail 0
# skipped 0
```

The client lock check resolved 18 packages without changing the desktop
environment. `git diff --check` was clean. UI source and the sibling `episodic`
library were not edited; no UI build or native environment synchronization was
needed. Ubuntu command scripts were exercised through Bash fixtures on Windows,
which does not substitute for running them on the Surface.

The real Windows command wrappers were reinstalled and checked from a temporary
directory containing a hostile dummy `recollect` package and `powershell.exe`:

```text
recollect.cmd: installed help succeeds from hostile caller directory
recollect-host.cmd: installed help succeeds from hostile caller directory
PowerShell resolves to: C:\Windows\system32\WindowsPowerShell\v1.0\powershell.exe
Installed-command test fixtures cleaned.
```

Application/model ports 8080 and 8000 had no listeners at the final inspection.
Production services remained stopped. All new synthetic fixtures were cleaned.

The prior audit's two retained synthetic-fixture directories are documented in
the original report. Automatic approval review rejected their deletion twice;
this task does not retry that operation.

### Authorized Docker validation attempt

Later on 2026-09-08, Docker startup and live testing were explicitly authorized.
The engine could not start, so neither live Docker test executed and the image
could not be inspected or rebuilt. The first startup failed accessing
`%LOCALAPPDATA%\Docker\vm-data\00000002.000007cf`; the backend reported failure
resetting its socket forwarder. Normal shutdown timed out, and Docker's supported
forced stop succeeded. A reversible attempt to rename that zero-byte reparse
entry also failed with Windows error 1920; no backup or file change occurred.

One clean restart then failed initializing its ingest listener at
`%LOCALAPPDATA%\Docker\run\sailor-ingest.sock`, again reporting “The file cannot
be accessed by the system.” Further recovery was paused under AGENTS.md's
two-failure rule. Docker Desktop remained in its startup-error state; Recollect
and models were not launched. No factory reset, image deletion, Docker data
deletion, or application-code change was performed.

The regular checks were rerun while diagnosing startup:

```text
All checks passed!
786 passed, 3 skipped, 1 warning in 53.45s
```

The Docker skips remain unresolved, rather than being reported as successful
live tests. This test run's temporary directory was cleaned.

The Docker dialog was subsequently inspected and dismissed through the desktop
UI at the user's request. It showed the same socket-access failure, with no
sign-in or Continue option. Dismissing it closed the Docker window and did not
establish engine readiness; live validation remains outstanding.

### Subsequent successful Docker and desktop validation

After Docker became available during the user's next launch, both opt-in Docker
lifecycle/containment tests passed. This supersedes the engine blocker above.
The sandbox image was rebuilt with the current restricted build context.

That launch exposed two Windows startup assumptions: this llama-server advertises
its exact model path rather than the filename, and a fresh terminal does not
include its CUDA 13 libraries. The launcher now accepts only the configured name
or exact path, scopes the existing CUDA library directory to Qwen's child process,
and checks CUDA device discovery before starting it. Native verbosity 4 provides
the GPU-offload evidence required by this build; the readiness check remains.
Regression tests cover incorrect identities, PATH restoration, startup failure,
and missing CUDA dependencies.

The complete standalone launcher subsequently passed: Docker/image ready, Qwen
GPU offload verified, the pinned CPU embedding sentinel matched, Whisper warmed
on CUDA float16, and Kokoro used CUDAExecutionProvider. The browser displayed the
existing saved session with 24 turns. No conversation or captured audio was added.
Services were left running. Actual Surface installation/audio, LAN behavior and
firewall scoping remain unverified.

Final follow-up checks (`RECOLLECT_RUN_DOCKER_TESTS=1`):

```text
All checks passed!
791 passed, 1 skipped, 1 warning in 68.58s (0:01:08)
```

Only the Windows symlink-creation privilege test was skipped; the warning remains
the existing Starlette TestClient/httpx deprecation. The task-owned pytest directory
was removed on exit.
