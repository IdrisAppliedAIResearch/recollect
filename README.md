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
episodes. Three retrieval paths compete for a fixed character budget:

| path | label | what it does |
|---|---|---|
| recency | **RECENT** | the last N episodes in order, no scoring |
| similarity | **RELATED** | episodes whose cosine clears a fixed threshold |
| coverage | **SPREAD** | a budgeted greedy: relevance plus a bonus for entering an uncovered topic cluster |

They are packed in that order, each admission charged the exact serialized
characters it costs, and the budget is a hard ceiling.

## Why the instrumentation is the point

The research this deploys found its most important results in what was
*not* delivered:

- The similarity path is **inert on the internal corpus**. Its threshold
  sits at 0.48 while the highest cosine ever recorded for known-relevant
  content is 0.2779 — roughly twice the height genuine relevance reaches.
  It delivered zero episodes at 8 of 8 probes.
- The coverage selector **chooses as though it owns the whole budget**,
  then gets packed last, after recency has already spent it. The set it
  picked is not the set it would have picked had it known what it would
  actually be given.

Neither is visible from looking at what came back. A count of "7 episodes
retrieved" is compatible with both a healthy system and a system where two
of its three paths never fire. So Recollect records a cosine for **every**
episode, a cluster for every candidate, every greedy step with its
arithmetic, and every packing decision with its reason — delivered or not.

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

Chat on the left, seven views of the turn on the right, and a headline strip
that stays put whichever view is open.

| tab | answers |
|---|---|
| **Pipeline** | what each path proposed, delivered, overlapped, and lost — with starvation and an inert similarity path called out in words |
| **Context** | the block exactly as the model received it, colour-coded by delivering path, with a character ruler against the budget |
| **Scores** | every episode in the store with its cosine, rank, cluster, paths, cost, and the reason it was dropped |
| **Clusters** | which topic regions the selector entered, and which it entered and then lost to packing |
| **Selector** | the greedy walk step by step, with the gain arithmetic behind each choice |
| **Budget** | where the characters went, and every admission decision in order |
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
