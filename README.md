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

## The architecture in one picture: three modes

Recollect runs in three modes, and they are deliberately different from each
other.

```
                          you
                           |
       +-------------------+-------------------+
       |                   |                   |
  MODE 1:             MODE 2:             MODE 3:
  conversation        delegated work      building a capability
  the main            a sandboxed         agents that write
  assistant           worker agent        Recollect's own code
       |                   |                   |
  episodic memory     task store +        frozen tests, a
  store (this         scratch files       candidate, a git
  conversation)       (thrown away)       commit
       |                   |                   |
       +-------------------+-------------------+
                           |
                  one local model
```

| | **Mode 1 — conversation** | **Mode 2 — delegated work** | **Mode 3 — building a capability** |
|---|---|---|---|
| What it is | The assistant you talk to | A background agent it hands jobs to | Agents that add a tool the worker is missing |
| Good at | Answering, recalling, deciding | Web research, multi-step work, producing files | Turning "I can't do that yet" into code that can |
| Where it runs | Inside the Recollect server | Inside a locked-down Docker container | In its own containers, one per agent |
| What it can reach | Your stored conversation | The internet and an empty scratch folder | A copy of the worker's own source code |
| How long it takes | Seconds | Minutes | Minutes to hours |
| What it remembers | Everything you actually said | Nothing, once the job ends | Nothing; what it keeps is a git commit |

Mode 1 is the only thing that owns memory. Mode 2 is a tool that Mode 1 picks
up, uses, and puts down. Keeping that line sharp is the point of the design:
if a worker's tool calls and internal chatter leaked into the conversation
memory, the memory would stop being a conversation and become a log file.

Mode 3 only starts when Mode 2 hits a wall and you say yes. It never touches
your memory either. What it changes is the worker's own toolbox.

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
from scratch. Two conditions let an episode in, and the block is everything
that satisfies either one:

| condition | label in the trace | what it contributes |
|---|---|---|
| relevance | **RELEVANT** | every episode in the whole store that matches the question closely enough |
| continuity | **RECENT** | the last 32 exchanges, whatever they scored |

A few details that matter:

- **RECENT is never dropped.** What you just said cannot be pushed out by a
  busy long-term search, because nothing here competes for room.
- **RELEVANT is a threshold, not a ranking.** Every episode is scored by
  meaning (vector cosine) against your question, and every one at or above
  0.48 is delivered — there is no top-N and no cutoff by size. The search runs
  over *every* episode in the store, not a recent slice, so something you said
  six months ago is as reachable as something you said this morning.
- **The two overlap, and that is the interesting part.** A recent episode that
  also clears the threshold is delivered once, not twice. When *everything*
  relevant was already recent, long-term memory contributed nothing that turn
  — the trace says so plainly, and that is a fact worth being able to see.
- **The mechanism has no budget.** Nothing is ranked, nothing is charged
  against an allowance, and nothing is dropped. The block is as large as the
  union needs to be, and the episodes arrive in the order they were said.
- **Recollect adds one ceiling anyway, and it is honest about it.** An
  uncapped block will eventually outgrow a local model's context window, and
  there is no other guard — the request simply fails. So a deployment limit
  (64,000 characters by default) decides how many episodes are handed to the
  library. This is a hardware constraint, not a claim about relevance: the
  research library caps nothing, deliberately. When the limit bites, the
  weakest-scoring episodes are held back first, what you just said is never
  held back at all, and the turn's trace says exactly what was withheld and
  why. Set `RECOLLECT_CONTEXT_CEILING_CHARS=0` to turn it off.

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
container never gets its own. (With three slots configured, building agents get
a lane of their own so a long build does not block your conversation.)

---

## Mode 3: building a capability it does not have

### When it happens

Sometimes a job needs something no tool can do — send a POST request, speak a
protocol nobody wrote a client for. The worker is required to say so in a
structured "capability gap" report rather than guess or work around it. Its
task pauses, and the assistant asks you a plain question: want me to build it?

Nothing is built without a yes. You can answer by voice, in chat, or with the
buttons on the task card.

### What happens after you say yes

1. **Tests first.** One agent writes the tests that define done, anchored on
   your original request. A second agent reviews them. They are frozen before
   any code exists, and nothing later is allowed to change them.
2. **Plan, review, write.** A third agent plans the change and a reviewer
   checks the plan — including whether the new tool can actually be called the
   way a model calls tools. Then the same agent writes the code.
3. **Prove it offline.** The frozen tests run in a container with **no
   network** and nothing of your machine mounted. A fresh reviewer reads the
   finished change.
4. **Prove it on the real request.** The new code becomes a second deployment,
   B. Your original request resumes there, with the new tool. Meanwhile your
   other work keeps running on the old one.
5. **Keep it.** Only if B actually finishes your request does the change get
   committed to the repository, on the branch you are already on. B becomes
   the deployment that serves everything.

If any step fails, B is thrown away — container, image and folders — and the
next attempt starts over from the unchanged original, told what went wrong,
including the errors the worker itself hit. Attempts continue until one works
or you stop it.

### What it is allowed to touch

Only the worker's own tree: its tool server, its research tools, its skills,
and new tool files. Its tool host, the package markers and the dependency lock
are off limits, so a build can add a tool but cannot pull in a new third-party
package. Any change outside that list is rejected before the code even runs.

### Undoing it

Every build tags the commit it started from, so one command puts the code back:

```bash
git reset --hard selfmod-before-<feature>-<timestamp>
```

Restart Recollect and the old toolbox is back. The full mechanism is in
**[docs/SELF_MODIFICATION.md](docs/SELF_MODIFICATION.md)**.

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
| Building a missing capability, and how to undo one | [docs/SELF_MODIFICATION.md](docs/SELF_MODIFICATION.md) |
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
  selfmod/            building a missing capability: tests, agents, A/B, commit
  engine/
    _internals.py     the single seam into the library's private modules
    shadow.py         the verified shadow trace
    embedder.py       the pinned in-process embedder
    generator.py      the chat client
    sandbox/          container isolation, attestation, and the worker runner
ui/                   Vite + React inspector
tests/                shadow-vs-library verification, swept over sizes and windows
evals/                live evaluation scripts, run by hand rather than in CI
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

The suite sweeps store sizes against continuity windows and thresholds —
including the silly ones: an empty store, a window of zero, a window larger
than the store, and a threshold nothing can clear — and asserts byte equality
between the library and the reconstruction every time.
It uses a deterministic fake embedder, so it needs no model file and needs
neither Docker nor a chat model. Tests that do want a real container are opt-in
behind a `docker` marker.

## Licence

**Dual licensed: AGPL-3.0-or-later, or a commercial licence.**
Copyright © 2026 Idris Applied AI Research.

| | AGPL-3.0-or-later | Commercial |
|---|---|---|
| Try it, read it, modify it, run it yourself | Yes, free | Yes |
| Deploy it or offer it over a network | Yes, **if** you release your complete source under the AGPL | Yes, with no source-release obligation |
| Ship it inside a closed-source product | No | Yes |

Unless you hold a separate agreement, you get Recollect under the
**GNU Affero General Public License v3 or later** ([`LICENSE`](LICENSE)). That
is a real grant: evaluate it, take it apart, run it, change it, redistribute
it. Section 13 is the catch — if users reach your modified version *over a
network*, you owe those users your complete corresponding source under the
AGPL. Merely never shipping a copy does not avoid it.

So: **trying it is free and needs no permission. Deploying it commercially,
without publishing what you built, is what the commercial licence is for.**

Full terms, the contribution policy, and how `episodic` fits:
[`LICENSING.md`](LICENSING.md). Enquiries:
**idrisappliedairesearch@gmail.com**.

`episodic` is dual licensed on the same terms by the same copyright holder, so
the two move together — take Recollect under the AGPL and `episodic` comes to
you under the AGPL. Every other dependency and its terms:
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
