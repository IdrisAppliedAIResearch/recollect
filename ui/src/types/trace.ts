/**
 * Hand-written mirror of `src/recollect/trace.py`.
 *
 * Kept field-for-field with the Pydantic models. Two deliberate differences:
 *
 * 1. `datetime` fields arrive as ISO-8601 strings.
 * 2. Pydantic `@property` accessors (`TierTrace.starved`,
 *    `VerificationTrace.trustworthy`, `ClusterTrace.covered`,
 *    `TurnTrace.starved_tiers`, `TurnTrace.budget_utilization`,
 *    `PromptCacheTrace.cache_hit_ratio`) are NOT part of `model_dump()`, so
 *    they are absent from the wire. They are recomputed in `src/lib/derive.ts`
 *    from the same definitions.
 */

// ---------------------------------------------------------------------------
// Tier vocabulary
// ---------------------------------------------------------------------------

export type TierName = 'recency' | 'similarity' | 'coverage'

export const TIER_ORDER: readonly TierName[] = [
  'recency',
  'similarity',
  'coverage',
] as const

export const TIER_LABELS: Record<TierName, string> = {
  recency: 'RECENT',
  similarity: 'RELATED',
  coverage: 'SPREAD',
}

/** The report-field shorthand the research uses: stm_count / k_count / coverage_count. */
export const TIER_CODES: Record<TierName, string> = {
  recency: 'N',
  similarity: 'K',
  coverage: 'A3',
}

export const TIER_DESCRIPTIONS: Record<TierName, string> = {
  recency:
    'The last N episodes in conversation order. No scoring involved. Packed first, so it spends the budget before anything else is considered.',
  similarity:
    'Episodes whose cosine against the query clears a fixed threshold. Measured inert on the internal corpus: the threshold sits above the highest score relevant content reaches.',
  coverage:
    'A budgeted greedy over the whole store: relevance plus a bonus for entering a topic cluster not yet covered. It selects as though it owns the entire budget, and is then packed last.',
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

  relevance: number
  /** 1 = highest cosine this turn. */
  relevance_rank: number
  cluster: number | null

  render_chars: number

  in_recency_window: boolean
  passes_similarity_threshold: boolean
  selected_by_coverage: boolean

  delivered: boolean
  /** The path that claimed it first; attribution follows packing order. */
  delivered_via: TierName | null
  drop_reason: string | null
}

export interface SelectorStepTrace {
  step: number
  candidate_id: string
  source_turn: number
  relevance: number
  objective_gain: number
  scaled_gain: number
  additive_chars: number
  cumulative_chars: number
  entered_new_cluster: boolean
  cluster: number | null
}

export interface ClusterTrace {
  id: number
  size: number
  member_ids: string[]
  mean_relevance: number
  max_relevance: number
  selected_ids: string[]
  delivered_ids: string[]
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

export interface SimilarityTierDetail {
  threshold: number
  hit_count: number
  max_relevance_observed: number
  /** threshold - best cosine. Positive means nothing could have cleared the bar. */
  margin_to_threshold: number
  inert: boolean
}

export interface PackingDecision {
  order: number
  candidate_id: string
  tier: TierName
  cost_chars: number
  payload_chars_after: number
  admitted: boolean
  reason: string
}

export interface PackingTrace {
  policy: string
  tier_order: TierName[]
  budget_chars: number
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
  chars_delivered: number
  chars_wanted: number
  chars_available: number
  shortfall_chars: number
  episodes_delivered: number
  episodes_dropped: number
  truncated: boolean
  stm_count: number
  k_count: number
  coverage_count: number
  latency_ms: number
  pool_size: number
  dropped_ids: string[]
  drop_policy: string
  budget_chars: number
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
}

// ---------------------------------------------------------------------------
// The turn
// ---------------------------------------------------------------------------

export interface TurnTrace {
  schema_version: 1
  turn_id: string
  session_id: string
  turn_index: number
  /** ISO-8601. */
  started_at: string
  total_ms: number | null

  query: QueryTrace
  store: StoreTrace

  candidates: CandidateTrace[]
  clusters: ClusterTrace[]
  tiers: TierTrace[]
  similarity_detail: SimilarityTierDetail
  selector_steps: SelectorStepTrace[]
  packing: PackingTrace

  context_block: ContextBlockTrace
  report: ReportTrace
  verification: VerificationTrace
  generation: GenerationTrace | null
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
}
