"""The instrumented turn loop.

``shadow`` computes the context block twice - once through the library
untouched, once through an instrumented reconstruction - and refuses to
serve a trace whose two accounts disagree. ``embedder`` holds the pinned
in-process embedder. ``_internals`` is the single seam where this package
reaches into the library's private modules, and explains why.
"""

from .embedder import HarnessEmbedder
from .shadow import RetrievalResult, TraceDivergenceError, retrieve_with_trace

__all__ = [
    "HarnessEmbedder",
    "RetrievalResult",
    "TraceDivergenceError",
    "retrieve_with_trace",
]
