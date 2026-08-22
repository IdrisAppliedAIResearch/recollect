# Third-party and separately licensed components

Recollect itself is proprietary — see [`LICENSE`](LICENSE). This file records
what Recollect depends on that `LICENSE` does **not** cover, because those
components are governed by their own terms.

Read this before assuming that a right you have in a dependency is also a right
you have in Recollect. It is not.

---

## 1. `episodic` — owned here, published under different terms

This is the one that needs explaining, because on its face it looks like a
contradiction.

`episodic` is the memory mechanism library. Recollect consumes it unmodified as
a path dependency from the sibling
[contextDecayWindow](https://github.com/IdrisAppliedAIResearch/contextDecayWindow)
repository. It is **dual licensed** there: **AGPL-3.0-or-later, or a commercial
licence** from Idris Applied AI Research.

The AGPL is a strong copyleft licence, and a proprietary product built on
AGPL-licensed code would ordinarily be a licence violation. It is not one here,
for a specific reason:

> Idris Applied AI Research holds the copyright in `episodic` and publishes it
> under two licences. Recollect uses it under the **commercial** licence, not
> under the AGPL. A copyright holder may license its own work to itself on any
> terms it chooses; the AGPL binds everyone who receives the library under the
> AGPL, and Recollect did not.

This is written down rather than left to inference precisely because Recollect
is a public repository. An outside reader seeing a proprietary product import
AGPL code should be able to find the resolution here instead of assuming a
violation.

**What this means for you.** Nothing in Recollect's `LICENSE` gives you any
right in `episodic`, and nothing in this note extends the commercial licence to
you. If you obtain `episodic`, you obtain it under the AGPL unless you hold a
separate commercial agreement. Your obligations under the AGPL — including
section 13, which reaches network use and not only distribution — are unaffected
by anything in this repository.

If you want `episodic` under commercial terms, that is a real product and it is
available: idrisappliedairesearch@gmail.com. See `LICENSING.md` in the
contextDecayWindow repository.

## 2. Python dependencies

All are permissively licensed. None imposes a copyleft obligation on Recollect.

| Component | Licence |
|---|---|
| `numpy` | BSD-3-Clause (with bundled 0BSD, MIT, Zlib components) |
| `pydantic` | MIT |
| `fastapi` | MIT |
| `uvicorn` | BSD-3-Clause |
| `httpx` | BSD-3-Clause |
| `python-dotenv` | BSD-3-Clause |
| `trafilatura` | Apache-2.0 |
| `mcp` | MIT |
| `llama-cpp-python` (optional `llama` extra) | MIT |

Two notes on the transitive tree. `certifi` is MPL-2.0 and `tld` is tri-licensed
MPL-1.1 / GPL-2.0-only / LGPL-2.1-or-later; both are file-level or weak copyleft
that is satisfied by using them unmodified, which is what happens here. The
`trafilatura` floor of `>=2.2.0` is load-bearing for more than features —
versions before 1.8 were GPL-3.0, and pinning below that floor would pull a
copyleft obligation into a proprietary product.

## 3. Frontend dependencies

The inspector UI (`ui/`) builds on React, Vite, TypeScript, and
`@tanstack/react-table`. The resolved dependency tree is MIT, Apache-2.0,
MPL-2.0, ISC, and BSD-3-Clause. `ui/package.json` is marked `private` and
`UNLICENSED`; it is not published to npm.

## 4. Model weights

The embedding model — the carried GGUF loaded through llama.cpp — is licensed by
its publisher and is **not** distributed with this software. Obtain it from its
own source under its own terms. `docs/EMBEDDER.md` records which build is part
of the runtime identity.

---

*If you believe something here is inaccurate or incomplete, that is worth
reporting: idrisappliedairesearch@gmail.com.*
