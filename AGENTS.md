# AGENTS.md

Instructions for any AI agent working in this repository. Read this file
completely before your first edit, and again after every context
compaction. It is the only rules file — `CLAUDE.md` just points here.

---

## 0. Boot sequence — do this before anything else

Run these four steps at the start of a session **and immediately after
every compaction or context reset.** They take under a minute and they are
what keeps you from redoing finished work or undoing it.

1. Read this file, start to finish.
2. Read `.agent/TODO.md`. If it does not exist, create it from the template
   in §1.
3. Run these and read the output:
   ```bash
   git status --short && git log --oneline -5 && git diff --stat
   ```
4. Update `.agent/TODO.md` so it matches what step 3 actually showed. Move
   finished items to `## Done`. Then continue.

For a request to start, launch, or bring up the local application, follow
the complete runtime sequence in §7.1. The agent owns that sequence;
do not hand the user a list of routine startup commands to run themselves.
Reading this file or doing documentation work does not itself mean start
the services. Respect an explicit shutdown until a launch is requested.

**After a compaction, do not trust your memory of what you were doing.**
Your summary of the conversation is lossy; `git diff` and `.agent/TODO.md`
are not. When they disagree with your recollection, they are right.

---

## 1. The TODO file

`.agent/TODO.md` is your working memory. It is gitignored — it is scratch
state, not project history. Keep it short. Rewrite it rather than letting
it grow.

```markdown
# Goal
<one sentence: what the user actually asked for>

## Done
- [x] thing that is finished and verified

## Now
- [ ] exactly one item

## Next
- [ ] later item
- [ ] later item

## Decided / do not revisit
- <choice already made, so you don't relitigate it after compaction>
```

Rules:

- **`## Now` holds exactly one item.** If you want to start a second, you
  are thrashing — finish or park the first.
- Move an item to `## Done` only after §4's checks pass. Not when the code
  "looks right".
- `## Decided` exists because compaction loses decisions. Write down
  anything you or the user settled, so you don't reopen it.
- If your harness has its own todo tool, use it too — but this file is the
  one that survives compaction, so it is authoritative.

---

## 2. What this project is (30 seconds)

Recollect makes a researched conversational-memory mechanism usable, and
shows what it is doing while it runs.

Each turn, instead of resending the whole chat transcript, the system
rebuilds a small context window from stored episodes. **RECENT** (last N)
is additive: rendered outside the character budget and never dropped. The
long-term block spends the budget under a protected 50/50 split: **CC80**
(0.8 dense / 0.2 BM25 over the complete store, skip-on-overflow) walks one
half, and a static **ASPECT** facet spread takes the other, admitting the
episodes with the most uncovered topical value per character; whatever the
split leaves over is returned to CC80 in rank order. When no eligible
episode remains for the split, a single CC80 walk owns the whole budget.

The mechanism itself lives in a separate library called `episodic`, at
`../contextDecayWindow/episodic`. **This repo does not contain it and must
not change it.**

The thing that makes this repo unusual: every turn is computed **twice** —
once through the library untouched (the authority), once through an
instrumented copy in `engine/shadow.py` that records the detail the library
throws away. The two are compared byte-for-byte. If they disagree, the turn
is refused. That is what lets the UI show per-episode scores that are
actually true.

---

## 3. Hard rules

Breaking one of these silently destroys the value of the project. They are
not style preferences and you may not trade them for a passing test.

**1. Never modify, fork, copy, or vendor `episodic`.**
It is certified to behave identically to the published research. A copy
with logging added is no longer that thing. If you need its internals, the
one legal seam is `src/recollect/engine/_internals.py`.

**2. Never weaken the shadow verification.**
If `TraceDivergenceError` fires, the instrumentation is wrong or stale —
**fix it**. Specifically forbidden, because these are the tempting moves:
- adding a tolerance or "close enough" comparison
- wrapping it in `try`/`except`
- passing `strict=False`
- skipping, xfailing, or deleting the test that caught it

A green suite bought with any of those is worse than a red one, because
every number the UI shows becomes unverified while still looking verified.

**3. Never add an HTTP path for embeddings.**
Embeddings are computed in-process, always. The same model file served over
HTTP returns *different vectors* (measured: up to 0.599 per component), and
the store refuses to open against them. See `docs/EMBEDDER.md`.

**4. Never move a constant between `EpisodicConfig` and `RecollectConfig`.**
`EpisodicConfig` values each shaped a published result. `RecollectConfig` is
machine-specific settings. The line between them is load-bearing.

**5. Never claim a quality improvement from a single run.**
The runtime is not bit-reproducible; the same prompt can give a different
answer. Counts, episode IDs and character accounting **are** exact and do
reproduce — quality judgements do not. Say "the count changed from 4 to 7",
never "this run was better".

**6. Never force push. There is no exception.**
Not `--force`, not `--force-with-lease`, not `push -f`, and not by way of
`git commit --amend`, `git rebase`, or `git reset --hard` on anything that
has already been pushed. Published history is the one thing here that
cannot be reconstructed from the working tree, and an agent that rewrites
it can destroy work it never saw — the user's local commits, another
machine's, a colleague's. If history genuinely looks wrong, **stop and
describe the problem to the user.** Let them decide. Fixing it forward with
a new commit is almost always available and is always safe.

`main` is also protected server-side against force pushes and deletion, and
the protection applies to admins, so this is enforced and not merely asked
for. If a push is rejected for that reason, **that is the rule working** —
do not attempt to disable the protection to get your push through.

**7. Never push, and never open a pull request, until the task is
complete.**
"Complete" means §4's checks have been run and passed, not that the code
looks finished. This includes **draft** PRs — a draft is still a push, still
notifies people, and still invites review of work you know is unfinished.
Do not open one to show progress; report progress in the conversation
instead. And do not push unless the user asked in this session: committing
locally is fine to do and offer, but publishing is the user's call.

**8. Never commit `.env`, `var/`, `node_modules/`, or any `.gguf`.**
They are gitignored. Do not add them with `-f`.

---

## 4. Before you say you are done

Run these. Both must be clean. Paste the real output.

```bash
uv run --no-sync ruff check . && uv run --no-sync pytest
```

If you touched anything in `ui/`:

```bash
cd ui && npm run build
```

**Report what actually happened.** If tests fail, say they failed and show
the output. Never write "tests pass" without having just run them and seen
it. A false green is the most expensive thing you can produce here, because
the user will build on it.

Do not mark a `.agent/TODO.md` item Done before this passes.

**Delete your scratch artifacts when the task is done.** After verifying,
remove the temporary files the task created outside the repository:
screenshots, throwaway seed or test scripts, log captures, temporary
browser profiles. They outlive the task, and the next agent cannot tell a
stale screenshot from a current one — leftovers from an old task read as
evidence of the current one. Anything test-only created *inside* the repo
(e.g. a throwaway session under `var/`) must be deleted or explicitly
pointed out to the user before you report done.

---

## 5. How to make changes

**Make the smallest change that does the job.** Do not refactor code you
were not asked to touch. Do not rename things for consistency. Do not
upgrade dependencies as a side quest.

**Match the surrounding code.** Same naming, same comment density, same
idioms. Read the file before editing it.

**Comments explain *why*, not *what*.** Especially where a value came from a
measurement — those comments are what stop the next person deleting a
constraint that looks arbitrary. Never leave changelog notes in source;
that is git's job.

**When a test fails, fix the cause.** Do not adjust the test to match the
broken behaviour unless you can state why the test's expectation was wrong.

### Stop and ask the user when

- a hard rule in §3 seems to be blocking the task
- you would need to change `episodic`
- the fix requires deleting or rewriting a test you did not write
- the same approach has failed **twice** — stop, do not try a third
  variation. Report what you tried and what happened.

Asking costs one message. Guessing wrong here costs the project's
credibility.

---

## 6. Where things are

```
src/recollect/
  trace.py          the TurnTrace schema — the shape everything else renders
  config.py         deployment settings only
  session.py        sessions, stores, turn lifecycle
  api.py            /api/* for the UI, /v1/* OpenAI-compatible
  cli.py            serve · doctor · chat
  engine/
    _internals.py   the ONLY file that reaches into the library
    shadow.py       the double-compute + verification  ← most delicate file
    embedder.py     in-process pinned embedder
    generator.py    chat client
ui/src/             React inspector (6 tabs)
tests/              shadow-vs-library sweep
docs/EMBEDDER.md    read before touching anything that makes a vector
```

Rough rule: if your change makes the UI show a *new number*, it probably
belongs in `trace.py` and `shadow.py` first, and the UI last.

---

## 7. Commands

```bash
uv run --no-sync pytest           # fake models; Docker tests are opt-in
uv run --no-sync ruff check .     # line length 88
uv run --no-sync recollect doctor # embedder identity + generator reachability
uv run --no-sync recollect serve  # UI + API on :8080; see full launch below
```

Use the existing Python 3.13 `.venv`. `--no-sync` preserves the installed,
binary-pinned embedder and GPU packages. Dependency installation is a repair
or provisioning step, not part of every launch; see §7.1.

### 7.1 Agent-owned full local launch

When the user asks to launch Recollect, the default scope is **Docker +
the GPU chat model + Recollect + warmed GPU voice**, unless they explicitly
request a narrower scope. Execute the steps, resolve routine startup issues,
and report verified readiness. Do not ask the user to repeat the model,
device, or launch choices already recorded here. Report actual blockers;
do not silently downgrade to CPU or call a text-only server fully ready.

This is the verified Windows/RTX 5090 configuration as of 2026-09-08.
Use the saved `.env` and installed files; verify paths before launching.
Machine-specific paths below are relative to `%USERPROFILE%` unless noted.

| Component | Where/how it runs | Ready condition |
|---|---|---|
| Qwen3.8-27B-UD-Q4_K_XL.gguf | Separate `llama-server`, GPU, loopback port 8001 | `/health` says `ok`, `/v1/models` identifies the expected model, startup log confirms GPU offload |
| Whisper large-v3-turbo | In Recollect, faster-whisper/CTranslate2, CUDA float16 | Voice status: `asr_backend=whisper`, `asr_device=cuda`, `asr_compute_type=float16`, `asr_ready=true` |
| Kokoro 82M | In Recollect, ONNX Runtime GPU | Voice status: `provider=CUDAExecutionProvider` |
| Qwen3-Embedding-0.6B-Q8_0.gguf | In Recollect, **CPU**, pinned native build, 8 threads, `n_ctx=512`, `n_gpu_layers=0` | Research sentinel matches; never move this model to GPU or HTTP |
| Vosk small English 0.15 + Silero VAD | In Recollect, **CPU** | Voice initialization succeeds for “Hey Idris”; wake/onset detection stays independent of Whisper |
| Docker/OpenCode research | Linux Docker engine + pinned `recollect-opencode-sandbox:1.18.18` image | Engine and image available; Recollect creates/attests its sandbox lazily on first delegation |
| Recollect UI/API | Existing `.venv`, loopback port 8080 | `/api/health` passes and the built UI loads |

OpenCode uses the **same Qwen server and single model slot** as main chat.
Do not start a second chat model or give the container its own GPU model.
The embedding and speech models run inside Recollect, not as additional
HTTP servers. A healthy empty sandbox list before the first research task
is expected; do not bypass the manager by manually running its container.

#### A. Preflight and reuse

1. Work from the repository root. Read `.env` selectively without dumping
   credentials. Check ports 8001/8080 and their owning process command lines.
   Reuse an already healthy matching service; do not duplicate it or kill
   an unrelated process merely because it occupies a port.
2. Verify the sibling `../contextDecayWindow/episodic`, existing `.venv`,
   model assets, and CUDA DLL directories. The known model server is
   `.unsloth\llama.cpp\build\bin\Release\llama-server.exe`.
   The chat weights are at
   `.cache\huggingface\hub\models--unsloth--Qwen3.8-27B-GGUF\snapshots\f1bfb127c64f7072bdd2cad55f258b9c8b2910fe\Qwen3.8-27B-UD-Q4_K_XL.gguf`.
   The embedding weights are at
   `.cache\huggingface\hub\Qwen3-Embedding-0.6B-GGUF\Qwen3-Embedding-0.6B-Q8_0.gguf`.
   Voice assets live under repository `var/models/voice`; Whisper is in
   `var/models/voice/whisper-large-v3-turbo`.
3. Verify the effective settings below. Preserve other `.env` values;
   never replace the user's `.env` with `.env.example` during a restart.

   ```dotenv
   RECOLLECT_GENERATOR_BASE_URL=http://127.0.0.1:8001/v1
   RECOLLECT_GENERATOR_MODEL=Qwen3.8-27B-UD-Q4_K_XL.gguf
   RECOLLECT_EMBEDDING_THREADS=8
   RECOLLECT_SUBAGENT_ENABLED=true
   RECOLLECT_SUBAGENT_BACKEND=opencode
   RECOLLECT_SANDBOX_CONTAINER_RUNTIME=docker
   RECOLLECT_SANDBOX_CONTAINER_IMAGE=recollect-opencode-sandbox:1.18.18
   RECOLLECT_VOICE_WAKE_PHRASE=hey idris
   RECOLLECT_VOICE_DEVICE=cuda
   RECOLLECT_VOICE_ASR_BACKEND=whisper
   RECOLLECT_VOICE_ASR_DEVICE=cuda
   RECOLLECT_VOICE_ASR_COMPUTE_TYPE=float16
   ```

   `RECOLLECT_EMBEDDING_MODEL_PATH` points to the embedding artifact above.
   `RECOLLECT_VOICE_CUDA_DLL_DIR` points to the absolute existing directory
   `.unsloth\studio\unsloth_studio\Lib\site-packages\torch\lib` under the
   user profile. Keep the established 1.4-second end-of-speech pause and
   120-second capture limit unless the user requests a timing change.
4. For the background-launch examples, initialize these variables from the
   repository root. Logs are intentional runtime files under ignored `var/`:

   ```powershell
   $recollectRoot = (Get-Location).Path
   $runtimeLogs = Join-Path $recollectRoot 'var\logs'
   $launchStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
   New-Item -ItemType Directory -Force -Path $runtimeLogs | Out-Null
   ```

#### B. Docker first

Run `docker info --format '{{.OSType}}'`; it must return `linux`. On this
machine Docker Desktop is installed at
`%LOCALAPPDATA%\Programs\DockerDesktop\frontend\Docker Desktop.exe`.
If the engine is down, start that verified executable with
`Start-Process -WindowStyle Hidden`, then poll the engine in short intervals
until ready (allow up to two minutes overall; keep the user informed).
On another machine discover the installed Docker path rather than guessing.

```powershell
# Only when the Linux engine is not already ready:
$dockerDesktop = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\frontend\Docker Desktop.exe'
Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
```

Check the image with:

```powershell
docker image inspect recollect-opencode-sandbox:1.18.18
```

Build it only if absent or stale relative to the Dockerfile, locked image
requirements, `src/recollect/engine/mcp_research.py`, or
`src/recollect/engine/webtools.py`. These are the image's build inputs, so
a tag alone does not establish that it contains current research-tool changes:

```powershell
docker build -f deploy/opencode-sandbox/Dockerfile `
  -t recollect-opencode-sandbox:1.18.18 .
```

The only Docker Desktop file share this app needs is
`%LOCALAPPDATA%\recollect\sandboxes`, matching `RECOLLECT_SANDBOX_ROOT`.
Preserve the isolated mounts and other restrictions in
[deploy/opencode-sandbox/README.md](deploy/opencode-sandbox/README.md).
Never start a host OpenCode fallback if Docker fails. Engine/image readiness
does not guarantee that external search providers will answer a later query.

#### C. Start the GPU chat model

Use an agent-managed long-running terminal, or a hidden background process.
The following PowerShell arguments reproduce the tested single-slot setup:

```powershell
$modelServerExe = Join-Path $env:USERPROFILE '.unsloth\llama.cpp\build\bin\Release\llama-server.exe'
$chatModel = Join-Path $env:USERPROFILE '.cache\huggingface\hub\models--unsloth--Qwen3.8-27B-GGUF\snapshots\f1bfb127c64f7072bdd2cad55f258b9c8b2910fe\Qwen3.8-27B-UD-Q4_K_XL.gguf'
$modelArgs = @(
  '--model', ('"{0}"' -f $chatModel), '--host', '127.0.0.1', '--port', '8001',
  '--ctx-size', '32768', '--parallel', '1', '--n-gpu-layers', '999',
  '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0',
  '--flash-attn', 'on', '--jinja', '--metrics', '--no-webui'
)
$chatProcess = Start-Process -FilePath $modelServerExe -ArgumentList $modelArgs `
  -WorkingDirectory $recollectRoot -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "$runtimeLogs\qwen-$launchStamp.stdout.log" `
  -RedirectStandardError "$runtimeLogs\qwen-$launchStamp.stderr.log"
```

Verify both paths before executing. Capture startup output in the managed
terminal or intentional logs under `var/`; inspect it for GPU offload.
Poll `http://127.0.0.1:8001/health` with bounded request timeouts and verify
`http://127.0.0.1:8001/v1/models`. Do not treat process creation as readiness.
Preserve the context size, one-slot serialization, and quantized KV cache;
they leave room for Whisper and Kokoro on the 32 GB GPU.

#### D. Start Recollect and warm the serving process

Run `uv run --no-sync recollect doctor` after Qwen is ready. Require the
research sentinel
`baecf77627380f36f75a69c4454b064d886133f04255c5e5b4d3f24f00e7c4b8`.
If it drifts, fix the environment using [docs/EMBEDDER.md](docs/EMBEDDER.md);
do not change the stored pin or offload the embedder to GPU.

Build the UI with `npm run build` from `ui/` when source changed or `ui/dist`
is absent. Launch `uv run --no-sync recollect serve` from the repository in
an agent-managed long-running terminal. For a PowerShell background launch:

```powershell
$appProcess = Start-Process -FilePath "$recollectRoot\.venv\Scripts\recollect.exe" `
  -ArgumentList 'serve' -WorkingDirectory $recollectRoot `
  -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "$runtimeLogs\recollect-$launchStamp.stdout.log" `
  -RedirectStandardError "$runtimeLogs\recollect-$launchStamp.stderr.log"
```

Poll `http://127.0.0.1:8080/api/health`. Require the embedder's
`sentinel_matches_research=true` and the generator's `reachable=true`.
Then **warm voice in this server**, before telling the user it is ready.
`voice-doctor` runs in another process and does not warm the serving process;
`/api/voice/status` alone also does not load the models. This PowerShell
snippet opens and closes the voice socket without capturing audio or adding
a chat turn:

```powershell
@'
import asyncio
import json
import httpx
from websockets.asyncio.client import connect

async def warm():
    async with connect("ws://127.0.0.1:8080/api/voice/listen") as socket:
        event = json.loads(await asyncio.wait_for(socket.recv(), 60))
        assert event["type"] == "state" and event["state"] == "waiting", event
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get("http://127.0.0.1:8080/api/voice/status")
        response.raise_for_status()
        status = response.json()
    assert status["available"] and status["error"] is None, status
    assert status["asr_backend"] == "whisper" and status["asr_ready"], status
    assert status["asr_device"] == "cuda", status
    assert status["asr_compute_type"] == "float16", status
    assert status["provider"] == "CUDAExecutionProvider", status
    print(json.dumps(status, indent=2))

asyncio.run(warm())
'@ | uv run --no-sync python -
```

If an existing browser already owns the voice listener, reuse its healthy
status instead of disconnecting it. Handle a cold-start timeout explicitly;
never report readiness from a still-pending warm-up.

Finally check `nvidia-smi`, open/refresh `http://127.0.0.1:8080/`, and verify
the existing history loads. Report Docker/image readiness, Qwen health,
embedding identity, and both GPU voice providers. Record current process IDs,
managed terminal handles/log paths, and readiness in `.agent/TODO.md`; old recorded
PIDs are evidence, never instructions to kill those IDs on a later launch.
Leave services running after a launch. Do not create test conversations in
the user's session or require another wake-phrase/configuration decision.

#### Provisioning or repair only

Do not resync, reinstall, rebuild, or redownload everything on each launch.
If dependencies/assets are missing, stop Recollect before replacing loaded
Windows DLLs, then use:

```powershell
uv sync --extra voice-gpu --extra voice-whisper --inexact
uv run --no-sync recollect voice-setup --whisper
```

`--inexact` preserves the manually provisioned `llama-cpp-python==0.3.25`
native build. A same-version replacement is not necessarily compatible.
Only `onnxruntime-gpu==1.29.0` belongs in this GPU environment; CPU
`onnxruntime` installs the same module and must not coexist. The dependency
exclusions in `pyproject.toml` prevent Kokoro/faster-whisper from reintroducing
it. Kokoro uses CUDA 13/cuDNN 9 DLLs from the configured directory; Whisper
uses its installed CUDA 12/cuDNN 9 packages, faster-whisper 1.2.1 and
CTranslate2 4.8.2. Do not confuse the two CUDA library configurations.
See [docs/VOICE.md](docs/VOICE.md) for repair details and pinned model receipts.

#### Shutdown when requested

Stop Recollect first, preferably gracefully so its sandbox manager can close
its container, then stop the verified Qwen process. For background processes,
recheck command lines and port owners before `Stop-Process`; include only
their verified launchers if still alive. Never kill all Python/Node processes.
Whisper, Kokoro, the embedder, Vosk and Silero unload with Recollect.
Check for an orphaned `recollect-subagent-*` container using `docker ps -a`;
verify its image and mounts belong to this app before stopping/removing it.
Do not shut down shared Docker Desktop/Engine unless requested or known to
have been started exclusively for this launch with no other workloads.
Verify ports 8080/8001 are closed and GPU model processes are gone. Preserve
model downloads, saved conversations, `.env`, and the pinned environment.
Record the stopped state in `.agent/TODO.md` so the next agent respects it.

Notes that save time:

- Tests use a **fake embedder** and need no model file. Keep it that way; a
  suite requiring a 639 MB download stops being run.
- When touching packing or budgets, test the degenerate values too: `0`,
  `1`, `EMPTY_PAYLOAD_CHARS - 1`, `EMPTY_PAYLOAD_CHARS`. Every off-by-one in
  this system has surfaced there.
- Blocking work (embedding, SQLite, retrieval) must run via
  `asyncio.to_thread`, never directly on the event loop.
- `uv.lock` is generated. Change dependencies in `pyproject.toml` and run
  the appropriate `uv sync --inexact` command above — never hand-edit the lock.

---

## 8. Honesty

This repository exists to make a system's behaviour inspectable. That
purpose fails if the agent working on it is not itself accurate.

- Do not invent numbers, file paths, or test results. If you did not read
  it or run it, say so.
- If you are unsure, say you are unsure.
- If you broke something, say what broke.
- If you skipped part of the task, say which part and why — do not report
  partial work as complete.
