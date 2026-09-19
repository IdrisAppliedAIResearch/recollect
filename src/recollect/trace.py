"""The TurnTrace: what one turn of memory retrieval actually did.

This is the project's central artifact. Both front ends - the inspector UI
and any OpenAI-compatible client - are renderers over this schema, and the
API is a transport for it. It is therefore the one thing worth designing
carefully before anything is built on top.

Three rules shaped it.

**Every scored candidate appears, not just the winners.** What was *not*
delivered is the part a delivered-set view cannot show: an episode that
missed the relevance threshold by a hundredth, or one that landed only
because it fell inside the continuity window and would otherwise have been
nowhere near. So ``candidates`` carries one row per episode in the store,
with its cosine against the query, whether it cleared the threshold,
whether continuity carried it, and therefore why it did or did not land.

**Text is not duplicated.** A candidate row carries a preview and a
character cost, never the full episode body: a 1,000-turn session would
otherwise write 1,000 copies of the transcript across its traces. The full
text of everything *delivered* is already present exactly once, in
``context_block.payload``, which is the ground truth of what the model saw.
Bodies for undelivered episodes are fetched from the store on demand.

**The trace must be provably faithful.** ``verification`` records whether
the instrumented reconstruction reproduced the library's own authoritative
output byte-for-byte. A trace that cannot prove this is worse than no
trace, because it invites confident conclusions from numbers nobody
checked. See ``recollect.engine.shadow``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Selection vocabulary
# ---------------------------------------------------------------------------

#: How a delivered episode earned its place. The timeline is a union, so an
#: episode can qualify both ways at once; ``both`` is kept distinct from
#: either alone because collapsing it would overstate what the threshold
#: retrieved on its own.
SelectionPath = Literal["relevance", "continuity", "both", "anchor"]

PATH_LABELS: dict[str, str] = {
    "relevance": "RELEVANT",
    "continuity": "RECENT",
    "both": "RELEVANT + RECENT",
    "anchor": "ANCHOR",
}

PATH_DESCRIPTIONS: dict[str, str] = {
    "relevance": (
        "Cleared the relevance threshold on raw cosine against the query, "
        "measured over the complete store. No ranking, no capacity: every "
        "episode at or above the threshold is delivered."
    ),
    "continuity": (
        "Inside the last recency_window_n exchanges by source order. "
        "Delivered as continuity regardless of how it scored, so the "
        "immediate conversation is never lost to a low cosine."
    ),
    "both": (
        "Qualified on relevance and fell inside the continuity window. "
        "Delivered once; the timeline is a union, not a concatenation."
    ),
    "anchor": (
        "An explicitly protected exchange, admitted regardless of relevance "
        "or recency because the caller named its turn."
    ),
}


# ---------------------------------------------------------------------------
# Stage records
# ---------------------------------------------------------------------------


class QueryTrace(BaseModel):
    """The turn's query and the identity of the vector it produced."""

    text: str
    chars: int
    embedding_sha256: str = Field(
        description="SHA-256 of the query vector's float32 bytes. Two turns "
        "with the same text must produce the same digest; if they do not, "
        "the embedder drifted and no cosine below is comparable."
    )
    embedding_norm: float
    embed_latency_ms: float
    embed_cache_hit: bool = False


class StoreTrace(BaseModel):
    """The state of the episode store this turn read from."""

    path: str
    episode_count: int
    config_json: str = Field(
        description="The EpisodicConfig this store was created under, "
        "serialized exactly as the store records it."
    )
    sentinel_sha256: str = Field(
        description="The call-shape sentinel digest the store asserted on "
        "open. Pins the embedder artifact and how it is called."
    )
    embedder_model_sha256: str


class CandidateTrace(BaseModel):
    """One episode in the store, and everything this turn decided about it.

    There is one of these per stored episode, whether or not it was
    delivered. That completeness is the point: a threshold is only
    interpretable next to the episodes it turned away.
    """

    id: str
    turn_number: int
    preview: str = Field(
        description="Leading characters of the user message, for display. "
        "Never the full body - see the module docstring."
    )
    assistant_preview: str

    cosine: float = Field(
        description="Cosine of this episode's stored vector against the "
        "query vector, in float64 over the retained float32 embeddings. "
        "Computed for every episode, every turn, and reported as measured: "
        "the timeline applies no scaling, fusion or rank to it."
    )
    margin: float = Field(
        description="cosine - relevance_threshold. Negative means it missed. "
        "Carried because the distance from the threshold is the whole "
        "question for an episode that did not land, and recomputing it in "
        "each renderer invites two answers to one question."
    )

    render_chars: int = Field(
        description="Exact serialized size of this episode's element, in "
        "characters. Nothing is charged against an allowance on this path - "
        "it is here so the cost of a delivered block stays attributable."
    )

    eligible: bool = Field(
        description="Within the caller's source-order horizon. False only "
        "when an explicit through_turn excluded it; without one, every "
        "stored episode is eligible."
    )
    relevant: bool = Field(
        description="Cosine at or above the relevance threshold. Independent "
        "of the continuity window: both can be true."
    )
    in_recency_window: bool = Field(
        description="Inside the trailing recency_window_n slice of eligible "
        "episodes, ordered by (turn_number, id). Delivered as continuity "
        "whatever its cosine."
    )
    is_anchor: bool = Field(
        default=False,
        description="Named by the caller's anchor_turn and therefore "
        "protected regardless of relevance or recency.",
    )

    withheld: bool = Field(
        default=False,
        description="Cleared the threshold and would have been delivered, "
        "but the deployment ceiling excluded it - see CeilingTrace. This is "
        "the one undelivered row the mechanism did not decide, and it is "
        "kept distinct from a near miss because the two mean opposite "
        "things: one was not relevant enough, the other was relevant and "
        "did not fit.",
    )

    delivered: bool
    delivered_via: SelectionPath | None = Field(
        default=None,
        description="Which condition put it in the block. An episode that "
        "is both relevant and recent reports 'both' rather than being "
        "attributed to one - the union has no precedence order to record.",
    )


class TimelineDetail(BaseModel):
    """The selection this turn made, and the settings that produced it.

    The timeline has no ranking, no capacity and no drops, so there is no
    decision sequence to record - only the two conditions and what each
    admitted. What is worth keeping is their overlap: a turn where every
    relevant episode already sat inside the continuity window retrieved
    nothing the last N exchanges would not have supplied anyway, and that
    is invisible from a delivered count alone.
    """

    read_policy: str
    relevance_threshold: float
    recency_window_n: int
    eligible_count: int = Field(
        description="Episodes inside the caller's source-order horizon. "
        "Equals the store's episode count unless through_turn was set."
    )
    relevant_ids: list[str] = Field(
        description="Cleared the threshold, in source order. Includes any "
        "that also fell inside the continuity window."
    )
    recent_ids: list[str] = Field(
        description="The trailing continuity slice, in source order."
    )
    selected_ids: list[str] = Field(
        description="The delivered union, chronologically - the order the "
        "block itself renders."
    )
    overlap_ids: list[str] = Field(
        default_factory=list,
        description="Qualified on both counts. A large overlap means the "
        "threshold contributed little the window did not already carry.",
    )
    relevance_only_count: int = Field(
        description="Episodes the threshold contributed that continuity "
        "would not have delivered anyway. The honest measure of what "
        "retrieval added this turn."
    )
    through_turn: int | None = None
    anchor_turn: int | None = None


class CeilingTrace(BaseModel):
    """Recollect's hardware ceiling - which the library's mechanism has not.

    Kept as its own record, deliberately not folded into ``TimelineDetail``,
    because it is **a deviation**. The library delivers every episode at or
    above the threshold and caps nothing; its own documentation says the
    timeline output "is also uncapped and has no established latency
    horizon". That is a defensible research position and this harness does
    not argue with it.

    It is also unrunnable on a 32K-context local model. So Recollect decides
    which episodes are *handed to* the library, and the library then does
    exactly what it always does with the set it is given. The alternative -
    trimming the payload after the fact - would make the shadow and the
    authority disagree byte-for-byte and refuse every turn, which is §3's
    second hard rule working as intended.

    Two properties keep this honest. Continuity is never withheld, so the
    ceiling can be exceeded by the recency window alone rather than
    silently dropping what was just said. And ``store_episodes`` is the
    true store size, because ``report.pool_size`` and
    ``report.eligible_count`` describe only what the library was shown and
    would otherwise understate the history that exists.
    """

    ceiling_chars: int | None = Field(
        description="The deployment ceiling in characters, or None when "
        "disabled and the library's uncapped behaviour is taken as-is."
    )
    engaged: bool = Field(
        description="True when the ceiling actually withheld something. "
        "False means this turn fit and the mechanism ran unmodified."
    )
    store_episodes: int = Field(
        description="Episodes in the store. The report's pool_size counts "
        "only what was handed to the library."
    )
    considered_episodes: int
    withheld_ids: list[str] = Field(
        default_factory=list,
        description="Episodes that cleared the threshold and would have "
        "been delivered, excluded lowest-cosine-first to fit the ceiling. "
        "Never a continuity or anchor episode.",
    )
    withheld_chars: int = Field(
        default=0,
        description="What those episodes would have added, at their exact "
        "serialized cost.",
    )


class ContextBlockTrace(BaseModel):
    """The assembled block, exactly as the model received it."""

    payload: str
    chars: int
    sha256: str
    recent_episode_count: int
    retrieved_episode_count: int


class ReportTrace(BaseModel):
    """The library's own ContextReport, carried verbatim.

    This is the authority. Every richer number in the trace is checked
    against it.

    Only the fields the timeline populates are carried. The report type
    also has ``episodes_dropped``, ``truncated``, ``dropped_ids``,
    ``drop_policy``, ``budget_chars``, ``coverage_count``, ``aspect_count``
    and ``returned_semantic_count``, all of which describe a budgeted path
    and are structurally constant here: nothing is ranked, nothing competes
    for capacity and nothing is dropped. Storing a column of zeros on every
    turn would invite a reader to conclude a drop could have happened. They
    are still asserted in ``shadow._verify`` - constant is a claim, and an
    unchecked claim is how a mechanism change goes unnoticed.
    """

    chars_delivered: int
    chars_wanted: int
    episodes_delivered: int
    stm_count: int
    k_count: int
    latency_ms: float
    pool_size: int
    read_policy: str
    relevance_threshold: float
    eligible_count: int
    selected_ids: list[str] = Field(default_factory=list)
    retrieval_chars_delivered: int | None = None
    recency_count: int = 0
    semantic_count: int = 0
    recent_ids: list[str] = Field(default_factory=list)
    recency_additive: bool = True
    through_turn: int | None = None
    anchor_turn: int | None = None


class VerificationTrace(BaseModel):
    """Proof that the instrumented reconstruction matched the library.

    The harness never forks the mechanism. It calls the library for the
    authoritative answer, independently recomputes the same pipeline to
    capture the internals the library discards, and then checks the two
    agree. ``payload_identical`` is the strong claim: the reconstruction
    produced the same characters, not merely the same counts.
    """

    payload_identical: bool
    report_fields_identical: bool
    authority_payload_sha256: str
    shadow_payload_sha256: str
    mismatched_fields: list[str] = Field(default_factory=list)
    library_version: str
    shadow_latency_ms: float

    @property
    def trustworthy(self) -> bool:
        return self.payload_identical and self.report_fields_identical


class PromptCacheTrace(BaseModel):
    """How much of this prompt the server had already computed.

    Measured on this hardware at a ~18k-token prompt: a stable prefix with
    the question appended costs 0.27s on a warm cache, while rewriting the
    memory block each turn costs 5.9s - a 22x difference. This architecture
    rebuilds its context block every turn by design, so it forfeits most of
    that cache on purpose. The number is recorded rather than assumed,
    because "rebuild from scratch" has a price and this is where it shows
    up. Fields are None when the server does not report timings.
    """

    prompt_tokens: int | None = Field(
        default=None, description="Total prompt length in tokens."
    )
    cached_tokens: int | None = Field(
        default=None,
        description="Tokens reused from the server's prefix cache "
        "(llama.cpp `cache_n`).",
    )
    processed_tokens: int | None = Field(
        default=None,
        description="Tokens the server actually had to prefill this request "
        "(llama.cpp `prompt_n`). Note this is the *new* work, not the total: "
        "the prompt length is this plus the cached count, and reading "
        "`prompt_n` as the total is what produces impossible hit ratios "
        "above 100%.",
    )
    prefill_ms: float | None = None

    @property
    def cache_hit_ratio(self) -> float | None:
        if not self.prompt_tokens or self.cached_tokens is None:
            return None
        return min(1.0, self.cached_tokens / self.prompt_tokens)


class ToolCallTrace(BaseModel):
    """One tool invocation the model requested via OpenAI ``tool_calls``.

    ``arguments`` is the raw JSON string exactly as the server streamed it,
    not a re-serialized dict: a re-serialization can silently reorder or
    normalize things the model did not write.
    """

    id: str
    name: str
    arguments: str


class SubagentTrace(BaseModel):
    """One-line accounting for the ephemeral subagent, if one ran.

    Deliberately a *summary* and nothing more: the subagent's full arc —
    prompts, tool calls, observations — is an internal implementation detail
    of one generation step and is never written to the episode store. Keeping
    only these counts here is what makes that guarantee inspectable from the
    trace itself.
    """

    task: str
    effort: Literal["focused", "deep"] = "focused"
    backend: Literal["legacy", "opencode"] = "legacy"
    isolation: str = Field(
        default="in_process",
        description="The effective execution boundary: container, "
        "in_process, test, or unavailable.",
    )
    fresh_context: bool = Field(
        default=True,
        description="Whether this call started without prior subagent context.",
    )
    server_reused: bool = Field(
        default=False,
        description="Whether a warm OpenCode server process served this call.",
    )
    status: str = Field(
        description="'ok' when the subagent returned usable final output, "
        "'partial' when a cap or missing final answer ended it early, "
        "'error' when it could not run at all."
    )
    steps: int
    tools_used: list[str]
    sources: list[str]
    returned_chars: int
    total_ms: float
    error: str | None = None


class GenerationTrace(BaseModel):
    """What the model was asked, and what it did with it."""

    model: str
    base_url: str
    system_prompt_chars: int
    context_block_chars: int
    total_prompt_chars: int
    task_context_chars: int = 0
    task_ids: list[str] = Field(default_factory=list)
    model_queue_ms: float | None = None

    response_text: str = ""
    memory_response_text: str | None = None
    response_chars: int = 0
    reasoning_text: str = Field(
        default="",
        description="Chain-of-thought the server routed to a separate "
        "`reasoning_content` field. Captured because the carried models "
        "leave `content` empty while thinking, which reads as a silent "
        "failure to any client that only watches `content`.",
    )
    thinking_enabled: bool = False

    ttft_ms: float | None = None
    total_ms: float | None = None
    tokens_out: int | None = None
    tokens_per_sec: float | None = None
    tool_calls: list[ToolCallTrace] = Field(
        default_factory=list,
        description="Tool invocations the model requested in this generation, "
        "accumulated across the stream. Empty when the call had no tools or "
        "the model did not use any - requests without tools are unaffected.",
    )
    prompt_cache: PromptCacheTrace = Field(default_factory=PromptCacheTrace)
    finish_reason: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------------


class TurnTrace(BaseModel):
    """One complete turn: retrieval, assembly, generation, and the proof."""

    #: 3 is the timeline read path. A version-2 trace described CC80/ASPECT
    #: packing - different fields, different mechanism - so the two are not
    #: mixed: a stored v2 trace is history, readable only by a v2 reader.
    schema_version: Literal[3] = 3

    turn_id: str
    session_id: str
    turn_index: int
    started_at: datetime
    total_ms: float | None = None

    query: QueryTrace
    store: StoreTrace

    candidates: list[CandidateTrace]
    timeline: TimelineDetail
    ceiling: CeilingTrace

    context_block: ContextBlockTrace
    report: ReportTrace
    verification: VerificationTrace
    generation: GenerationTrace | None = None

    subagent: SubagentTrace | None = Field(
        default=None,
        description="Summary of the ephemeral subagent for this "
        "turn, when the main model delegated to one. One line of accounting "
        "only - the subagent's content is never recorded. None on ordinary "
        "turns.",
    )

    # -- convenience views the UI would otherwise recompute ----------------

    @property
    def relevance_only_ids(self) -> list[str]:
        """Delivered on relevance alone - what continuity would have missed.

        The one number that says whether retrieval earned its place this
        turn. A block can look full and still be nothing but the last N
        exchanges.
        """
        recent = set(self.timeline.recent_ids)
        return [
            identifier
            for identifier in self.timeline.selected_ids
            if identifier not in recent
        ]


class TurnSummary(BaseModel):
    """A turn reduced to what a session list needs. Cheap to load in bulk."""

    turn_id: str
    session_id: str
    turn_index: int
    started_at: datetime
    query_preview: str
    response_preview: str
    episodes_delivered: int
    chars_delivered: int
    stm_count: int
    k_count: int
    eligible_count: int
    trace_trustworthy: bool
    recency_count: int = 0
    semantic_count: int = 0
    relevance_only_count: int = 0
    #: The deployment ceiling withheld episodes the mechanism would have
    #: delivered. Worth carrying into a session list: a run of these is the
    #: signal that the store has outgrown the deployed context window.
    ceiling_engaged: bool = False
