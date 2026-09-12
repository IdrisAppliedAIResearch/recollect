# Third-party and separately licensed components

Recollect is dual licensed — AGPL-3.0-or-later ([`LICENSE`](LICENSE)) or a
commercial licence ([`LICENSING.md`](LICENSING.md)). This file records what
Recollect depends on that neither of those documents covers, because those
components are governed by their own terms.

Read this before assuming that a right you have in Recollect is also a right
you have in everything it loads. Some of it is not distributed here at all.

---

## 1. `episodic` — owned here, published in its own repository

`episodic` is the memory mechanism library. Recollect consumes it unmodified as
a path dependency from the sibling
[contextDecayWindow](https://github.com/IdrisAppliedAIResearch/contextDecayWindow)
repository. It is **dual licensed** there on the same terms as Recollect:
**AGPL-3.0-or-later, or a commercial licence** from Idris Applied AI Research.

Because both works are dual licensed by the same copyright holder under the
same two options, they move together and there is nothing to reconcile:

- Take Recollect under the **AGPL**, and `episodic` reaches you under the AGPL.
  An AGPL work building on an AGPL library is the ordinary case.
- Take Recollect under a **commercial licence**, and that agreement covers
  `episodic` too. You do not negotiate the two separately.

**What this means for you.** Your obligations under the AGPL — including
section 13, which reaches network use and not only distribution — apply to the
combined work, not to Recollect's own source alone. If you deploy a modified
Recollect and users reach it over a network, the complete corresponding source
you owe them includes `episodic`.

Commercial terms for either or both: idrisappliedairesearch@gmail.com.

## 2. Python dependencies

The base dependencies listed below are permissively licensed. The optional
speech stack has additional terms recorded separately below.

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
`trafilatura` floor of `>=2.2.0` remains for feature reasons; versions before
1.8 were GPL-3.0, which under Recollect's own AGPL-3.0-or-later grant is now a
compatible licence rather than a conflict.

Under the AGPL, the permissive licences above (MIT, BSD, Apache-2.0) are
one-way compatible: those components may be combined into an AGPL-3.0 work, and
their notices must be preserved. This table is that preservation.

### Optional local speech (`voice` extra)

| Component | Licence |
|---|---|
| [Vosk API](https://github.com/alphacep/vosk-api) | Apache-2.0 |
| [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) | MIT |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | MIT |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (optional `voice-whisper`) | MIT |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) (optional `voice-whisper`) | MIT |
| [espeakng-loader wrapper](https://github.com/thewh1teagle/espeakng-loader/blob/main/LICENSE) | MIT |
| [Phonemizer](https://github.com/bootphon/phonemizer#licence) | GPL-3.0-or-later |
| [eSpeak NG library and data bundled by espeakng-loader](https://github.com/espeak-ng/espeak-ng#license-information) | GPL-3.0-or-later, with separately noted BSD components |

Kokoro's default text-to-phoneme path imports Phonemizer and loads eSpeak NG
in-process. These GPL-3.0-or-later components are part of the optional installed
runtime; the MIT licence of the Kokoro wrapper does not replace their terms.

Recollect's AGPL-3.0-or-later grant is compatible with them: AGPLv3 section 13
expressly permits combination with GPLv3 works, and "or later" on both sides
leaves no version gap. A **commercial** licence for Recollect does not extend to
these components, and does not authorize distributing them under non-GPL terms.
If you take Recollect commercially and intend to redistribute the voice extra,
resolve Phonemizer and eSpeak NG separately — or use a text-to-phoneme path that
does not load them.

The Windows `voice-whisper` extra also installs NVIDIA cuBLAS, cuDNN and their
CUDA runtime dependencies. Those binaries retain the NVIDIA licence terms
included in their packages; Recollect's licence does not cover them. PyAV and
its bundled FFmpeg components also retain their own package notices.

## 3. Frontend dependencies

The inspector UI (`ui/`) builds on React, Vite, TypeScript, and
`@tanstack/react-table`. The resolved dependency tree is MIT, Apache-2.0,
MPL-2.0, ISC, and BSD-3-Clause. `ui/package.json` is marked `private` and
`AGPL-3.0-or-later`, matching the repository; it is not published to npm.

## 4. Model weights

The embedding model — the carried GGUF loaded through llama.cpp — is licensed by
its publisher and is **not** distributed with this software. Obtain it from its
own source under its own terms. `docs/EMBEDDER.md` records which build is part
of the runtime identity.

The optional `recollect voice-setup` command downloads these speech assets
directly from their publishers into the ignored runtime model directory; they
are not committed to this repository:

| Asset | Publisher / source | Licence |
|---|---|---|
| `vosk-model-small-en-us-0.15` | [Alpha Cephei model catalogue](https://alphacephei.com/vosk/models) | Apache-2.0 |
| Kokoro 82M v1.0 ONNX and voice vectors | [thewh1teagle ONNX export](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0), from [hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) | Apache-2.0 |
| Silero VAD v6.2.1 ONNX | [snakers4/silero-vad, pinned commit](https://github.com/snakers4/silero-vad/tree/7e30209a3e901f9842f81b225f3e93d8199902b1) | [MIT](https://github.com/snakers4/silero-vad/blob/7e30209a3e901f9842f81b225f3e93d8199902b1/LICENSE) |
| Whisper large-v3-turbo CTranslate2 conversion | [Pinned conversion](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo/tree/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf), from [OpenAI Whisper](https://github.com/openai/whisper) | MIT |

---

*If you believe something here is inaccurate or incomplete, that is worth
reporting: idrisappliedairesearch@gmail.com.*
