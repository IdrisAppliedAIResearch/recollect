# Working in this repository

## What this project is

Recollect deploys a researched conversational-memory mechanism as a usable
product. The mechanism lives in the `episodic` library
(`../contextDecayWindow/episodic`) and is consumed **unmodified**. This
repository adds the turn loop, the instrumentation, the server, and the UI.

Read [README.md](README.md) for the architecture and
[docs/EMBEDDER.md](docs/EMBEDDER.md) before touching anything that produces
a vector.

## Rules that are not style preferences

**Never fork or patch `episodic`.** It is certified behavior-preserving
against committed artifacts; a copy with logging in it is no longer that
thing, and every published number would need re-earning. If you need
internals, the seam is `engine/_internals.py` and the safety net is the
shadow verification.

**Never weaken the verification.** `engine/shadow.py` computes each turn
twice and raises `TraceDivergenceError` when the two disagree. If a change
makes that fire, the change is wrong or the instrumentation is stale — do
not add a tolerance, do not catch it, and do not switch the server to
`strict=False`.

**Never add an HTTP embedding path.** Measured: it returns different
vectors, and the store's gate refuses to open against it. See
docs/EMBEDDER.md §1.

**Keep mechanism constants out of deployment config.** `EpisodicConfig`
values each shaped a committed research number. `RecollectConfig` holds
machine-specific choices. Do not move a field across that line for
convenience.

**Distinguish counts from scores.** Delivery counts, episode identities and
character accounting are exact and reproduce. Judgements about answer
quality from single runs do not — the source research measured a 3.0-point
band on a 13-point rubric across byte-identical replicates, and the runtime
is not bit-reproducible. Do not claim a quality improvement from one run.

## Conventions

- Python 3.13, `uv` for everything. `uv sync`, `uv run pytest`, `uv run ruff check`.
- Line length 88, ruff-enforced.
- Pydantic models for anything crossing the HTTP boundary; the OpenAPI
  schema is what the UI's types are derived from.
- Blocking work (embedding, retrieval, SQLite) runs via `asyncio.to_thread`.
  Do not call it directly on the event loop.
- Traces are files, not rows: `var/sessions/<id>/traces/<turn>.json`. A
  record you can open in a text editor later is worth more than one that
  needs the app running.

## Comments

Explain *why*, especially where the code encodes a measured finding rather
than a preference — those are the comments that stop someone "simplifying"
a constraint back out. Do not narrate what the code plainly does. Do not
leave changelog commentary in source; that is what git is for.

## Testing

`uv run pytest` runs without a model file, using a deterministic fake
embedder — the shadow-vs-library property is about the pipeline, not about
any particular vectors. Keep it that way; a suite that needs a 639 MB
artifact stops being run.

When adding a mechanism-adjacent feature, sweep the degenerate budgets
(0, 1, `EMPTY_PAYLOAD_CHARS - 1`, `EMPTY_PAYLOAD_CHARS`) as well as the
comfortable ones. Every off-by-one in this system has surfaced there.

## Running it

```bash
uv run recollect doctor   # embedder identity, store gate, generator reachability
uv run recollect serve
uv run recollect chat
```

A chat model must be running separately. llama-server is preferred over
Ollama: it reports the prefill and cache timings the trace records, and it
does not idle-unload and stall a session by ~30s.
