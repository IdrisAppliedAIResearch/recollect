"""The TurnTrace: what one turn of memory retrieval actually did.

This is the project's central artifact. Both front ends - the inspector UI
and any OpenAI-compatible client - are renderers over this schema, and the
API is a transport for it. It is therefore the one thing worth designing
carefully before anything is built on top.

Three rules shaped it.

**Every scored candidate appears, not just the winners.** The research this
harness deploys found its most important results in what was *not*
delivered: episodes that top the CC80 rank yet never land because the
initial half's walk skipped them, and candidates the ASPECT saturation
rejected on marginal gain. Neither is visible from the delivered set. So
``candidates`` carries one row per episode in the store, with its dense and
BM25 terms, its fused score and rank, and the reason it did or did not land.

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

#: The retrieval paths, the way the deployed CC-007 read path composes them:
#: additive continuity, budgeted CC80 semantic admission, and the protected
#: static ASPECT spread that carves out half of the long-term allowance.
TierName = Literal["recency", "semantic", "aspect"]

TIER_LABELS: dict[str, str] = {
    "recency": "RECENT",
    "semantic": "SEMANTIC",
    "aspect": "ASPECT",
}

TIER_DESCRIPTIONS: dict[str, str] = {
    "recency": (
        "The last recency_window_n episodes in conversation order. Rendered "
        "additively OUTSIDE the long-term budget: always delivered, never "
        "dropped, and excluded from long-term admission by identity."
    ),
    "semantic": (
        "Long-term admission ranked by frozen CC80 over the complete store: "
        "dense cosine and BM25 each min-max normalized per query, fused "
        "0.8 dense / 0.2 BM25, packed in rank order with skip-on-overflow. "
        "On ASPECT turns this is the initial half plus whatever the slack "
        "return rescued afterwards."
    ),
    "aspect": (
        "The protected static ASPECT half: a greedy saturation over frozen "
        "parser facets (entity, date, number, event, relation, noun) that "
        "admits episodes whose facets are not yet covered, scored by CC80 "
        "score times facet idf, budgeted to the other half of the allowance."
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

    dense_cosine: float = Field(
        description="Cosine of this episode's stored vector against the "
        "query vector. Computed for every episode, every turn, and reported "
        "as measured - no normalization is applied here."
    )
    dense_normalized: float = Field(
        description="The dense term after per-query min-max scaling, as it "
        "enters the CC80 fusion. Zero for every episode when the store has "
        "only one candidate or all cosines tie: a constant component "
        "contributes nothing rather than dividing by zero."
    )
    bm25_score: float = Field(
        description="Raw Robertson BM25 of this episode against the "
        "tokenized query, before normalization."
    )
    bm25_normalized: float = Field(
        description="The BM25 term after per-query min-max scaling, as it "
        "enters the CC80 fusion."
    )
    cc80_score: float = Field(
        description="The fused CC80 ranking score: semantic_dense_weight * "
        "dense_normalized + (1 - weight) * bm25_normalized, under the "
        "store-pinned weights (0.8 dense / 0.2 BM25 by default)."
    )
    cc80_rank: int = Field(
        description="1 = highest CC80 score this turn. Ties broken by turn "
        "number, then id, exactly as the library orders them."
    )

    render_chars: int = Field(
        description="Exact serialized size of this episode's element, in "
        "characters. The admission charge is this plus one separator "
        "character - see the packing decisions."
    )

    in_recency_window: bool = Field(
        description="Within the trailing recency_window_n slice. If so, the "
        "episode is delivered additively and is never considered by "
        "long-term admission."
    )
    in_semantic_initial: bool = Field(
        description="Admitted by the initial budgeted CC80 walk (rank order, "
        "skipping what does not fit the half allowance)."
    )
    selected_by_aspect: bool = Field(
        description="Admitted by the protected ASPECT spread: the greedy "
        "facet-saturation half."
    )
    returned_semantic: bool = Field(
        description="Rejected by the initial and spread packs, then rescued "
        "by the final slack walk while budget still remained."
    )

    delivered: bool
    delivered_via: TierName | None = Field(
        default=None,
        description="The path that claimed it first. Attribution follows "
        "decision order, so an episode in both the recency window and the "
        "CC80 selection is attributed to recency.",
    )
    drop_reason: str | None = Field(
        default=None,
        description="Why a proposed episode did not land: the reason of its "
        "last admission attempt that proposed it. None when it was never "
        "proposed, or when it was delivered.",
    )


class AspectStepTrace(BaseModel):
    """One greedy step of the ASPECT facet saturation, arithmetic shown."""

    step: int
    candidate_id: str
    source_turn: int
    score: float = Field(
        description="This episode's CC80 score, the multiplier in every "
        "facet marginal of the step."
    )
    marginal: float = Field(
        description="Sum, over this episode's facets, of "
        "max(0, score * idf(facet) - coverage(facet)): how much uncovered "
        "faceted relevance it adds over what earlier choices already carry."
    )
    ratio: float = Field(
        description="marginal divided by the episode's additive character "
        "cost. The greedy takes the highest ratio, ties broken by CC80 rank "
        "then store index, so long episodes are penalized."
    )
    additive_chars: int
    cumulative_chars: int
    covered_total: int = Field(
        description="Size of the coverage map after this admission: how many "
        "distinct facets the running selection accounts for."
    )


class CC80Detail(BaseModel):
    """How this turn's CC80 fusion was scaled.

    Broken out because min-max normalization makes every ranking number
    query-relative: the same episode scores 0.00 on one turn and 0.84 on
    another. A constant component (one candidate, an empty query token
    stream, or all-equal vectors) normalizes to all zeros and is flagged
    rather than silently diluted.
    """

    dense_weight: float
    bm25_k1: float
    bm25_b: float
    dense_min: float
    dense_max: float
    dense_constant: bool
    bm25_min: float
    bm25_max: float
    bm25_constant: bool


class AspectDetail(BaseModel):
    """The protected ASPECT half: what it admitted and why it stopped.

    ``mode`` says which path this turn actually took:

    - ``off``      - ASPECT disabled in the store's config.
    - ``protected`` - full pipeline: initial CC80 half, facet-spread half,
      slack return.
    - ``fallback``  - ASPECT was enabled but either no eligible episode
      remained or the initial half admitted nothing; the full budget went
      to a single CC80 walk instead.

    ``initial_ids`` and the returned list are long-term admissions that the
    semantic path owns, even on a protected turn, because they come from the
    CC80 walk; ``spread_ids`` are the ones only the saturation produced.
    """

    enabled: bool
    share: float
    model: str
    mode: Literal["off", "protected", "fallback"]
    facet_latency_ms: float | None = Field(
        default=None,
        description="Wall time of parsing the store into facets this turn. "
        "None when no spread ran. Excluded from verification: latency is "
        "not byte-reproducible.",
    )
    initial_ids: list[str]
    spread_ids: list[str]
    returned_ids: list[str] = Field(
        description="Candidates the slack walk admitted after initial plus "
        "spread, in CC80 rank order."
    )
    solo_chars: int | None = Field(
        default=None,
        description="The spread's own accounting: starting from the empty "
        "tags, how many characters its running selection spent against the "
        "half allowance. None when no spread ran.",
    )
    stopping_reason: str | None = Field(
        default=None,
        description="Why the saturation stopped: no_complete_candidate_fits "
        "or no_positive_marginal. None when no spread ran.",
    )
    steps: list[AspectStepTrace] = Field(default_factory=list)


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


class PackingDecision(BaseModel):
    """One admission attempt, in the order packing made it."""

    order: int
    candidate_id: str
    tier: TierName
    phase: Literal["full", "initial", "spread", "slack"]
    cost_chars: int
    payload_chars_after: int
    admitted: bool
    reason: str


class PackingTrace(BaseModel):
    """How the long-term budget was spent, decision by decision.

    Decisions are ordered exactly as the library made them, across the
    phases that ran this turn: ``full`` (a single CC80 walk over the whole
    allowance - the off and fallback shapes), ``initial`` (the CC80 half
    walk), ``spread`` (the ASPECT-selected candidates packed against the
    other half), and ``slack`` (the final return, charged at exact
    additive cost until the allowance ran out).
    """

    policy: str = Field(
        description="The named drop policy from the library. Candidates are "
        "considered in decision order and a candidate that does not fit is "
        "skipped rather than ending the walk."
    )
    phases: list[Literal["full", "initial", "spread", "slack"]]
    budget_chars: int = Field(
        description="The long-term allowance this walk governed. Recent "
        "continuity is additive and sits outside it."
    )
    half_chars: int = Field(
        description="int(budget * aspect_share): each protected half's "
        "allowance. Zero when no protected turn ran."
    )
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
    against it. On the CC-007 path ``chars_delivered`` is the total output
    and may exceed ``budget_chars`` because recency is additive; the pair
    ``retrieval_chars_delivered`` / ``retrieval_budget_chars`` is the part
    the allowance governed.
    """

    chars_delivered: int
    chars_wanted: int
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
    retrieval_chars_delivered: int | None = None
    retrieval_budget_chars: int | None = None
    recency_count: int = 0
    semantic_count: int = 0
    aspect_count: int = 0
    returned_semantic_count: int = 0
    aspect_enabled: bool = False
    recent_ids: list[str] = Field(default_factory=list)
    recency_additive: bool = False

    @property
    def chars_available(self) -> int:
        """Unused long-term allowance, excluding additive recent continuity."""
        delivered = (
            self.chars_delivered
            if self.retrieval_chars_delivered is None
            else self.retrieval_chars_delivered
        )
        budget = (
            self.budget_chars
            if self.retrieval_budget_chars is None
            else self.retrieval_budget_chars
        )
        return budget - delivered

    @property
    def shortfall_chars(self) -> int:
        """How much more allowance the proposed selection would have needed."""
        delivered = (
            self.chars_delivered
            if self.retrieval_chars_delivered is None
            else self.retrieval_chars_delivered
        )
        return max(0, self.chars_wanted - delivered)


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

    schema_version: Literal[2] = 2

    turn_id: str
    session_id: str
    turn_index: int
    started_at: datetime
    total_ms: float | None = None

    query: QueryTrace
    store: StoreTrace

    candidates: list[CandidateTrace]
    tiers: list[TierTrace]
    cc80_detail: CC80Detail
    aspect_detail: AspectDetail
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
        """How much of the long-term allowance the retrieval block used.

        Measured on the retrieval pair, not the total: recent continuity is
        additive and renders outside the allowance, so a fully packed
        retrieval plus a recent window would read over 100% of the total.
        """
        budget = self.report.retrieval_budget_chars
        delivered = self.report.retrieval_chars_delivered
        if not budget or budget <= 0 or delivered is None:
            return 0.0
        return delivered / budget


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
    recency_count: int = 0
    semantic_count: int = 0
    aspect_count: int = 0
