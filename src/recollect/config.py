"""Harness configuration.

Two configs exist and they are deliberately separate.

``EpisodicConfig`` (from the library) holds the *mechanism* constants -
window size, threshold, selector parameters, embedder identity. Every one
of those values shaped a committed research number, the store records the
config it was created under, and reopening under a different one is an
error. Nothing here may quietly change them.

``RecollectConfig`` (this module) holds the *deployment* choices - where
the model file lives, which generator to talk to, where to put data. These
are machine-specific and safe to vary. Keeping them apart is what stops a
deployment convenience from silently becoming a mechanism change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv
from episodic import EpisodicConfig

#: The deployed context budget, in characters. This is the value the studies
#: ran at; the library enforces it as a hard ceiling with no tolerance.
DEFAULT_BUDGET_CHARS = 32_000

#: Threads for the in-process embedder. Measured bit-identical to the pinned
#: single-threaded output across 1/2/4/8/16 threads on 21 texts, while cutting
#: per-call latency from ~305ms to ~55ms. Thread count is not part of the
#: vector identity on this build; the store's sentinel gate re-checks that
#: claim on every open, so a wrong assumption here fails loudly.
DEFAULT_EMBEDDING_THREADS = 8

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant with a long-term episodic memory.\n\n"
    "Before each reply you are given two blocks. <recent_context> holds the "
    "most recent exchanges in order. <retrieved_stm> holds older exchanges "
    "that were retrieved because they may bear on what was just asked. Both "
    "are drawn from your own earlier conversation with this user.\n\n"
    "Treat them as your memory, not as documents: do not mention the blocks, "
    "do not cite turn numbers, and do not say that something was retrieved. "
    "If the blocks do not contain what you need, say you do not recall it "
    "rather than inventing a memory.\n\n"
    "The run_subagent tool delegates a self-contained task to an autonomous "
    "subagent. It works the task in its own context, with open-web and "
    "scholarly research tools and a scratch workspace, and returns a compact "
    "result with sources that you then answer from. Use it for current or "
    "external research and genuinely sustained multi-step work. Do not "
    "delegate ordinary reasoning, writing, or work answerable from this "
    "conversation. Keep the task brief and self-contained: what to do, and "
    "what a good result looks like. Mark narrow lookups and bounded tasks as "
    "focused; reserve deep effort for substantial multi-source work."
)


def _default_sandbox_root() -> Path:
    """The default sandbox workdir root: machine-local, outside any tree."""
    override = os.environ.get("RECOLLECT_SANDBOX_ROOT")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(
            Path.home() / ".local" / "share"
        )
    return Path(base) / "recollect" / "sandboxes"


@dataclass(frozen=True)
class RecollectConfig:
    """Deployment settings. Mechanism constants live in ``EpisodicConfig``."""

    # -- embedding (in-process, never over HTTP) --------------------------
    embedding_model_path: Path
    embedding_threads: int = DEFAULT_EMBEDDING_THREADS

    # -- generation (HTTP, OpenAI-compatible) ------------------------------
    generator_base_url: str = "http://127.0.0.1:8000/v1"
    generator_model: str = "local"
    generator_api_key: str = "not-needed"
    generator_timeout_s: float = 300.0
    #: The carried models route chain-of-thought into a separate field and
    #: leave `content` empty while thinking. Off by default so a first
    #: conversation returns visible text.
    generator_thinking: bool = False
    #: The 1024 cap truncated real replies (finish_reason "length"); the
    #: generator's context is 32k tokens, so 4096 leaves headroom.
    generator_max_tokens: int = 4_096
    generator_temperature: float = 0.7

    # -- memory -------------------------------------------------------------
    budget_chars: int = DEFAULT_BUDGET_CHARS
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    episodic: EpisodicConfig = field(default_factory=EpisodicConfig)
    #: Deployment choice (D1, locked 2026-08-25): the protected static
    #: ASPECT spread runs by default here, so the base dependency carries
    #: the [aspect] extra. It overrides the library's own aspect_enabled,
    #: which defaults off; a store created under an explicit config keeps
    #: pinning it at first open.
    aspect_enabled: bool = True

    # -- subagent ---------------------------------------------------------------
    # Deployment bounds for the ephemeral subagent. These cap cost,
    # not quality: a run that hits any of them stops with a *partial* result
    # rather than an empty one, so a loose value reads as a longer answer,
    # never as a broken server.
    subagent_enabled: bool = True
    subagent_max_steps: int = 8
    subagent_max_tool_calls: int = 8
    subagent_observation_chars: int = 4_000
    subagent_max_tokens: int = 1_024

    # -- subagent backends ------------------------------------------------------
    # "legacy" runs the in-harness agent loop (engine/subagent.py);
    # "opencode" runs the task in a globally shared sandboxed opencode
    # server (engine/sandbox). Both emit the same SubagentStep /
    # SubagentResult shapes, so the turn pipeline and the one-line trace
    # are identical either way.
    subagent_backend: str = "legacy"
    # Limits for the opencode backend. The step cap is handed to opencode
    # (it forces a text-only final pass at the cap) and is the run's only
    # bound: there is no client-side wallclock, so a long research pass is
    # never killed mid-flight.
    sandbox_steps: int = 24
    # A sandbox idle this long with no running delegation is shut down.
    # The server process may stay warm between calls, but every call gets a
    # new OpenCode session and a scrubbed workspace.
    sandbox_idle_ttl_s: float = 1_800.0
    #: Production OpenCode runs only inside this container image. There is
    #: intentionally no host-process fallback: OpenCode permissions are
    #: defense in depth, not an operating-system security boundary.
    sandbox_container_runtime: str = "docker"
    sandbox_container_image: str = "recollect-opencode-sandbox:1.18.18"
    sandbox_container_memory_mb: int = 1_024
    sandbox_container_pids: int = 256
    sandbox_container_cpus: float = 2.0

    #: Root for the shared sandbox workdir (opencode backend). This must
    #: sit outside any git repository: opencode scopes "the project" to
    #: the enclosing repo root, so a sandbox inside the recollect repo
    #: could read and edit this entire codebase, unfenced by any
    #: permission (the repo *is* the project). Also kept off ``data_dir``
    #: so a machine-local path cannot drag the repo into it.
    sandbox_root: Path = field(default_factory=_default_sandbox_root)

    # -- storage / server ---------------------------------------------------
    data_dir: Path = Path("var")
    host: str = "127.0.0.1"
    port: int = 8080

    def __post_init__(self) -> None:
        if self.budget_chars < 0:
            raise ValueError("budget_chars must be non-negative")
        if self.generator_max_tokens < 1:
            raise ValueError("generator_max_tokens must be positive")
        if self.embedding_threads < 1:
            raise ValueError("embedding_threads must be positive")
        if self.subagent_max_steps < 1:
            raise ValueError("subagent_max_steps must be positive")
        if self.subagent_max_tool_calls < 1:
            raise ValueError("subagent_max_tool_calls must be positive")
        if self.subagent_observation_chars < 1:
            raise ValueError("subagent_observation_chars must be positive")
        if self.subagent_max_tokens < 1:
            raise ValueError("subagent_max_tokens must be positive")
        if self.subagent_backend not in ("legacy", "opencode"):
            raise ValueError("subagent_backend must be 'legacy' or 'opencode'")
        if self.sandbox_steps < 1:
            raise ValueError("sandbox_steps must be positive")
        if self.sandbox_idle_ttl_s <= 0:
            raise ValueError("sandbox_idle_ttl_s must be positive")
        if not self.sandbox_container_runtime.strip():
            raise ValueError("sandbox_container_runtime must be non-empty")
        if not self.sandbox_container_image.strip():
            raise ValueError("sandbox_container_image must be non-empty")
        if self.sandbox_container_memory_mb < 128:
            raise ValueError("sandbox_container_memory_mb must be at least 128")
        if self.sandbox_container_pids < 16:
            raise ValueError("sandbox_container_pids must be at least 16")
        if self.sandbox_container_cpus <= 0:
            raise ValueError("sandbox_container_cpus must be positive")
        if not isinstance(self.aspect_enabled, bool):
            raise ValueError("aspect_enabled must be a boolean")
        # The deployment owns switch on or off; the mechanism constants stay
        # frozen. Frozen dataclass, hence the setattr.
        object.__setattr__(
            self,
            "episodic",
            replace(self.episodic, aspect_enabled=self.aspect_enabled),
        )

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    def store_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "episodes.sqlite"

    def traces_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "traces"

    @classmethod
    def from_env(cls, *, env_file: str | Path | None = ".env") -> RecollectConfig:
        """Build from environment, loading a .env file if one is present."""
        if env_file is not None and Path(env_file).is_file():
            load_dotenv(env_file)

        model_path = os.environ.get("RECOLLECT_EMBEDDING_MODEL_PATH")
        if not model_path:
            # Fall back to the research repository's variable so an existing
            # machine works without a new .env.
            model_path = os.environ.get("CDW_EMBEDDING_MODEL_PATH")
        if not model_path:
            raise ValueError(
                "Set RECOLLECT_EMBEDDING_MODEL_PATH to the carried "
                "Qwen3-Embedding-0.6B Q8_0 GGUF. The harness embeds "
                "in-process against that exact artifact; there is no "
                "supported HTTP embedding path."
            )

        return cls(
            embedding_model_path=Path(model_path),
            embedding_threads=int(
                os.environ.get(
                    "RECOLLECT_EMBEDDING_THREADS", DEFAULT_EMBEDDING_THREADS
                )
            ),
            generator_base_url=os.environ.get(
                "RECOLLECT_GENERATOR_BASE_URL", "http://127.0.0.1:8000/v1"
            ),
            generator_model=os.environ.get("RECOLLECT_GENERATOR_MODEL", "local"),
            generator_api_key=os.environ.get(
                "RECOLLECT_GENERATOR_API_KEY", "not-needed"
            ),
            generator_thinking=_flag(os.environ.get("RECOLLECT_GENERATOR_THINKING")),
            generator_max_tokens=int(
                os.environ.get("RECOLLECT_GENERATOR_MAX_TOKENS", 4_096)
            ),
            budget_chars=int(
                os.environ.get("RECOLLECT_BUDGET_CHARS", DEFAULT_BUDGET_CHARS)
            ),
            aspect_enabled=_flag(os.environ.get("RECOLLECT_ASPECT_ENABLED", "1")),
            data_dir=Path(os.environ.get("RECOLLECT_DATA_DIR", "var")),
            host=os.environ.get("RECOLLECT_HOST", "127.0.0.1"),
            port=int(os.environ.get("RECOLLECT_PORT", 8080)),
            subagent_enabled=_flag(
                os.environ.get("RECOLLECT_SUBAGENT_ENABLED", "1")
            ),
            subagent_max_steps=int(
                os.environ.get("RECOLLECT_SUBAGENT_MAX_STEPS", 8)
            ),
            subagent_max_tool_calls=int(
                os.environ.get("RECOLLECT_SUBAGENT_MAX_TOOL_CALLS", 8)
            ),
            subagent_observation_chars=int(
                os.environ.get("RECOLLECT_SUBAGENT_OBSERVATION_CHARS", 4_000)
            ),
            subagent_max_tokens=int(
                os.environ.get("RECOLLECT_SUBAGENT_MAX_TOKENS", 1_024)
            ),
            subagent_backend=os.environ.get("RECOLLECT_SUBAGENT_BACKEND", "legacy"),
            sandbox_steps=int(os.environ.get("RECOLLECT_SANDBOX_STEPS", 24)),
            sandbox_idle_ttl_s=float(
                os.environ.get("RECOLLECT_SANDBOX_IDLE_TTL_S", 1_800.0)
            ),
            sandbox_container_runtime=os.environ.get(
                "RECOLLECT_SANDBOX_CONTAINER_RUNTIME", "docker"
            ),
            sandbox_container_image=os.environ.get(
                "RECOLLECT_SANDBOX_CONTAINER_IMAGE",
                "recollect-opencode-sandbox:1.18.18",
            ),
            sandbox_container_memory_mb=int(
                os.environ.get("RECOLLECT_SANDBOX_CONTAINER_MEMORY_MB", 1_024)
            ),
            sandbox_container_pids=int(
                os.environ.get("RECOLLECT_SANDBOX_CONTAINER_PIDS", 256)
            ),
            sandbox_container_cpus=float(
                os.environ.get("RECOLLECT_SANDBOX_CONTAINER_CPUS", 2.0)
            ),
        )


def _flag(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
