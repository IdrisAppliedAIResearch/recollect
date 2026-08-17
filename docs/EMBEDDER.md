# The embedder is pinned at the binary level

Read this before changing anything about how embeddings are produced.

Every cosine in this system compares a query vector against vectors stored
months earlier. That comparison is only meaningful if both were produced by
the same computation. "The same computation" turns out to be a much
narrower thing than it sounds, and this document records the three ways it
has already been observed to break.

---

## 1. HTTP and in-process are different computations

The obvious deployment choice — run the GGUF behind `llama-server` and call
`/v1/embeddings` — does not work here, and not for a reason that can be
configured away.

Measured on this machine, same artifact, same text:

| | in-process | `/v1/embeddings` | native `/embedding` |
|---|---|---|---|
| sentinel sha256 | `baecf776…` | `668c6e94…` | `49b08dba…` |
| cosine to in-process | — | 0.9997598 | 0.9997719 |
| max component difference | — | 0.2219 | 0.2370 |

Note that the two endpoints on the *same server* also disagree with each
other. Forcing the server to match the in-process settings as closely as
the CLI allows — `--embd-normalize -1 --device none -ngl 0 -c 512 -b 512
-ub 512 -t 1 -tb 1 --parallel 1 --cache-ram 0` — did not close the gap:
still no byte-equal result, and a **maximum component difference of 0.2690
with both vectors raw and unnormalized**, so normalization is not the
explanation.

Across 14 texts: cosine 0.9995959–0.9997610, max component difference
0.224–0.599, **0 of 14 byte-equal**.

For scale, the effect that motivated the library's `CallShapeError` gate in
the first place — the same text embedded alone versus inside a batch — was
cosine 0.999837 with a component difference of 0.217, and it was enough to
flip committed selection payloads. The HTTP gap is the same size or larger.

**The gate catches it.** A store built in-process and reopened against an
HTTP embedder raises:

```
CallShapeError: Embedding call-shape sentinel drifted: the sentinel text no
longer embeds to the vector stored with this store (stored baecf7762738...,
observed 044c47a71680...)
```

HTTP is not a latency win either: 68–143 ms for distinct texts against
~55 ms in-process at 8 threads.

**Consequence:** `recollect.engine.embedder.HarnessEmbedder` loads the
model in-process. There is no HTTP embedding path and adding one would
invalidate every stored vector.

---

## 2. A pinned version number does not pin the computation

This one cost real time, and it is the reason `doctor` reports library
hashes.

Two virtual environments on the same machine, both with
`llama-cpp-python==0.3.25`, the same 639,150,592-byte model file, and the
same Python code, produced **different embeddings**:

| | research venv | a fresh install |
|---|---|---|
| sentinel sha256 | `baecf77627380f36…` | `f40dfff907da3b72…` |
| vector L2 norm | 82.51444 | 82.07051 |

The cause: every native library underneath differed.

```
ggml-base.dll  643408b0b61a626c  vs  56d68fa0b7b26bdc   DIFFERENT
ggml-cpu.dll   90d33fac8031bf6d  vs  ded444a3eb0ed627   DIFFERENT
ggml.dll       6fd888f082f69f0f  vs  5764ef2ca3457eb8   DIFFERENT
llama.dll      bd94277bfed432a6  vs  8cfbda457bae9def   DIFFERENT
mtmd.dll       1aacf4aad3d62118  vs  5305cef5ddc5e6fe   DIFFERENT
ggml-cuda.dll  present (169 MB)  vs  absent
```

One install was a CUDA-enabled build, the other CPU-only — and the
difference showed up **even at `n_gpu_layers=0`**, where no layer is
offloaded to the GPU at all.

PyPI publishes no binary wheel for this package, so `pip install` and
`uv pip install` both resolve to whatever build the local cache or index
happens to hold. That is not a reproducible pin.

**Consequence:** the working install must carry the CUDA-enabled build.
`HarnessEmbedder.runtime_fingerprint()` hashes the loaded libraries and
`recollect doctor` prints them, so this failure reports itself as "you are
running a different build" rather than as an unexplained sentinel drift.

To install the correct build, copy the package from a known-good
environment rather than resolving it:

```bash
cp -r <known-good-venv>/Lib/site-packages/llama_cpp <target-venv>/Lib/site-packages/
```

---

## 3. Thread count is *not* part of the identity — and that was worth checking

The library pins `n_threads=1`. Holding everything else fixed — the
artifact, `n_ctx=512`, `n_gpu_layers=0`, one text per call — and varying
only the thread count produced **bit-identical vectors**:

| `n_threads` | warm mean | bit-identical to t=1 |
|---|---|---|
| 1 | 304.9 ms | — |
| 2 | 163.3 ms | yes (0/21 mismatch) |
| 4 | 89.2 ms | yes (0/21 mismatch) |
| 8 | **55.3 ms** | yes (0/21 mismatch) |
| 16 | 57.0 ms | yes (0/21 mismatch) |

Eight threads is the knee, and a 5.5× latency reduction is the difference
between an embedder that sits comfortably in an interactive request path
and one that does not.

This is evidence from 21 texts on one machine and one build, not proof.
Nothing in the harness depends on it being true: the store re-embeds its
sentinel on **every open** and refuses to serve if the digest moved. A
wrong assumption here fails at startup rather than quietly poisoning
cosines.

---

## What is fixed, and what may be tuned

| pinned — changing it changes every vector | tunable |
|---|---|
| the model artifact and its SHA-256 | `n_threads` / `n_threads_batch` |
| the native library binaries | cache size |
| `n_ctx = 512` | |
| `n_gpu_layers = 0` | |
| one text per call ("solo" call shape) | |
| the `User: …\nAssistant: …` pair format | |

### A note on `n_ctx = 512`

This is inherited and it has a practical consequence worth stating: an
episode whose rendered pair text exceeds 512 tokens is truncated by the
runtime before embedding, so a very long turn is represented by its opening
tokens only. That is the behaviour every committed research number was
produced under. Raising it would change every vector, so it is pinned — but
it is a real limitation on long turns rather than a neutral default.

---

## The identity, in one line

```
baecf77627380f36f75a69c4454b064d886133f04255c5e5b4d3f24f00e7c4b8
```

That is the SHA-256 of the float32 bytes of the sentinel string
`episodic call-shape sentinel: one text per call`. `recollect doctor`
recomputes it at startup. If it matches, stored and fresh vectors live in
the same space. If it does not, nothing downstream is trustworthy and the
harness says so instead of continuing.
