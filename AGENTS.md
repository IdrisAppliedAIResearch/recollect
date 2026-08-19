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
rebuilds a small context window from stored episodes. Three paths compete
for a fixed character budget: **RECENT** (last N), **RELATED** (cosine over
a threshold), **SPREAD** (topic-diversity greedy). They pack in that order
against a hard ceiling.

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
uv run ruff check . && uv run pytest
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
ui/src/             React inspector (7 tabs)
tests/              shadow-vs-library sweep
docs/EMBEDDER.md    read before touching anything that makes a vector
```

Rough rule: if your change makes the UI show a *new number*, it probably
belongs in `trace.py` and `shadow.py` first, and the UI last.

---

## 7. Commands

```bash
uv sync                      # install (needs ../contextDecayWindow checked out)
uv run pytest                # no model file needed; ~10s
uv run ruff check .          # line length 88
uv run recollect doctor      # embedder identity + generator reachability
uv run recollect serve       # UI + API on :8080
```

A chat model must be running separately on port 8000 (llama-server
preferred — it reports the cache timings the trace records).

Notes that save time:

- Tests use a **fake embedder** and need no model file. Keep it that way; a
  suite requiring a 639 MB download stops being run.
- When touching packing or budgets, test the degenerate values too: `0`,
  `1`, `EMPTY_PAYLOAD_CHARS - 1`, `EMPTY_PAYLOAD_CHARS`. Every off-by-one in
  this system has surfaced there.
- Blocking work (embedding, SQLite, retrieval) must run via
  `asyncio.to_thread`, never directly on the event loop.
- `uv.lock` is generated. Change dependencies in `pyproject.toml` and run
  `uv sync` — never hand-edit the lock.

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
