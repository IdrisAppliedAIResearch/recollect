# Recollect security and deployment posture review

This is the original, pre-hardening audit snapshot. The subsequent authorized
implementation and remaining deployment gaps are tracked in
[the hardening record](SECURITY_HARDENING_2026-09-08.md). Evidence and line references
below describe the version reviewed, not the current hardened working tree.

Reviewed 2026-09-08 against the working tree based on `9300ea0`, including the
uncommitted three-mode deployment and short-command implementation.

The split has useful authentication, proxy, and container controls, but I would
resolve the four high-priority findings before normal deployment with sensitive
conversations. The most consequential issues are unsafe Windows command lookup,
standalone browser access, session path traversal, and unencrypted pairing over a
host interface whose current firewall rules are broader than intended.

This was an analysis task. Application code, installed commands, dependencies,
firewall rules, tokens, conversations, and model assets were not changed. Docker,
Qwen, and Recollect were not started. Findings below distinguish reproductions
from configuration observations and risks that still need live validation.

## Scope and trust boundaries

| Boundary | Current behavior | Security implication |
|---|---|---|
| Windows `recollect` | Entire stack, UI/API on loopback | Loopback limits network access, but needs application-level browser protection |
| Windows `recollect-host` | Authenticated backend; default `0.0.0.0:8080` | Every reachable interface can accept connection attempts; transport and firewall matter |
| Ubuntu `recollect-deploy` | Loopback UI/proxy; reads saved desktop address and token file | Browser receives no pairing token; local programs can use the proxy's authority |
| Surface to desktop | HTTP/WS or HTTPS/WSS to a fixed origin | Documented HTTP setup exposes token, text, audio, and responses to network interception |
| Desktop models and memory | Qwen on loopback; CPU embeddings and speech inside Recollect | Keeps the researched embedding identity and avoids exposing individual model APIs to the Surface |
| Research | OpenCode container and external research tools | Host filesystem containment is substantial; outbound networking and untrusted research remain separate concerns |
| Persistence | Desktop SQLite stores, JSON traces, session metadata and logs | Sensitive content remains on disk; pairing does not provide encryption at rest or per-user separation |

The intended trust model is one owner controlling both devices. It currently has
no per-user or per-device authorization, independent revocation, or tenant
isolation. The OpenAI-compatible `user` field selects memory by name; it is not
an authenticated identity. Possession of the shared token grants access to the
host's application API and all its saved sessions.

## Findings in remediation order

### F01 — High: Windows commands can execute code from the caller's directory

Evidence: [install-commands.ps1](../scripts/install-commands.ps1), line 51;
[launch.py](../src/recollect/launch.py), lines 42 and 54.

The installed `recollect-host` wrapper runs the exact virtual-environment Python
with `-m recollect.launch`. Python searches the current directory before the
installed package. Separately, both desktop commands locate PowerShell with
`shutil.which("powershell.exe")`, which searched the current directory in the
actual Windows environment reviewed here.

Two safe temporary-directory probes confirmed the problem:

- A harmless local `recollect/launch.py` executed when invoking the host command
  with `--help`, instead of the installed application module.
- A dummy `powershell.exe` in the caller's directory was selected by the real
  launcher. Process execution was mocked for this probe.

An attacker needs to supply files in a directory from which the user runs a
trusted launch command, such as a downloaded project. Execution has the user's
Windows permissions. This is a local code-execution issue, not a demonstrated
remote API exploit.

Use isolated Python startup (`-I -m recollect.launch`) and a verified absolute
PowerShell location that excludes caller-controlled directories. The isolated
Python invocation displayed the genuine host help in the hostile-directory
fixture. Python documents both the current-directory behavior and isolated
mode in its [command-line reference](https://docs.python.org/3/using/cmdline.html).

### F02 — High: standalone HTTP and WebSocket access lacks a browser boundary

Evidence: [api.py](../src/recollect/api.py), line 227;
[deployment.py](../src/recollect/deployment.py), line 154;
[voice_api.py](../src/recollect/voice_api.py), line 79.

Standalone allows all CORS origins, methods, and headers and has neither host
authentication nor the client proxy's trusted Host/Origin middleware. The voice
origin check accepts a matching, caller-supplied Host without establishing that
the host is trusted.

Actual routes with synthetic state returned:

```text
GET /api/sessions/synthetic/history; Origin: https://attacker.invalid
200; Access-Control-Allow-Origin: *; synthetic private history returned

OPTIONS /api/chat; Origin: https://attacker.invalid; requested method: POST
200; POST allowed

Voice socket with Host attacker.invalid:8080 and matching Origin
synthetic listener reached waiting state
```

A malicious webpage needs browser access to the local service; browser local
network restrictions can affect exploitability. The WebSocket case additionally
requires suitable routing or DNS rebinding. Those browser/network prerequisites
were not exercised on real devices. The server-side acceptance is confirmed.

Apply trusted Host and same-origin checks to standalone HTTP and WebSocket
requests, with a narrow explicit development exception if required. Also reject
non-loopback standalone binds in `DeploymentConfig`: the Windows launcher has
this guard, but direct `serve --mode standalone --host 0.0.0.0` bypasses it.
Equivalent unauthenticated host-mode requests were correctly rejected with 401.

### F03 — High: session identifiers can escape the storage directory

Evidence: [config.py](../src/recollect/config.py), line 251;
[session.py](../src/recollect/session.py), lines 113, 128 and 189;
[api.py](../src/recollect/api.py), line 283.

`session_dir()` joins an unvalidated identifier to the configured storage root.
On the Windows host, encoded backslashes survive a single URL path parameter and
are filesystem separators. JSON chat requests also accept arbitrary session IDs.
The episode GET route opens or creates a store before checking that the session
exists.

In a temporary directory, a session ID of `..\..\outside` produced:

```json
{
  "traversal_resolves_outside_sessions": true,
  "outside_history_status": 200,
  "get_episode_status": 404,
  "outside_sqlite_created": true
}
```

The outside fixture contained a synthetic session. Even though the episode
request returned 404, it created `episodes.sqlite` outside the permitted root.
This demonstrates escaped storage access and a write side effect on GET. The
readers still expect application schemas and filenames; arbitrary file download,
arbitrary-content overwrite, and code execution were not demonstrated.

Host exploitation requires API authority, including authority obtained through a
compromised token or local client; standalone has the additional exposure in F02.
Validate session and turn IDs against their generated formats, enforce resolved
path containment, and require an existing session before opening its store.
Read-only routes should not create files. Reject malformed identifiers before
creating locks or doing filesystem work.

### F04 — High on an untrusted network: plaintext pairing and broad host exposure

Evidence: [client.py](../src/recollect/client.py), lines 124, 183 and 269;
[deployment.py](../src/recollect/deployment.py), line 57;
[launch.ps1](../scripts/launch.ps1), line 138; read-only Windows posture checks.

The documented HTTP setup sends the reusable bearer token in cleartext, along
with requests and responses. Audio, transcripts, histories, and generated speech
have the same transport exposure. The startup response `{"mode":"host"}` does
not authenticate the server against an active network impersonator when sent
over HTTP. Stub capture confirmed that HTTP requests carry the token; no real
traffic was intercepted.

This desktop currently has:

- Ethernet classified as **Public**; a Tailscale interface classified as Private.
- Windows firewall enabled for all profiles.
- Existing Public-profile inbound TCP and UDP rules for the exact Python 3.13
  base executable used by the virtual environment, allowing any remote address
  and any local port.
- No listeners on Recollect/Qwen ports 8080/8000 during the review.

The all-interface host default combined with those Python rules is broader than
the documented Surface-only private-network posture. This is configuration
evidence, not proof of internet exposure or a successful remote connection;
router, routing, and other firewall policies were not tested.

Use authenticated TLS or a verified encrypted tunnel between devices, bind to the
intended interface, and scope host inbound access to the Surface. Review the
existing broad Python rules rather than assuming that adding a narrow allow rule
overrides them. Do not expose Qwen separately. No machine settings were changed.

### F05 — Medium: research URL validation does not bind the connection address

Evidence: [webtools.py](../src/recollect/engine/webtools.py), lines 662, 672,
744 and 764; [isolation.py](../src/recollect/engine/sandbox/isolation.py), line 110.

`_blocked_host()` resolves the hostname and checks its addresses, then HTTPX
receives the original hostname and can resolve it again when connecting. An
attacker-controlled DNS answer can change between validation and use. Repeating
the check after a redirect does not close this gap.

A mocked resolver and transport returned a public address to the guard and
loopback at the simulated connection. `web_fetch()` returned 200 with the
synthetic internal response. The guard also accepted shared-address space
(`100.64.0.1`) and multicast addresses. No network connection to a private service
was made.

A hostile page or task must induce a fetch to attacker-controlled DNS. With the
default OpenCode backend the request originates inside Docker, limiting the
reachable targets to that network context; the legacy backend runs from the
desktop. Docker bridge access and `host.docker.internal` deliberately exist, so
filesystem isolation does not establish a public-internet-only egress boundary.

Connect to a vetted public address while preserving the intended HTTP Host and
TLS identity, revalidate every redirect, reject nonpublic/special destinations,
and disable environment proxy routing for clients relying on this guard. Add
network-level egress controls if public-only research is a required security
property. This follows the separation between application checks and network
controls in [OWASP's SSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html).

### F06 — Medium: request size and concurrency admission are insufficient

Evidence: [api.py](../src/recollect/api.py), lines 97, 103, 191 and 262;
[client.py](../src/recollect/client.py), line 105;
[voice_api.py](../src/recollect/voice_api.py), lines 48 and 113;
[voice.py](../src/recollect/engine/voice.py), line 248.

There is no total HTTP request-size limit or bounded admission queue. Chat
messages, titles, and session identifiers have no schema length bounds. Session
creation writes synchronously in an async route, and per-session locks remain in
an unbounded dictionary, including locks for invalid chat IDs. Serializing model
execution does not cap the requests waiting for it.

Bounded synthetic probes confirmed:

- `ChatRequest` accepted 1,048,576-character messages and session IDs.
- POST `/api/sessions` returned 200 and persisted a 1,048,576-character title.
- The client forwarded a 2 MiB upload unchanged.
- With a two-thread test executor and fake Kokoro, two concurrent speech requests
  occupied both workers: one synthesized while the other waited on a threading
  lock. An unrelated `asyncio.to_thread` operation could not run until release.

An authorized client, a local program, or an attacker using F02 can pressure
memory, storage, CPU, and response latency. The worker-starvation mechanism was
reproduced at reduced scale; production exhaustion was not attempted.

Add body and field limits, bounded async admission before scheduling blocking
work, per-device/request quotas, and cleanup for inactive locks. Give speech a
bounded queue or dedicated worker rather than letting waiters occupy the shared
executor. Preserve the existing streaming backpressure and voice packet limits.

Two research paths also need memory bounds: the unlimited event queue in
[runner.py](../src/recollect/engine/sandbox/runner.py), lines 167 and 238,
accumulated all 5,000 stub events while consumption was paused. In
[webtools.py](../src/recollect/engine/webtools.py), lines 785–787, a 3,912-byte gzip
fixture expanded to a 4,000,000-byte chunk before the 2 MB response limit rejected
it. Bound event queues with backpressure and enforce decompression limits before
large decoded allocations. These probes did not exhaust production resources.

### F07 — Medium: concurrent requests can enter the same native embedder

Evidence: [embedder.py](../src/recollect/engine/embedder.py), lines 115–137;
[api.py](../src/recollect/api.py), lines 489–493.

The embedder lock protects cache access, but the shared model's `embed()` call
runs outside that lock. Different sessions can execute retrieval concurrently.
The installed llama-cpp-python embedding implementation mutates the shared batch
and native context during a call.

A two-thread fake-model barrier confirmed **two simultaneous entries into the
same model object**. This proves the missing serialization, not a native crash or
corrupted vector. Real-model concurrency was deliberately not exercised.

Serialize model initialization and each native embedding call without changing
the model, CPU placement, call shape, library, or strict shadow comparison. Verify
concurrent-session behavior with an instrumented fake first, followed by the
existing identity checks during a separately authorized live run. Comparing two
retrieval implementations does not itself protect a native model from concurrent
access.

### F08 — Medium: existing pairing files bypass permission enforcement

Evidence: [launch.ps1](../scripts/launch.ps1), lines 87–109.

The Windows launcher restricts ACLs only when generating a new token. An existing
token is accepted without validating its permissions. A dummy file granting
Authenticated Users read access was accepted with that permission retained.
New-file creation also writes the secret before applying the ACL; an ACL failure
leaves a file that the next run treats as an existing token.

Create the file with private permissions before writing and validate existing
token ownership/permissions. Apply the same requirement during Ubuntu setup;
the current Ubuntu instructions rely on a separate `chmod 600` step for the token.
The saved Ubuntu connection configuration itself is created privately. The real
default desktop token file does not yet exist, so its eventual ACL was not tested.

## Controls that held up

- Host bearer middleware covers HTTP and WebSockets before route execution and
  uses constant-time secret comparison. Missing/duplicate/malformed authorization
  is covered by tests. No host authentication bypass was found in this review.
- Client foreign-origin requests returned 403 with zero upstream requests. The
  upstream origin stayed fixed for double-slash paths, encoded host-looking paths,
  and URL-looking query parameters. Caller credentials and routing headers are
  replaced; hop-by-hop headers are stripped; redirects and environment proxies
  are disabled. HTTPS certificate verification remains enabled.
- The browser receives no pairing credential. The renderer creates React nodes
  rather than injecting HTML and restricts link schemes. No direct model-output
  script injection was found in the inspected rendering paths.
- Voice requires explicit microphone opt-in, has packet/control and utterance
  bounds, allows one listener, and implements disconnect/cancellation cleanup.
  Speech playback uses saved verified completed replies. Microphone capture and
  playback occur on the Surface; captured audio streams to the desktop for model
  processing.
- The research container is non-root with a read-only root, dropped capabilities,
  no-new-privileges, bounded memory/CPU/PIDs/tmpfs, and narrowly defined mounts.
  Repository, memory stores, host home, and Docker socket are not mounted. The
  manager validates the runtime profile, and there is no host OpenCode fallback.
  OpenCode shell/native web tools and plugins are denied in its generated policy.
- Qwen remains loopback-only with one shared chat/research slot. Embeddings remain
  in process on CPU; strict shadow verification and the sibling library were
  unchanged. Launch readiness checks cover the configured sentinel and GPU voice.
- Desktop startup preserves the pinned environment. Ubuntu configuration is
  parsed as NUL-delimited data; wrappers preserve literal arguments. Installer
  ownership checks prevent silent replacement of unrelated commands.

## Data, dependencies, and operations

**Local-device authority.** A local Surface process can call the proxy without an
Origin header or pairing token and receive authenticated host API access. A
synthetic probe confirmed this. The proxy's origin guard protects the browser
boundary; it does not isolate other local users or processes. A stolen unlocked
Surface therefore exposes host conversation authority while the proxy is running.
Use a dedicated trusted account/device; add local authentication and device-scoped
credentials if this trust model expands.

**Persistence and privacy.** Session metadata, complete messages, reasoning, and
retrieval/research traces are retained in application-readable files. The app
does not provide encryption at rest or a defined retention/deletion policy. The
inspected `.env` and `var` ACLs include inherited Modify access for agent/tooling
identities in addition to the owner, SYSTEM, and administrators. Those grants may
be intentional; they mean these files are not exclusively accessible to the owner.
Review access and encrypted backups in the deployment account. BitLocker status
could not be read without additional rights; Ubuntu disk encryption was not
inspected. No conclusion about disk encryption is made.

Local model hosting does not make research offline. Research queries and fetched
URLs reach external services, and URLs can carry sensitive text. Untrusted
research can influence subsequent model/tool choices. Container confinement
limits filesystem/process impact; it does not guarantee resistance to prompt
injection or prevent disclosure of information included in a delegated task.

**Dependency audit.** `npm audit --json` reported zero known vulnerabilities
across 72 dependencies. An isolated `pip-audit` inspection of the installed
desktop environment reported one known vulnerability: DiskCache 5.6.3,
CVE-2025-69872 / GHSA-w8v5-vhqr-4h9v. Its unsafe pickle reads require an attacker
to write a cache that the victim later reads, as described in the
[original disclosure](https://github.com/EthanKim88/ethan-cve-disclosures/blob/main/CVE-2025-69872-DiskCache-Pickle-Deserialization.md).

DiskCache is present through llama-cpp-python. The inspected Recollect embedder
uses an in-memory OrderedDict and does not enable `LlamaDiskCache`; the installed
Llama class defaults to no cache. No active path to the vulnerable disk-cache
read was found. Track the advisory and retain that restriction; do not replace
the pinned native environment merely to clear a scanner result. The scanner
reported no fixed version. It could not audit the local `recollect`,
`episodic-chat`, or `en-core-web-sm` packages. Native DLLs, model parsers, the
standalone Qwen binary, Docker image OS packages, and the actual Surface's
dependency graph were not covered by these package-name/version scans.

**Build and update posture.** There is no `.dockerignore` or Dockerfile-specific
ignore. The root build context unnecessarily makes `.env`, runtime data, and
local environments eligible inputs. Explicit Dockerfile COPY paths do not copy
them into the final image, and BuildKit may transfer inputs selectively; image
inclusion was not demonstrated. Add a narrow build-context policy. The Dockerfile
pins base images and Python build dependencies; the client script pins direct
dependencies but has no transitive script lock. Add that lock for reproducible
fresh Surface installations. Existing CI runs Python and UI checks on Ubuntu;
add Windows launcher coverage and dependency/advisory checks. GitHub Actions use
version tags rather than immutable commit pins. No tracked `.env`, `var` content,
or GGUF files were found in the current index; full historical secret scanning
was not performed.

**Container attestation hardening.** The fixed launch command requests the
restrictive profile described above. Its verifier is less strict:
[isolation.py](../src/recollect/engine/sandbox/isolation.py), lines 179–184,
checks that `CapDrop` contains `ALL` without rejecting `CapAdd`, and looks for the
substring `no-new-privileges` without requiring a true value. Modified inspection
fixtures with added `SYS_ADMIN` or `no-new-privileges:false` were accepted. Treat
this as a lower-priority drift-detection defect: it requires changed launch or
runtime state, and no model-driven container escape was demonstrated. Validate
the exact effective capability/security settings and reject conflicting values.

**Host baseline.** Windows 11 Pro build 26200 was observed. Defender antivirus
and real-time protection were enabled, with a signature timestamp on the review
date. This does not establish OS patch completeness. Tailscale interface presence
does not establish an active, authorized tunnel or its access policy.

**Failure handling.** Startup checks fail visibly, but a later readiness failure
can leave already-started components running; the launch journal records status
and process/log paths. There is no demonstrated service supervisor or automatic
recovery policy. Confirm clean startup failure, restart, token rotation, network
loss, and shutdown behavior before unattended operation. Two independently started
processes must not share and write the same store merely by choosing different
ports; the session locks are process-local.

## Verification and remaining deployment gates

Required checks were run against the reviewed implementation:

```text
uv run --no-sync ruff check .
All checks passed!

uv run --no-sync pytest
675 passed, 2 skipped, 1 warning in 51.21s
```

The two skips are the opt-in Docker end-to-end tests. The warning is the existing
Starlette TestClient/httpx deprecation. The UI suite also ran:

```text
npm test
# tests 106
# pass 106
# fail 0
# skipped 0
```

The separate boundary-focused run passed 146 tests with one warning. Security
reproductions used synthetic ASGI state, stub transports, fake models, and
temporary fixtures. They did not read or alter real conversations, intercept real
traffic, run native-model stress tests, or attempt container escapes. UI source
was not edited; no UI rebuild was required. The initial audit-tool invocation
used an unsupported flag and was corrected before the completed dependency scan.

Before calling the paired deployment ready:

1. Resolve F01–F04, then F05–F08, with focused regression tests for the reproduced
   cases. Reinstall the Windows wrapper after fixing its bootstrap.
2. Establish the encrypted device connection and restricted firewall/interface
   policy. Verify an authorized Surface connects and another device cannot.
3. Validate the actual Ubuntu installation, token permissions, local account
   boundary, OS patching, and storage encryption/backup policy.
4. Run the complete Windows cold launch and actual Surface chat/audio flow. Test
   interruption, slow/lost network, desktop restart, and token replacement.
5. Run the opt-in Docker boundary tests and scan the actual built image. Exercise
   concurrent sessions and speech with bounded load while verifying model identity.

Temporary inline fixtures were cleaned by their context managers. Automatic
approval review rejected deletion of two retained pytest directories with
`blocked by policy`; no further deletion attempts were made after the repeated
rejection. These audit-only folders remain and contain synthetic fixtures:

```text
C:\Users\muzaf\AppData\Local\Temp\pytest-of-muzaf\pytest-0
C:\Users\muzaf\AppData\Local\Temp\pytest-of-muzaf\pytest-1
```

This report is the only repository deliverable added by the audit, alongside the
ignored working-memory update. The implementation findings remain open.
