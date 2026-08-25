/**
 * Hand-written mirror of `src/recollect/trace.py` (schema v2, CC-007 read
 * path).
 *
 * Kept field-for-field with the Pydantic models. Two deliberate differences:
 *
 * 1. `datetime` fields arrive as ISO-8601 strings.
 * 2. Pydantic `@property` accessors (`TierTrace.starved`,
 *    `TierTrace.fully_overlapped`, `TierTrace.contributed`,
 *    `ReportTrace.chars_available`, `ReportTrace.shortfall_chars`,
 *    `VerificationTrace.trustworthy`, `TurnTrace.starved_tiers`,
 *    `TurnTrace.budget_utilization`, `PromptCacheTrace.cache_hit_ratio`) are
 *    NOT part of `model_dump()`, so they are absent from the wire. They are
 *    recomputed in `src/lib/derive.ts` from the same definitions.
 */

// ---------------------------------------------------------------------------
// Tier vocabulary
// ---------------------------------------------------------------------------

export type TierName = 'recency' | 'semantic' | 'aspect'

export const TIER_ORDER: readonly TierName[] = [
  'recency',
  'semantic',
  'aspect',
] as const

export const TIER_LABELS: Record<TierName, string> = {
  recency: 'RECENT',
  semantic: 'SEMANTIC',
  aspect: 'ASPECT',
}

/** The report-field shorthand: recency_count / semantic_count / aspect_count. */
export const TIER_CODES: Record<TierName, string> = {
  recency: 'N',
  semantic: 'K',
  aspect: 'A',
}

export const TIER_DESCRIPTIONS: Record<TierName, string> = {
  recency:
    'The last recency_window_n episodes in conversation order. Rendered additively OUTSIDE the long-term budget: always delivered, never dropped, and excluded from long-term admission by identity.',
  semantic:
    'Long-term admission ranked by frozen CC80 over the complete store: dense cosine and BM25 each min-max normalized per query, fused 0.8 dense / 0.2 BM25, packed in rank order with skip-on-overflow. On ASPECT turns this is the initial half plus whatever the slack return rescued afterwards.',
  aspect:
    'The protected static ASPECT half: a greedy saturation over frozen parser facets (entity, date, number, event, relation, noun) that admits episodes whose facets are not yet covered, scored by CC80 score times facet idf, budgeted to the other half of the allowance.',
}

// ---------------------------------------------------------------------------
// Stage records
// ---------------------------------------------------------------------------

export interface QueryTrace {
  text: string
  chars: number
  /** SHA-256 of the query vector's float32 bytes. Drift here invalidates every cosine below. */
  embedding_sha256: string
  embedding_norm: number
  embed_latency_ms: number
  embed_cache_hit: boolean
}

export interface StoreTrace {
  path: string
  episode_count: number
  config_json: string
  sentinel_sha256: string
  embedder_model_sha256: string
}

export interface CandidateTrace {
  id: string
  turn_number: number
  preview: string
  assistant_preview: string

  /** Measured cosine, unnormalized. */
  dense_cosine: number
  /** The dense term after per-query min-max scaling, as it enters the CC80 fusion. */
  dense_normalized: number
  /** Raw Robertson BM25 against the tokenized query, before normalization. */
  bm25_score: number
  /** The BM25 term after per-query min-max scaling, as it enters the CC80 fusion. */
  bm25_normalized: number
  /** The fused CC80 score: 0.8 * dense_normalized + 0.2 * bm25_normalized. */
  cc80_score: number
  /** 1 = highest CC80 score this turn. Ties broken by turn number, then id. */
  cc80_rank: number

  render_chars: number

  in_recency_window: boolean
  in_semantic_initial: boolean
  selected_by_aspect: boolean
  returned_semantic: boolean

  delivered: boolean
  /** The path that claimed it first; attribution follows decision order. */
  delivered_via: TierName | null
  drop_reason: string | null
}

/** One greedy step of the ASPECT facet saturation, arithmetic shown. */
export interface AspectStepTrace {
  step: number
  candidate_id: string
  source_turn: number
  /** The episode's CC80 score, the multiplier in every facet marginal of the step. */
  score: number
  /** Sum, over the episode's facets, of max(0, score * idf - coverage(facet)). */
  marginal: number
  /** marginal divided by the episode's additive character cost. */
  ratio: number
  additive_chars: number
  /** The spread's own running spend, against the half allowance. */
  cumulative_chars: number
  /** Distinct facets the running selection accounts for after this admission. */
  covered_total: number
}

/** How this turn's CC80 fusion was scaled. */
export interface CC80Detail {
  dense_weight: number
  bm25_k1: number
  bm25_b: number
  dense_min: number
  dense_max: number
  dense_constant: boolean
  bm25_min: number
  bm25_max: number
  bm25_constant: boolean
}

export type AspectMode = 'off' | 'protected' | 'fallback'

/** The protected ASPECT half: what it admitted and why it stopped. */
export interface AspectDetail {
  enabled: boolean
  share: number
  model: string
  /** off = disabled in config; protected = full pipeline; fallback = one CC80 walk. */
  mode: AspectMode
  /** Wall time of parsing the store into facets. Null when no spread ran. */
  facet_latency_ms: number | null
  /** Long-term admissions from the initial CC80 half. */
  initial_ids: string[]
  /** The ones only the facet saturation produced. */
  spread_ids: string[]
  /** Slacked-back CC80 admits after initial plus spread. */
  returned_ids: string[]
  /** The spread's own spend against the half. Null when no spread ran. */
  solo_chars: number | null
  /** no_complete_candidate_fits or no_positive_marginal. Null when no spread ran. */
  stopping_reason: string | null
  steps: AspectStepTrace[]
}

export interface TierTrace {
  name: TierName
  label: string
  description: string
  proposed_ids: string[]
  /** Reached the context AND were credited to this path. */
  delivered_ids: string[]
  /** In the context, but credited to an earlier path that also proposed them. */
  overlapped_ids: string[]
  /** Absent from the context entirely: the budget was gone. */
  skipped_ids: string[]
  chars_delivered: number
  chars_proposed: number
}

export type PackPhase = 'full' | 'initial' | 'spread' | 'slack'

export interface PackingDecision {
  order: number
  candidate_id: string
  tier: TierName
  phase: PackPhase
  cost_chars: number
  payload_chars_after: number
  admitted: boolean
  reason: string
}

export interface PackingTrace {
  policy: string
  /** The phases that ran this turn, in the order they first made decisions. */
  phases: PackPhase[]
  /** The long-term allowance this walk governed. Recent continuity is outside it. */
  budget_chars: number
  /** int(budget * aspect_share). Zero when no protected turn ran. */
  half_chars: number
  /** Cost of the two empty block tags; a budget below this expresses nothing. */
  empty_payload_chars: number
  decisions: PackingDecision[]
  duplicate_ids: string[]
}

export interface ContextBlockTrace {
  payload: string
  chars: number
  sha256: string
  recent_episode_count: number
  retrieved_episode_count: number
}

export interface ReportTrace {
  /** Total output. May EXCEED budget_chars: recency renders additively outside the allowance. */
  chars_delivered: number
  /** How much the proposed long-term selection would have needed. */
  chars_wanted: number
  episodes_delivered: number
  episodes_dropped: number
  truncated: boolean
  /** Legacy names, carried verbatim from the library report. */
  stm_count: number
  k_count: number
  coverage_count: number
  latency_ms: number
  /** The whole store. */
  pool_size: number
  /** Long-term candidates that never reached the context, in rank order. */
  dropped_ids: string[]
  drop_policy: string
  /** The long-term allowance. */
  budget_chars: number
  /** Null on pre-CC-007 reports; the CC-007 path always carries the pair. */
  retrieval_chars_delivered: number | null
  retrieval_budget_chars: number | null
  recency_count: number
  semantic_count: number
  aspect_count: number
  returned_semantic_count: number
  aspect_enabled: boolean
  recent_ids: string[]
  recency_additive: boolean
}

export interface VerificationTrace {
  payload_identical: boolean
  report_fields_identical: boolean
  authority_payload_sha256: string
  shadow_payload_sha256: string
  mismatched_fields: string[]
  library_version: string
  shadow_latency_ms: number
}

export interface PromptCacheTrace {
  /** Total prompt length: processed + cached. */
  prompt_tokens: number | null
  /** Reused from the server's prefix cache. */
  cached_tokens: number | null
  /** Actually prefilled this request — the new work, not the total. */
  processed_tokens: number | null
  prefill_ms: number | null
}

export interface ToolCallTrace {
  id: string
  name: string
  /** The raw JSON string exactly as streamed, not a re-serialized dict. */
  arguments: string
}

/** One-line accounting for the subagent of a turn, if one ran. */
export interface SubagentTrace {
  task: string
  effort: 'focused' | 'deep'
  backend: 'legacy' | 'opencode'
  isolation: string
  fresh_context: boolean
  server_reused: boolean
  /** 'ok' | 'partial' | 'error'. */
  status: string
  steps: number
  tools_used: string[]
  sources: string[]
  returned_chars: number
  total_ms: number
  error: string | null
}

export interface GenerationTrace {
  model: string
  base_url: string
  system_prompt_chars: number
  context_block_chars: number
  total_prompt_chars: number

  response_text: string
  response_chars: number
  reasoning_text: string
  thinking_enabled: boolean

  ttft_ms: number | null
  total_ms: number | null
  tokens_out: number | null
  tokens_per_sec: number | null
  prompt_cache: PromptCacheTrace
  finish_reason: string | null
  error: string | null
  tool_calls: ToolCallTrace[]
}

// ---------------------------------------------------------------------------
// The turn
// ---------------------------------------------------------------------------

export interface TurnTrace {
  schema_version: 2
  turn_id: string
  session_id: string
  turn_index: number
  /** ISO-8601. */
  started_at: string
  total_ms: number | null

  query: QueryTrace
  store: StoreTrace

  candidates: CandidateTrace[]
  tiers: TierTrace[]
  cc80_detail: CC80Detail
  aspect_detail: AspectDetail
  packing: PackingTrace

  context_block: ContextBlockTrace
  report: ReportTrace
  verification: VerificationTrace
  generation: GenerationTrace | null
  /** Present only when the turn delegated to the ephemeral subagent. */
  subagent: SubagentTrace | null
}

export interface TurnSummary {
  turn_id: string
  session_id: string
  turn_index: number
  started_at: string
  query_preview: string
  response_preview: string
  episodes_delivered: number
  episodes_dropped: number
  chars_delivered: number
  budget_chars: number
  stm_count: number
  k_count: number
  coverage_count: number
  starved_tiers: string[]
  trace_trustworthy: boolean
  recency_count: number
  semantic_count: number
  aspect_count: number
}
