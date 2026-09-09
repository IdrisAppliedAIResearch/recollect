"""Recollect - a deployable harness for episodic conversational memory.

The mechanism lives in the ``episodic`` library and is consumed here
unmodified. What this package adds is everything needed to *use* it as a
product and to *see* it while it runs: an instrumented turn loop that
proves its own account of what happened, a session store, an
OpenAI-compatible server, and a trace schema rich enough that the whole
architecture is legible from a single turn.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import RecollectConfig
    from .trace import TurnSummary, TurnTrace

__version__ = "0.1.0"

__all__ = ["RecollectConfig", "TurnTrace", "TurnSummary", "__version__"]


def __getattr__(name: str):
    # A UI client must import without the desktop's research/model packages.
    if name == "RecollectConfig":
        from .config import RecollectConfig

        return RecollectConfig
    if name in {"TurnTrace", "TurnSummary"}:
        from . import trace

        return getattr(trace, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
