"""The pinned embedder, in-process, with a memo cache.

**Embedding does not go over HTTP here, and that is not a preference.**
It was measured. Running the same GGUF behind ``llama-server`` and asking
for the same text returns a different vector than loading it in-process:
across 14 texts, cosine agreement 0.9996-0.9998 but a maximum component
difference of 0.224-0.599, with zero byte-equal results. Forcing the server
to CPU-only, unnormalized output, a single slot, ``-c 512 -t 1`` did not
close the gap. That divergence is the same magnitude as the batch-versus-
solo effect the library's ``CallShapeError`` gate was built to catch, and
the gate does catch it: a store built in-process refuses to reopen against
an HTTP embedder. Every committed retrieval number in the research came
from the in-process path, so that is the only path offered.

**Threads are the one tuning knob, and it was earned.** The library pins
``n_threads=1``. Holding the artifact, ``n_ctx=512``, ``n_gpu_layers=0``
and one-text-per-call fixed while varying only thread count produced
bit-identical vectors at 1, 2, 4, 8 and 16 threads across 21 texts, while
per-call latency fell from ~305ms to ~55ms at 8. Thread count is therefore
not part of the vector identity on this build. That is evidence from one
machine and one wheel, not proof - which is why nothing here relies on it
being true. The store re-embeds its sentinel on every open and refuses to
serve if the digest moved, so a wrong assumption fails loudly at startup
instead of quietly poisoning cosines.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
from episodic._embedding import (
    EMBEDDING_DIMENSION,
    SENTINEL_TEXT,
    PinnedEmbedder,
    vector_sha256,
)

#: The sentinel digest the carried artifact produces under the pinned call
#: shape. Committed in the research repository; asserted here at startup so
#: a mismatch is reported once, in plain language, rather than surfacing as
#: an unexplained store failure later.
EXPECTED_SENTINEL_SHA256 = (
    "baecf77627380f36f75a69c4454b064d886133f04255c5e5b4d3f24f00e7c4b8"
)

#: The context length the model is loaded with. Part of the pinned identity,
#: so it is a constant rather than a setting. Note the practical consequence:
#: an episode whose rendered pair text exceeds this is truncated by the
#: runtime before embedding, so very long turns are represented by their
#: opening tokens. That is inherited behaviour, not a choice made here.
PINNED_N_CTX = 512


class HarnessEmbedder(PinnedEmbedder):
    """``PinnedEmbedder`` with a thread count and a memo cache.

    Everything that participates in vector identity is inherited unchanged.
    The only override is the loader, and the only thing it changes is how
    many threads llama.cpp may use.
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        n_threads: int = 8,
        cache_size: int = 4_096,
    ) -> None:
        super().__init__(model_path)
        self.n_threads = int(n_threads)
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_size = int(cache_size)
        self._lock = threading.Lock()
        self._call_stats = threading.local()

        self.calls = 0
        self.cache_hits = 0
        self.last_latency_ms = 0.0
        self.last_cache_hit = False

    @property
    def last_latency_ms(self) -> float:
        return getattr(self._call_stats, "latency_ms", 0.0)

    @last_latency_ms.setter
    def last_latency_ms(self, value: float) -> None:
        self._call_stats.latency_ms = value

    @property
    def last_cache_hit(self) -> bool:
        return getattr(self._call_stats, "cache_hit", False)

    @last_cache_hit.setter
    def last_cache_hit(self, value: bool) -> None:
        self._call_stats.cache_hit = value

    def _get_model(self):
        """Load the pinned artifact, varying only thread count.

        Mirrors ``PinnedEmbedder._get_model`` exactly apart from
        ``n_threads``/``n_threads_batch``. Kept as an explicit copy rather
        than a super() call because every argument here is part of the
        embedding identity and should be readable in one place.
        """
        if self._model is None:
            try:
                from llama_cpp import Llama
            except ImportError as error:  # pragma: no cover - environment
                raise RuntimeError(
                    "The pinned embedder needs llama-cpp-python==0.3.25. "
                    "It is a compiled package: install the prebuilt wheel "
                    "rather than building from source."
                ) from error
            self._model = Llama(
                model_path=str(self.model_path),
                embedding=True,
                n_gpu_layers=0,
                n_ctx=PINNED_N_CTX,
                n_threads=self.n_threads,
                n_threads_batch=self.n_threads,
                verbose=False,
            )
        return self._model

    def __call__(self, text: str) -> np.ndarray:
        """One text, one call. Repeat texts are served from memory."""
        with self._lock:
            hit = self._cache.get(text)
            if hit is not None:
                self._cache.move_to_end(text)
                self.cache_hits += 1
                self.last_cache_hit = True
                self.last_latency_ms = 0.0
                return hit.copy()

            # A Llama instance owns mutable native context and batch buffers.
            # Keep initialization, inference and cache publication in one lease.
            started = time.perf_counter()
            computed = np.asarray(
                self._get_model().embed(text), dtype=np.float32
            ).reshape(EMBEDDING_DIMENSION)
            elapsed = (time.perf_counter() - started) * 1_000.0

            self.calls += 1
            self.last_cache_hit = False
            self.last_latency_ms = elapsed
            self._cache[text] = computed
            self._cache.move_to_end(text)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return computed.copy()

    # -- startup checks ----------------------------------------------------

    def runtime_fingerprint(self) -> dict:
        """Hash the native libraries actually loaded, not the version string.

        This exists because of a failure worth remembering. Two installs of
        ``llama-cpp-python==0.3.25``, same machine, same model file, same
        Python code, produced different embeddings: sentinel ``baecf776…``
        against ``f40dfff9…``, vector norm 82.514 against 82.071. The
        version string was identical and every DLL underneath it was not -
        one install was a CUDA-enabled build carrying ``ggml-cuda.dll``,
        the other a CPU-only build without it. The difference showed up
        even at ``n_gpu_layers=0``.

        A pinned version number therefore does not pin the computation. The
        binaries do. Reporting them turns "sentinel drifted, cause unknown"
        into "you are running a different build", which is a five-minute
        fix instead of an afternoon.
        """
        try:
            import llama_cpp as llama_cpp_module
        except ImportError:
            return {"package_version": "not installed", "libraries": {}}

        library_dir = Path(llama_cpp_module.__file__).parent / "lib"
        libraries: dict[str, str] = {}
        if library_dir.is_dir():
            for path in sorted(library_dir.glob("*.dll")) + sorted(
                library_dir.glob("*.so")
            ) + sorted(library_dir.glob("*.dylib")):
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                libraries[path.name] = digest.hexdigest()
        return {
            "package_version": getattr(llama_cpp_module, "__version__", "unknown"),
            "libraries": libraries,
            "cuda_backend_present": any(
                "cuda" in name.lower() for name in libraries
            ),
        }

    def warm_up(self) -> dict:
        """Load the model and verify it is the artifact the research used.

        Called once at startup so the ~750ms cold load and any identity
        failure both happen before the first user is waiting on a reply.
        Returns the observed digests for the trace and the logs.
        """
        started = time.perf_counter()
        sentinel = self(SENTINEL_TEXT)
        observed = vector_sha256(sentinel)
        load_ms = (time.perf_counter() - started) * 1_000.0

        drifted = observed != EXPECTED_SENTINEL_SHA256
        return {
            "sentinel_sha256": observed,
            "sentinel_matches_research": not drifted,
            "expected_sentinel_sha256": EXPECTED_SENTINEL_SHA256,
            "model_path": str(self.model_path),
            "n_threads": self.n_threads,
            "n_ctx": PINNED_N_CTX,
            "cold_load_ms": load_ms,
            "embedding_dimension": EMBEDDING_DIMENSION,
        }

    @property
    def stats(self) -> dict:
        total = self.calls + self.cache_hits
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "cache_entries": len(self._cache),
            "hit_ratio": (self.cache_hits / total) if total else 0.0,
        }
