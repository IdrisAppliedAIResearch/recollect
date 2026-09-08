# Recollect

[![CI](https://github.com/IdrisAppliedAIResearch/recollect/actions/workflows/ci.yml/badge.svg)](https://github.com/IdrisAppliedAIResearch/recollect/actions/workflows/ci.yml)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)

A deployable harness for episodic conversational memory, instrumented so
that every retrieval decision is visible.

The memory mechanism itself is not new work — it comes from the
[contextDecayWindow](https://github.com/IdrisAppliedAIResearch/contextDecayWindow)
research programme and is consumed here as a pinned library, unmodified.
What Recollect adds is everything needed to *use* it as a product and to
*watch* it while it runs.

---

## What it does

On every turn, instead of resending a growing transcript, the system
rebuilds a small context window from scratch out of stored conversation
episodes.

| path | label | what it does |
|---|---|---|
| recency | **RECENT** | the last N episodes, rendered additively — outside the budget, never dropped |
| semantic | **SEMANTIC** | CC80 ranking (0.8 dense / 0.2 BM25) over the whole store, packed in rank order |
| aspect | **ASPECT** | a protected facet spread: the episodes with the most uncovered topical value per character |

The long-term block splits its character budget 50/50 between the two
ranked paths, whatever remains is returned to CC80, and every admission is
charged the exact serialized characters it costs. When nothing is eligible
for the split, a single CC80 walk owns the whole budget.

## Why the instrumentation is the point

The research this deploys found its most important results in what was
*not* delivered, and the current pipeline has the same failure shapes:
an episode that ranks high but never fits, a semantic half that admits
nothing and silently hands the budget to the fallback, an ASPECT spread
that never runs because the store is still younger than the recency
window. A count of "7 episodes retrieved" is compatible with a healthy
system and with a system where two of its three paths never fire.

So Recollect records a score for **every** episode, every facet-spread
step with its arithmetic, and every packing decision with its reason —
delivered or not.

## The trace is checked, not trusted

The library returns a payload and a report of counts. Everything richer
than that is computed by an instrumented reconstruction in
[`engine/shadow.py`](src/recollect/engine/shadow.py) — and a
reconstruction that merely *looks* right is a liability, because it invites
confident conclusions from numbers nobody verified.

So each turn is computed twice: once through the library untouched, once
through the instrumented path. The two are then compared — the payload
byte for byte, the report field by field. A mismatch raises
`TraceDivergenceError` and the turn is not served.

```
verification:
  payload_identical      : true
  report_fields_identical: true
  authority sha256       : 735196e1b1d63ea6…
  shadow    sha256       : 735196e1b1d63ea6…
  shadow cost            : 0.57 ms
```

This is why the harness can claim its instrumentation is faithful rather
than plausible, and it is why the library is never forked to add logging.

---

## Local quickstart

The default setup uses Docker for the isolated OpenCode research subagent.
Three components remain running locally: Docker Engine (or Docker Desktop),
the chat model server, and Recollect. Recollect starts and attests the OpenCode
container itself on the first delegated research task; do not start that
container manually.

### 1. Install prerequisites

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- Docker Engine on Linux, or Docker Desktop on Windows
- A chat-model GGUF and `llama-server`
- The carried `Qwen3-Embedding-0.6B-Q8_0.gguf` embedding artifact
- The binary-pinned `llama_cpp` package described in
  [docs/EMBEDDER.md](docs/EMBEDDER.md)

The embedder requirement is stricter than a package version: a newly resolved
`llama-cpp-python==0.3.25` build may produce different vectors. Provision the
known-good binary build and let `recollect doctor` verify its identity.

### 2. Clone both sibling repositories

`episodic` is an editable path dependency at
`../contextDecayWindow/episodic`. The repositories must therefore sit next to
each other; cloning Recollect alone makes `uv sync` fail.

```bash
git clone https://github.com/IdrisAppliedAIResearch/contextDecayWindow.git
git clone https://github.com/IdrisAppliedAIResearch/recollect.git
cd recollect
uv sync
```

The path dependency is deliberate. Changes to the research implementation are
exercised against Recollect's shadow verification immediately instead of being
hidden until a package release.

### 3. Configure the environment

```bash
cp .env.example .env
```

At minimum, set the embedding artifact path and confirm the generator URL in
`.env`. The OpenCode values shown here match the pinned image built below:

```dotenv
RECOLLECT_EMBEDDING_MODEL_PATH=/absolute/path/to/Qwen3-Embedding-0.6B-Q8_0.gguf
RECOLLECT_GENERATOR_BASE_URL=http://127.0.0.1:8000/v1
RECOLLECT_SUBAGENT_BACKEND=opencode
RECOLLECT_SANDBOX_CONTAINER_RUNTIME=docker
RECOLLECT_SANDBOX_CONTAINER_IMAGE=recollect-opencode-sandbox:1.18.18
```

### 4. Prepare Docker and build the sandbox image

Start Docker and verify that its Linux engine is reachable:

```bash
docker version
docker build -f deploy/opencode-sandbox/Dockerfile -t recollect-opencode-sandbox:1.18.18 .
docker image inspect recollect-opencode-sandbox:1.18.18
```

On Windows with Docker VMM, create
`%LOCALAPPDATA%\recollect\sandboxes` and add only that directory under
**Docker Desktop > Settings > Resources > File sharing**. Do not share the
repository, home directory, or an entire drive. Linux Docker Engine needs no
equivalent file-sharing configuration. See
[deploy/opencode-sandbox/README.md](deploy/opencode-sandbox/README.md) for the
isolation profile, resource limits, and live Docker tests.

### 5. Start the one-slot model server

Run this in its own long-running terminal. `--parallel 1` is intentional:
main chat, OpenCode, and any native OpenCode subagents take turns using one
model slot.

```bash
llama-server -m <chat-model.gguf> --host 127.0.0.1 --port 8000 -ngl 999 -c 32768 --parallel 1 -fa on --no-webui
```

### 6. Verify dependencies and start Recollect

With Docker and the model server running:

```bash
uv run recollect doctor
uv run recollect serve
```

Keep `recollect serve` in its own terminal. Then use one of:

- Inspector UI: <http://127.0.0.1:8080/>
- OpenAI-compatible API: `http://127.0.0.1:8080/v1`
- Terminal client: `uv run recollect chat`

The sandbox container is lazy. It will not appear in `docker ps` until a chat
turn delegates research, and it remains warm afterward while every invocation
gets a fresh OpenCode session and scrubbed workspace.

### The inspector

Chat on the left, six views of the turn on the right, and a headline strip
that stays put whichever view is open.

| tab | answers |
|---|---|
| **Pipeline** | what each path proposed, delivered, overlapped, and lost — with starvation and an ASPECT fallback called out in words |
| **Context** | the block exactly as the model received it, colour-coded by delivering path, with a character ruler against the budget |
| **Scores** | every episode in the store with its CC80 components, rank, path claims, cost, and the reason it was dropped |
| **Aspect** | the protected spread step by step — marginal, ratio, and coverage arithmetic — or the fallback reason when it did not run |
| **Budget** | where the characters went, allowance versus total output, and every admission decision in order |
| **Verify** | whether the trace reproduces the library, plus embedding identity and generation timings |

Retrieval is emitted as its own event before the model starts, so the
inspector fills in while the reply is still being written. Clicking any past
reply rewinds it to that turn.

There is a **Mock data** toggle in the header: it runs the entire UI off a
generated 120-episode trace with no server at all, which is the fastest way
to see what the views look like under load.

The UI is served from the same origin as the API, so `recollect serve` is the
only process you need. For UI development, `cd ui && npm run dev` proxies
`/api` and `/v1` to port 8080.

### Hands-free conversation

Recollect can listen locally for **“Hey Idris”**, transcribe your speech with
Vosk, run the existing verified chat turn, and speak the reply with Kokoro 82M.
Silero VAD gives you time to pause and detects when you start speaking again.
Install the optional CPU speech packages and download the models once:

```bash
uv sync --extra voice --inexact
uv run --no-sync recollect voice-setup
uv run --no-sync recollect voice-doctor
uv run --no-sync recollect serve
```

`--inexact` preserves the manually installed, binary-pinned embedder. Open the
inspector on localhost, choose a conversation, click **Enable voice**, and allow
microphone access. Say **“Hey Idris, what did we decide?”** and watch the live
transcript in the text box. A 1.4-second pause submits your request. Ask follow-up
questions without repeating the wake phrase. Speaking during generation or
playback interrupts the old reply. **Stop voice** releases the microphone and
stops playback.

For NVIDIA GPU synthesis, stop Recollect, run `uv pip uninstall onnxruntime`,
then `uv sync --extra voice-gpu --inexact`. Set
`RECOLLECT_VOICE_DEVICE=cuda` to require GPU execution and restart the server.
The GPU runtime needs CUDA 13 and cuDNN 9; an optional absolute
`RECOLLECT_VOICE_CUDA_DLL_DIR` can point to those DLLs in a compatible PyTorch
installation. Default `auto` prefers an available GPU; `cpu` selects CPU
synthesis. The GPU and CPU voice extras must not be installed together.

Kokoro speaks the completed, verified reply in audio chunks, starting playback
when the first chunk is ready while later chunks are still being synthesized.

GPU Whisper Turbo dictation is available with the additional `voice-whisper`
extra. Run `uv sync --extra voice-gpu --extra voice-whisper --inexact`, then
`uv run --no-sync recollect voice-setup --whisper`. Select
`RECOLLECT_VOICE_ASR_BACKEND=whisper` and restart Recollect. Vosk still detects
the wake phrase; Silero handles interruptions independently of GPU decoding.
See [voice setup](docs/VOICE.md) for runtime requirements and the distinction
between live transcript revisions and final submitted text.

Voice remains active while the page is open and the browser and computer are
awake. The microphone stays open while enabled, with browser echo cancellation;
use headphones if speaker echo causes unwanted interruptions. Existing voice
installations reuse their verified models and download only the new 2.3 MB
Silero model. Setup, model sources, settings, and troubleshooting are in
[docs/VOICE.md](docs/VOICE.md).

### Using it from Open WebUI

Add an OpenAI-compatible connection pointing at
`http://127.0.0.1:8080/v1` with any API key. The model appears as
`recollect`.

**One deliberate incompatibility.** OpenAI clients resend the whole
transcript on every request; Recollect ignores it and reads only the final
user message. Reconstructing the relevant past from the store is the
mechanism being deployed — honouring the client's history instead would
silently replace the memory system with the client's scrollback and make
every number in the trace meaningless. Sessions are keyed off the request's
`user` field.

---

## Layout

```
src/recollect/
  trace.py            the TurnTrace schema — the central artifact
  config.py           deployment settings, kept apart from mechanism constants
  session.py          sessions, stores, and the turn lifecycle
  api.py              /api/* for the inspector, /v1/* for everyone else
  cli.py              serve · doctor · chat
  engine/
    _internals.py     the single seam into the library's private modules
    shadow.py         the verified shadow trace
    embedder.py       the pinned in-process embedder
    generator.py      the OpenAI-compatible chat client
ui/                   Vite + React inspector
tests/                shadow-vs-library verification, swept over sizes and budgets
docs/
```

## Constraints worth knowing before changing anything

**Embeddings are computed in-process and never over HTTP.** The same GGUF
behind `llama-server` returns different vectors than the same file loaded
in-process — up to 0.599 per component, never byte-equal. The store's
call-shape gate refuses to open against a drifted embedder, by design.
Details in [docs/EMBEDDER.md](docs/EMBEDDER.md).

**A pinned version number does not pin the computation.** Two installs of
`llama-cpp-python==0.3.25` on this machine produced different embeddings
because one was a CUDA build and one was CPU-only. The binaries are the
identity. `recollect doctor` reports their hashes.

**The mechanism constants are not tuning knobs.** `EpisodicConfig` values
each shaped a committed research number, and a store records the config it
was created under and refuses to reopen under a different one. Deployment
settings live separately in `RecollectConfig`.

**The live instrument is coarse.** The source research measured a 3.0-point
run-to-run band on a 13-point rubric across byte-identical replicates, and
the runtime is not bit-reproducible — the same prompt at the same seed can
produce a different answer. Delivery counts, episode identities and
character accounting *are* exact and do reproduce; scored judgements about
answer quality from single runs do not.

## Tests

```bash
uv run pytest
```

The suite sweeps store sizes × budgets — including the degenerate ones:
zero, one character, and exactly the cost of the empty block tags — and
asserts byte equality between the library and the reconstruction every
time. It uses a deterministic fake embedder, so it needs no model file and
runs in about ten seconds.

## Licence

**Proprietary. Source-available, not open source.**
Copyright © 2026 Idris Applied AI Research. All rights reserved.

This repository is public so the mechanism can be read, reviewed, and checked
against the claims made about it. That is the whole of the grant: you may read
this source, and you may not run, deploy, copy, modify, or build on it. See
[`LICENSE`](LICENSE).

Deployment and commercial licences are available —
**idrisappliedairesearch@gmail.com**.

### On `episodic` and the AGPL

Recollect builds on the `episodic` library, which is dual licensed
AGPL-3.0-or-later **or** commercial. A proprietary product built on AGPL code
would normally be a violation; it is not one here, because Idris Applied AI
Research holds the copyright in `episodic` and uses it under its own commercial
licence rather than under the AGPL.

That reasoning applies to the copyright holder and to nobody else. If you obtain
`episodic`, you get it under the AGPL — including section 13, which reaches
network use, not just distribution — unless you hold a separate agreement.
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) has the full chain, along
with every other dependency and its terms.
