# Recollect

[![CI](https://github.com/IdrisAppliedAIResearch/recollect/actions/workflows/ci.yml/badge.svg)](https://github.com/IdrisAppliedAIResearch/recollect/actions/workflows/ci.yml)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)

Recollect is a chat application with a long-term memory that never grows.

Most chat systems remember by re-sending the whole conversation to the model
on every message. Recollect does not. It stores the conversation, and on each
turn it builds a small, fresh context out of the pieces that matter right now.
It also shows you exactly which pieces it picked, and why.

The memory mechanism itself is not new work here. It comes from the
[contextDecayWindow](https://github.com/IdrisAppliedAIResearch/contextDecayWindow)
research programme and is used as a pinned library, unchanged. What this
repository adds is everything needed to *run* it as a product and to *watch*
it while it runs.

---

## The architecture in one picture: two modes

Recollect runs in two modes, and they are deliberately different from each
other.

```
                          you
                           |
            +--------------+--------------+
            |                             |
   MODE 1: conversation          MODE 2: delegated work
   the main assistant            a sandboxed worker agent
            |                             |
   episodic memory store          task store + scratch files
   (this conversation)            (thrown away when done)
            |                             |
            +--------------+--------------+
                           |
                  one local model
```

| | **Mode 1 — conversation** | **Mode 2 — delegated work** |
|---|---|---|
| What it is | The assistant you talk to | A background agent it hands jobs to |
| Good at | Answering, recalling, deciding | Web research, multi-step work, producing files |
| Where it runs | Inside the Recollect server | Inside a locked-down Docker container |
| What it can reach | Your stored conversation | The internet and an empty scratch folder |
| How long it takes | Seconds | Minutes |
| What it remembers | Everything you actually said | Nothing, once the job ends |

Mode 1 is the only thing that owns memory. Mode 2 is a tool that Mode 1 picks
up, uses, and puts down. Keeping that line sharp is the point of the design:
if a worker's tool calls and internal chatter leaked into the conversation
memory, the memory would stop being a conversation and become a log file.

---

## Mode 1: the conversation, and how its memory works

### The problem with the normal approach

A normal chat app keeps the full transcript and sends it again every time you
type. That works until the transcript gets too big for the model. Then the app
has to do one of two things:

1. **Cut** the oldest messages off. Whatever fell off is gone for good.
2. **Compact** them, which means asking the model to summarize the old part
   and keeping the summary instead.

Both are lossy, and neither tells you what you lost. Compaction is worse than
it looks, because the summary is itself a guess, and later summaries end up
being summaries of summaries.

### What Recollect does instead

Recollect saves the conversation as **episodes**. One episode is one exchange:
your message plus the assistant's reply. An episode is written only after the
reply is finished, because half a turn is not a memory yet.

Then, on every new turn, it throws the old context away and builds a new one
from scratch. Three paths compete to fill it:

| path | label in the trace | what it contributes |
|---|---|---|
| recency | **RECENT** | the last 32 episodes, in order, always included |
| semantic | **SEMANTIC** | the best matches from the whole store, by meaning and by keyword |
| aspect | **ASPECT** | a spread of episodes that each add a *new topic*, not more of the same |

A few details that matter:

- **RECENT is never dropped.** It sits outside the character budget, so a busy
  long-term search can never push out what you just said.
- **SEMANTIC** is a fixed blend called CC80: 80% meaning-based (vector) search
  and 20% keyword (BM25) search, run over *every* episode in the store, not
  over a recent slice of it. Something you said six months ago is as reachable
  as something you said this morning.
- **ASPECT** exists because top-ranked results tend to repeat each other. It
  picks the episodes that cover the most ground you have not covered yet, per
  character spent.
- The long-term part of the context gets a budget of **32,000 characters**.
  That budget is split 50/50 between SEMANTIC and ASPECT. Whatever one side
  cannot use goes back to the other. Every episode admitted is charged the
  exact number of characters it actually costs once rendered, not an estimate.

### Why the context never grows

Every request Recollect sends to the model has exactly three parts:

1. the system prompt (fixed),
2. one memory block (rebuilt from scratch this turn, capped),
3. your new message.

That is it. There is no transcript. Turn 5 and turn 5,000 send the model the
same shape and roughly the same amount of input. A conversation can run for
months and the per-turn cost does not drift upward.

This is also why **nothing is ever compacted**. There is no growing thing that
needs shrinking, so there is no summarizing step, and so there is no point at
which your earlier words get replaced by a paraphrase of your earlier words.

### Why nothing is ever lost

The episode store is append-only. Episodes are not edited, merged, summarized,
or deleted to make room, because nothing needs to be made room *for*.

So "forgetting" here means something narrower and more honest than usual: an
episode that did not appear this turn was **not selected this turn**. It is
still in the store at full fidelity, and a different question can pull it back
at any time.

### One deliberate incompatibility

Recollect speaks the OpenAI chat API, so tools like Open WebUI can connect to
it. Those tools re-send the whole transcript on every request. **Recollect
ignores it** and reads only your last message.

That is not a bug. Honouring the client's transcript would quietly replace the
memory system with the client's scrollback, and every number Recollect reports
about its own retrieval would become meaningless. Sessions are keyed off the
request's `user` field instead.

### The trace is checked, not trusted

The library returns the finished context and a short report of counts. It does
not explain itself. Everything richer than that — a score for every episode, a
reason for every rejection — is produced by an instrumented re-implementation
in [`engine/shadow.py`](src/recollect/engine/shadow.py).

A re-implementation that merely *looks* right is dangerous, because it invites
confident conclusions from numbers nobody verified. So every turn is computed
**twice**: once through the untouched library (the authority), once through the
instrumented copy. The two results are then compared — the context byte for
byte, the report field by field.

If they disagree, `TraceDivergenceError` is raised and **the turn is not
served**.

```
verification:
  payload_identical      : true
  report_fields_identical: true
  authority sha256       : 735196e1b1d63ea6…
  shadow    sha256       : 735196e1b1d63ea6…
  shadow cost            : 0.57 ms
```

This is why the instrumentation can be called faithful rather than plausible,
and it is why the library is never forked just to add logging to it.

---

## Mode 2: delegated work

### When it happens

The assistant has one tool, `run_subagent`. It is meant for work the
conversation cannot do on its own: looking something up on the live web, or a
job with enough steps that it needs its own workspace.

The assistant is told to use it for exactly that, and not for ordinary
reasoning or for anything already answerable from memory.

### Where the worker runs

The worker is OpenCode, running inside a pinned Docker image. The container is
deliberately boring to be inside:

- read-only root filesystem, non-root user, no added Linux capabilities,
  `no-new-privileges`;
- limits on processes, CPU, memory, and open files; no swap; private IPC;
- exactly two mounted folders — `/config` (its configuration and skills,
  read-only) and `/workspace` (an empty scratch folder, erased before *and*
  after every job).

There is no fallback to running the worker directly on the host. If Docker or
the pinned image is missing, delegation fails closed. Recollect starts and
attests the container itself, the first time a job actually needs it.

### Two ways a job comes back

- **Inline (the default).** The turn pauses, the worker runs, and the
  assistant answers using the result. You wait, but you get one clean answer.
- **Continuous (opt-in, `RECOLLECT_SUBAGENT_CONTINUOUS_ENABLED=true`).** The
  job becomes a durable task with its own ID and its own message mailbox. You
  keep talking while it runs, ask how it is going, steer it, cancel it, or ask
  for a revision later. Tasks survive a restart. Other conversations cannot
  see them.

### The memory boundary

This is the part that keeps Mode 1 clean. When delegation is involved:

| goes into conversation memory | does **not** |
|---|---|
| what you actually said | tool calls and their arguments |
| the assistant's substantive answers | the worker's step-by-step chatter |
| finished research findings, saved as ordinary episodes | progress updates, status checks, file-delivery notices |

Findings are written back as real episodes on purpose. Without that, a
completed piece of research would fall out of reach as soon as it aged past
the live task list, and the assistant would have to cram everything into one
long answer. Because they are stored, the answer can be short and you can ask
about it again next week.

### One model, one slot

Main chat and the worker share the **same** local model server, with a single
slot (`--parallel 1`). They take turns. There is no second chat model, and the
container never gets its own.

---

## Seeing it work

The inspector is the web UI at `http://127.0.0.1:8080/`. Chat is on the left;
six views of the current turn are on the right.

| tab | answers |
|---|---|
| **Pipeline** | what each path proposed, delivered, overlapped, and lost |
| **Context** | the exact block the model received, coloured by which path delivered each piece |
| **Scores** | every episode in the store, its score, its rank, its cost, and why it was dropped |
| **Aspect** | the topic spread step by step, with the arithmetic |
| **Budget** | where the 32,000 characters went, decision by decision |
| **Verify** | whether the trace reproduced the library, plus timings |

Retrieval is sent to the browser *before* the model starts writing, so the
inspector fills in live rather than after the fact. Clicking an old reply
rewinds every tab to that turn. A **Mock data** toggle runs the whole UI off a
generated 120-episode trace with no server at all.

Why bother with all of this: the original research found its most important
results in what was *not* delivered. "7 episodes retrieved" is equally
consistent with a healthy system and with a system where two of its three
paths never fired at all.

---

## Running it

Setup is involved, mostly because the embedding model has to be bit-for-bit
the one the research used. Full instructions are in
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

The short version:

- You need Python 3.13, [uv](https://docs.astral.sh/uv/), Docker, a chat model
  served by `llama-server`, and the pinned `Qwen3-Embedding-0.6B-Q8_0.gguf`.
- Clone this repository **next to** `contextDecayWindow`. `episodic` is a path
  dependency, so cloning Recollect on its own makes `uv sync` fail.
- Copy `.env.example` to `.env` and set the embedding model path.
- Run `uv run recollect doctor`, then `uv run recollect serve`.

| topic | document |
|---|---|
| Standalone, desktop-host, and Ubuntu-client setups | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| Hands-free "Hey Idris" voice conversation | [docs/VOICE.md](docs/VOICE.md) |
| The pinned embedder, and why it is pinned | [docs/EMBEDDER.md](docs/EMBEDDER.md) |
| Container isolation profile | [deploy/opencode-sandbox/README.md](deploy/opencode-sandbox/README.md) |
| Network hardening and pairing | [docs/SECURITY_HARDENING_2026-09-08.md](docs/SECURITY_HARDENING_2026-09-08.md) |

---

## Layout

```
src/recollect/
  trace.py            the TurnTrace schema — the central artifact
  config.py           deployment settings, kept apart from mechanism constants
  session.py          sessions, stores, and the turn lifecycle
  api.py              /api/* for the inspector, /v1/* for everyone else
  cli.py              serve · doctor · chat
  tasks.py            delegated tasks and their mailbox
  task_chat.py        the conversation path that can delegate
  engine/
    _internals.py     the single seam into the library's private modules
    shadow.py         the verified shadow trace
    embedder.py       the pinned in-process embedder
    generator.py      the chat client
    sandbox/          container isolation, attestation, and the worker runner
ui/                   Vite + React inspector
tests/                shadow-vs-library verification, swept over sizes and budgets
docs/
```

## Before you change anything

**Embeddings are computed in-process, never over HTTP.** The same model file
behind an HTTP server returns different numbers than the same file loaded
in-process — off by as much as 0.599 per component, never identical. The store
refuses to open against a drifted embedder, by design. See
[docs/EMBEDDER.md](docs/EMBEDDER.md).

**A pinned version number does not pin the computation.** Two installs of
`llama-cpp-python==0.3.25` on one machine produced different embeddings,
because one was a CUDA build and one was CPU-only. The binaries are the
identity, and `recollect doctor` reports their hashes.

**The mechanism constants are not tuning knobs.** Each value in
`EpisodicConfig` shaped a published research number. A store records the
config it was created under and refuses to reopen under a different one.
Deployment settings live separately, in `RecollectConfig`.

**Never fork `episodic` to add logging.** The whole verification argument
depends on the library being the untouched one. The single legal seam into it
is `engine/_internals.py`.

**The live instrument is coarse about quality.** Delivery counts, episode
identities, and character accounting are exact and reproduce every time.
Judgements about *answer quality* from single runs do not: the source research
measured a 3.0-point run-to-run band on a 13-point rubric across identical
inputs, and the runtime is not bit-reproducible.

## Tests

```bash
uv run pytest
```

The suite sweeps store sizes against budgets — including the silly ones: zero
characters, one character, and exactly the cost of an empty block — and
asserts byte equality between the library and the reconstruction every time.
It uses a deterministic fake embedder, so it needs no model file and runs in
about ten seconds.

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
would normally be a violation. It is not one here, because Idris Applied AI
Research holds the copyright in `episodic` and uses it under its own
commercial licence rather than under the AGPL.

That reasoning applies to the copyright holder and to nobody else. If you
obtain `episodic`, you get it under the AGPL — including section 13, which
reaches network use and not just distribution — unless you hold a separate
agreement. [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) has the full
chain, along with every other dependency and its terms.
