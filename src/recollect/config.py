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

from .limits import validate_identifier

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
    "are drawn from your own earlier conversation with this user. Treat them "
    "as your memory, not as documents: do not mention the blocks or say that "
    "something was retrieved. If they do not contain what you need, say you "
    "do not recall it rather than inventing a memory.\n\n"
    "You are talking with the user. Default to the shortest answer that "
    "satisfies the question, and say more only when they ask for more. Use "
    "plain spoken prose, not headings or bullet lists.\n\n"
    "The run_subagent tool delegates a self-contained task to an autonomous "
    "subagent with research tools and a scratch workspace, which returns a "
    "result you then answer from. Use it for current or external research and "
    "genuinely sustained multi-step work, not for ordinary reasoning or "
    "anything answerable from this conversation. Keep the task brief: what to "
    "do, and what a good result looks like. Mark narrow lookups as focused; "
    "reserve deep effort for substantial multi-source work."
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
    generator_base_url: str = "http://127.0.0.1:8001/v1"
    generator_model: str = "local"
    generator_api_key: str = "not-needed"
    generator_timeout_s: float = 300.0
    #: The carried models route chain-of-thought into a separate field and
    #: leave `content` empty while thinking. Off by default so a first
    #: conversation returns visible text.
    generator_thinking: bool = False
    #: A conversational turn is 15-30 words when answering an open question
    #: and shorter otherwise. This is the deterministic ceiling, not the
    #: target: the prompt asks for the shortest sufficient answer. The margin
    #: above ~40 tokens of speech is the task_reply JSON wrapper, measured at
    #: 97 output tokens for a 43-word reply.
    generator_max_tokens: int = 256
    #: Routing replies are JSON, so truncation is a parse failure rather than
    #: a short answer - a cut run_subagent call means the research never
    #: starts. The extra headroom buys nothing visible: this output is
    #: internal, so it cannot make a reply longer.
    generator_routing_max_tokens: int = 320
    #: The background relay compresses a finished report into speech. It is
    #: plain prose, so overrun is a severed sentence rather than a parse
    #: failure; `_trim_to_sentence` cleans that seam and this bounds the cost.
    task_relay_max_tokens: int = 320
    generator_temperature: float = 0.7

    # -- local speech (separate from the embedding and chat models) ---------
    voice_model_dir: Path = Path("var/models/voice")
    voice_wake_phrase: str = "hey idris"
    voice_name: str = "af_heart"
    voice_device: str = "auto"
    voice_cuda_dll_dir: Path | None = None
    voice_asr_backend: str = "vosk"
    voice_asr_model_dir: Path = Path("var/models/voice/whisper-large-v3-turbo")
    voice_asr_device: str = "cuda"
    voice_asr_compute_type: str = "float16"
    voice_asr_cuda_dll_dir: Path | None = None
    voice_threads: int = 2
    voice_max_utterance_s: float = 120.0
    voice_wait_s: float = 8.0
    voice_end_s: float = 1.4
    voice_speech_start_s: float = 0.224
    voice_speech_threshold: float = 0.5
    voice_interrupt_threshold: float = 0.8

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
    subagent_continuous_enabled: bool = False
    generator_context_tokens: int = 32_768
    generator_parallel_slots: int = 1
    subagent_inference_tokens: int = 2_048
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
    downloads_dir: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8080

    def __post_init__(self) -> None:
        if self.downloads_dir is not None and not self.downloads_dir.is_absolute():
            raise ValueError("downloads_dir must be an absolute path")
        if not self.voice_wake_phrase.strip():
            raise ValueError("voice_wake_phrase must be non-empty")
        if not self.voice_name.strip():
            raise ValueError("voice_name must be non-empty")
        if self.voice_device not in ("auto", "cpu", "cuda"):
            raise ValueError("voice_device must be 'auto', 'cpu', or 'cuda'")
        if (self.voice_cuda_dll_dir is not None
                and not self.voice_cuda_dll_dir.is_absolute()):
            raise ValueError("voice_cuda_dll_dir must be an absolute path")
        if self.voice_threads < 1:
            raise ValueError("voice_threads must be positive")
        if self.voice_asr_backend not in ("vosk", "whisper"):
            raise ValueError("voice_asr_backend must be 'vosk' or 'whisper'")
        if self.voice_asr_device not in ("cpu", "cuda"):
            raise ValueError("voice_asr_device must be 'cpu' or 'cuda'")
        if self.voice_asr_compute_type not in (
            "float32", "float16", "int8", "int8_float16", "int8_float32",
        ):
            raise ValueError("Unsupported voice_asr_compute_type")
        if (self.voice_asr_cuda_dll_dir is not None
                and not self.voice_asr_cuda_dll_dir.is_absolute()):
            raise ValueError("voice_asr_cuda_dll_dir must be an absolute path")
        if not 1 <= self.voice_wait_s <= self.voice_max_utterance_s <= 120:
            raise ValueError("voice timeouts must satisfy 1 <= wait <= maximum <= 120")
        if not 0.3 <= self.voice_end_s <= 5:
            raise ValueError("voice_end_s must be between 0.3 and 5 seconds")
        if not 0.064 <= self.voice_speech_start_s <= 1:
            raise ValueError("voice_speech_start_s must be between 0.064 and 1 second")
        if not 0 < self.voice_speech_threshold <= self.voice_interrupt_threshold < 1:
            raise ValueError(
                "voice thresholds must satisfy 0 < speech <= interrupt < 1"
            )
        if self.budget_chars < 0:
            raise ValueError("budget_chars must be non-negative")
        if self.generator_max_tokens < 1:
            raise ValueError("generator_max_tokens must be positive")
        if self.generator_routing_max_tokens < 1:
            raise ValueError("generator_routing_max_tokens must be positive")
        if self.task_relay_max_tokens < 1:
            raise ValueError("task_relay_max_tokens must be positive")
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
        if not 4_096 <= self.generator_context_tokens <= 131_072:
            raise ValueError("generator_context_tokens must be 4096..131072")
        if (type(self.generator_parallel_slots) is not int
                or self.generator_parallel_slots not in {1, 2}):
            raise ValueError("generator_parallel_slots must be 1 or 2")
        if not 128 <= self.subagent_inference_tokens < self.generator_context_tokens:
            raise ValueError("subagent inference output must fit the model context")
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
        return self._contained_path(validate_identifier(session_id))

    def _contained_path(self, *parts: str) -> Path:
        path = self.sessions_dir.joinpath(*parts)
        if not path.resolve().is_relative_to(self.sessions_dir.resolve()):
            raise ValueError("Session storage path leaves the configured directory.")
        return path

    def session_file(self, session_id: str, filename: str) -> Path:
        if filename not in {"session.json", "turns.jsonl", "episodes.sqlite"}:
            raise ValueError("Unsupported session file.")
        return self._contained_path(validate_identifier(session_id), filename)

    def store_path(self, session_id: str) -> Path:
        return self.session_file(session_id, "episodes.sqlite")

    def traces_dir(self, session_id: str) -> Path:
        return self._contained_path(validate_identifier(session_id), "traces")

    def trace_path(self, session_id: str, turn_id: str) -> Path:
        return self._contained_path(
            validate_identifier(session_id), "traces",
            f"{validate_identifier(turn_id)}.json",
        )

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
            voice_model_dir=Path(
                os.environ.get("RECOLLECT_VOICE_MODEL_DIR", "var/models/voice")
            ),
            voice_wake_phrase=os.environ.get(
                "RECOLLECT_VOICE_WAKE_PHRASE", "hey idris"
            ),
            voice_name=os.environ.get("RECOLLECT_VOICE_NAME", "af_heart"),
            voice_device=os.environ.get("RECOLLECT_VOICE_DEVICE", "auto").lower(),
            voice_cuda_dll_dir=(
                Path(os.environ["RECOLLECT_VOICE_CUDA_DLL_DIR"])
                if os.environ.get("RECOLLECT_VOICE_CUDA_DLL_DIR") else None
            ),
            voice_threads=int(os.environ.get("RECOLLECT_VOICE_THREADS", 2)),
            voice_asr_backend=os.environ.get(
                "RECOLLECT_VOICE_ASR_BACKEND", "vosk"
            ).lower(),
            voice_asr_model_dir=Path(os.environ.get(
                "RECOLLECT_VOICE_ASR_MODEL_DIR",
                "var/models/voice/whisper-large-v3-turbo",
            )),
            voice_asr_device=os.environ.get(
                "RECOLLECT_VOICE_ASR_DEVICE", "cuda"
            ).lower(),
            voice_asr_compute_type=os.environ.get(
                "RECOLLECT_VOICE_ASR_COMPUTE_TYPE", "float16"
            ).lower(),
            voice_asr_cuda_dll_dir=(
                Path(os.environ["RECOLLECT_VOICE_ASR_CUDA_DLL_DIR"])
                if os.environ.get("RECOLLECT_VOICE_ASR_CUDA_DLL_DIR") else None
            ),
            voice_max_utterance_s=float(
                os.environ.get("RECOLLECT_VOICE_MAX_UTTERANCE_S", 120)
            ),
            voice_wait_s=float(os.environ.get("RECOLLECT_VOICE_WAIT_S", 8)),
            voice_end_s=float(os.environ.get("RECOLLECT_VOICE_END_S", 1.4)),
            voice_speech_start_s=float(
                os.environ.get("RECOLLECT_VOICE_SPEECH_START_S", 0.224)
            ),
            voice_speech_threshold=float(
                os.environ.get("RECOLLECT_VOICE_SPEECH_THRESHOLD", 0.5)
            ),
            voice_interrupt_threshold=float(
                os.environ.get("RECOLLECT_VOICE_INTERRUPT_THRESHOLD", 0.8)
            ),
            embedding_threads=int(
                os.environ.get(
                    "RECOLLECT_EMBEDDING_THREADS", DEFAULT_EMBEDDING_THREADS
                )
            ),
            generator_base_url=os.environ.get(
                "RECOLLECT_GENERATOR_BASE_URL", "http://127.0.0.1:8001/v1"
            ),
            generator_model=os.environ.get("RECOLLECT_GENERATOR_MODEL", "local"),
            generator_api_key=os.environ.get(
                "RECOLLECT_GENERATOR_API_KEY", "not-needed"
            ),
            generator_thinking=_flag(os.environ.get("RECOLLECT_GENERATOR_THINKING")),
            generator_max_tokens=int(
                os.environ.get("RECOLLECT_GENERATOR_MAX_TOKENS", 256)
            ),
            generator_routing_max_tokens=int(
                os.environ.get("RECOLLECT_GENERATOR_ROUTING_MAX_TOKENS", 320)
            ),
            task_relay_max_tokens=int(
                os.environ.get("RECOLLECT_TASK_RELAY_MAX_TOKENS", 320)
            ),
            budget_chars=int(
                os.environ.get("RECOLLECT_BUDGET_CHARS", DEFAULT_BUDGET_CHARS)
            ),
            aspect_enabled=_flag(os.environ.get("RECOLLECT_ASPECT_ENABLED", "1")),
            data_dir=Path(os.environ.get("RECOLLECT_DATA_DIR", "var")),
            downloads_dir=(Path(os.environ["RECOLLECT_DOWNLOADS_DIR"])
                           if os.environ.get("RECOLLECT_DOWNLOADS_DIR") else None),
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
            subagent_continuous_enabled=_flag(
                os.environ.get("RECOLLECT_SUBAGENT_CONTINUOUS_ENABLED", "0")
            ),
            generator_context_tokens=int(
                os.environ.get("RECOLLECT_GENERATOR_CONTEXT_TOKENS", 32_768)
            ),
            generator_parallel_slots=int(
                os.environ.get("RECOLLECT_GENERATOR_PARALLEL_SLOTS", 1)
            ),
            subagent_inference_tokens=int(
                os.environ.get("RECOLLECT_SUBAGENT_INFERENCE_TOKENS", 2_048)
            ),
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
