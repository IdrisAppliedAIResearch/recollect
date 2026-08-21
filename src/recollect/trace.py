"""The TurnTrace: what one turn of memory retrieval actually did.

This is the project's central artifact. Both front ends - the inspector UI
and any OpenAI-compatible client - are renderers over this schema, and the
API is a transport for it. It is therefore the one thing worth designing
carefully before anything is built on top.

Three rules shaped it.

**Every scored candidate appears, not just the winners.** The research this
harness deploys found its most important results in what was *not*
delivered: a similarity path that fires at zero because its threshold sits
at roughly twice the height genuine relevance reaches, and a coverage
selector that chooses its set as though it owns the whole budget and is
then handed the remainder. Neither is visible from the delivered set. So
``candidates`` carries one row per episode in the store, with its cosine,
its cluster, and the reason it did or did not land.

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
# Tier vocabulary
# ---------------------------------------------------------------------------

#: The three retrieval paths, in the order packing considers them. The short
#: names are the research repository's; the labels are what the UI shows.
TierName = Literal["recency", "similarity", "coverage"]

TIER_LABELS: dict[str, str] = {
    "recency": "RECENT",
    "similarity": "RELATED",
    "coverage": "SPREAD",
}

TIER_DESCRIPTIONS: dict[str, str] = {
    "recency": (
        "The last N episodes in conversation order. No scoring involved. "
        "Packed first, so it spends the budget before anything else is "
        "considered."
    ),
    "similarity": (
        "Episodes whose cosine against the query clears a fixed threshold. "
        "Measured inert on the internal corpus: the threshold sits above the "
        "highest score relevant content reaches."
    ),
    "coverage": (
        "A budgeted greedy over the whole store: relevance plus a bonus for "
        "entering a topic cluster not yet covered. It selects as though it "
        "owns the entire budget, and is then packed last."
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
    delivered. That completeness is the point: the interesting failures in
    this architecture are all about candidates that scored well and never
    arrived.
    """

    id: str
    turn_number: int
    preview: str = Field(
        description="Leading characters of the user message, for display. "
        "Never the full body - see the module docstring."
    )
    assistant_preview: str

    relevance: float = Field(
        description="Cosine of this episode's stored vector against the "
        "query vector. Computed for every episode, every turn."
    )
    relevance_rank: int = Field(
        description="1 = highest cosine this turn. Ties broken by turn."
    )
    cluster: int | None = Field(
        default=None,
        description="Which of the k deterministic clusters this episode fell "
        "into. None when it was outside the candidate pool.",
    )

    render_chars: int = Field(
        description="Exact serialized cost of admitting this episode, in "
        "characters, as the renderer would write it."
    )

    in_recency_window: bool
    passes_similarity_threshold: bool
    selected_by_coverage: bool

    delivered: bool
    delivered_via: TierName | None = Field(
        default=None,
        description="The path that claimed it first. Attribution follows the "
        "packing order, so an episode in both the recency window and the "
        "coverage selection is attributed to recency.",
    )
    drop_reason: str | None = Field(
        default=None,
        description="Why a proposed episode did not land. None when it was "
        "never proposed, or when it was delivered.",
    )


class SelectorStepTrace(BaseModel):
    """One greedy step of the coverage selector, with its arithmetic shown."""

    step: int
    candidate_id: str
    source_turn: int
    relevance: float = Field(description="The modular relevance term.")
    objective_gain: float = Field(
        description="Marginal gain: relevance plus the cluster-novelty bonus "
        "if this episode enters an uncovered cluster."
    )
    scaled_gain: float = Field(
        description="Objective gain divided by cost^r. With r=0 this equals "
        "the objective gain and cost does not influence the choice."
    )
    additive_chars: int
    cumulative_chars: int
    entered_new_cluster: bool
    cluster: int | None = None


class ClusterTrace(BaseModel):
    """One deterministic topic cluster over the candidate pool."""

    id: int
    size: int
    member_ids: list[str]
    mean_relevance: float
    max_relevance: float
    selected_ids: list[str] = Field(
        default_factory=list,
        description="Members the coverage selector chose.",
    )
    delivered_ids: list[str] = Field(
        default_factory=list,
        description="Members that survived packing and reached the model.",
    )

    @property
    def covered(self) -> bool:
        return bool(self.selected_ids)


class TierTrace(BaseModel):
    """What one retrieval path proposed, and what became of it.

    A path can end a turn having delivered nothing for two entirely
    different reasons, and conflating them would hide the exact fault this
    harness exists to expose:

    - **Starved.** It proposed episodes that never reached the context at
      all, because an earlier path had already spent the budget. This is
      the packing-order fault.
    - **Overlapped.** Its episodes did reach the context, but an earlier
      path had already claimed them, so it added nothing new. The path
      worked; it just duplicated.

    Both show as a zero in a delivered count. They are kept apart here.
    """

    name: TierName
    label: str
    description: str
    proposed_ids: list[str]
    delivered_ids: list[str] = Field(
        description="Reached the context AND were attributed to this path. "
        "Attribution follows packing order, so an earlier path wins ties."
    )
    overlapped_ids: list[str] = Field(
        default_factory=list,
        description="Proposed by this path, present in the context, but "
        "credited to an earlier path that also proposed them.",
    )
    skipped_ids: list[str] = Field(
        default_factory=list,
        description="Proposed and absent from the context entirely: the "
        "budget was gone by the time they were considered.",
    )
    chars_delivered: int
    chars_proposed: int

    @property
    def starved(self) -> bool:
        """Proposed episodes that never reached the context at all."""
        return bool(self.skipped_ids) and not self.delivered_ids

    @property
    def fully_overlapped(self) -> bool:
        """Everything it proposed was already claimed by an earlier path."""
        return (
            bool(self.proposed_ids)
            and not self.delivered_ids
            and not self.skipped_ids
        )

    @property
    def contributed(self) -> bool:
        """Added at least one episode the context would not otherwise have."""
        return bool(self.delivered_ids)


class SimilarityTierDetail(BaseModel):
    """Why the similarity path fired or did not.

    Broken out because a bare count of zero is the single most misleading
    number this system can report. Zero hits at a threshold of 0.48 when
    the best cosine in the store is 0.27 is not a quiet turn - it is a
    path that cannot fire, and the margin says so.
    """

    threshold: float
    hit_count: int
    max_relevance_observed: float
    margin_to_threshold: float = Field(
        description="threshold minus the best cosine observed. Positive "
        "means nothing in the store could have cleared the bar this turn."
    )
    inert: bool = Field(
        description="True when no episode reached the threshold. Reported "
        "as a distinct condition rather than as an empty result."
    )


class PackingDecision(BaseModel):
    """One admission attempt, in the order packing made it."""

    order: int
    candidate_id: str
    tier: TierName
    cost_chars: int
    payload_chars_after: int
    admitted: bool
    reason: str


class PackingTrace(BaseModel):
    """How the budget was spent, decision by decision."""

    policy: str = Field(
        description="The named drop policy from the library. Candidates are "
        "considered in tier order and a candidate that does not fit is "
        "skipped rather than ending the walk."
    )
    tier_order: list[TierName]
    budget_chars: int
    empty_payload_chars: int = Field(
        description="Cost of the two empty block tags. No payload is cheaper, "
        "so a budget below this cannot express any answer at all."
    )
    decisions: list[PackingDecision]
    duplicate_ids: list[str] = Field(
        default_factory=list,
        description="Episodes proposed by more than one path. Charged once.",
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
    """

    chars_delivered: int
    chars_wanted: int
    chars_available: int
    shortfall_chars: int
    episodes_delivered: int
    episodes_dropped: int
    truncated: bool
    stm_count: int
    k_count: int
    coverage_count: int
    latency_ms: float
    pool_size: int
    dropped_ids: list[str]
    drop_policy: str
    budget_chars: int


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

    response_text: str = ""
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

    schema_version: Literal[1] = 1

    turn_id: str
    session_id: str
    turn_index: int
    started_at: datetime
    total_ms: float | None = None

    query: QueryTrace
    store: StoreTrace

    candidates: list[CandidateTrace]
    clusters: list[ClusterTrace]
    tiers: list[TierTrace]
    similarity_detail: SimilarityTierDetail
    selector_steps: list[SelectorStepTrace]
    packing: PackingTrace

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

    def tier(self, name: TierName) -> TierTrace:
        for entry in self.tiers:
            if entry.name == name:
                return entry
        raise KeyError(name)

    @property
    def starved_tiers(self) -> list[TierName]:
        """Paths that proposed episodes and delivered none.

        Worth surfacing on its own: a starved path is the signature of the
        packing-order fault, and it is invisible in a delivered-set view.
        """
        return [entry.name for entry in self.tiers if entry.starved]

    @property
    def budget_utilization(self) -> float:
        if self.report.budget_chars <= 0:
            return 0.0
        return self.report.chars_delivered / self.report.budget_chars


class TurnSummary(BaseModel):
    """A turn reduced to what a session list needs. Cheap to load in bulk."""

    turn_id: str
    session_id: str
    turn_index: int
    started_at: datetime
    query_preview: str
    response_preview: str
    episodes_delivered: int
    episodes_dropped: int
    chars_delivered: int
    budget_chars: int
    stm_count: int
    k_count: int
    coverage_count: int
    starved_tiers: list[str]
    trace_trustworthy: bool
