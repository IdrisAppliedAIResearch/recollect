/**
 * Hand-written mirror of `src/recollect/trace.py` (schema v3, timeline read
 * path).
 *
 * Kept field-for-field with the Pydantic models. Two deliberate differences:
 *
 * 1. `datetime` fields arrive as ISO-8601 strings.
 * 2. Pydantic `@property` accessors (`VerificationTrace.trustworthy`,
 *    `TurnTrace.relevance_only_ids`, `PromptCacheTrace.cache_hit_ratio`) are
 *    NOT part of `model_dump()`, so they are absent from the wire. They are
 *    recomputed in `src/lib/derive.ts` from the same definitions.
 */

// ---------------------------------------------------------------------------
// Selection vocabulary
// ---------------------------------------------------------------------------

/**
 * How a delivered episode earned its place. The timeline is a union, so an
 * episode can qualify both ways at once; `both` stays distinct from either
 * alone because collapsing it would overstate what the threshold retrieved
 * on its own.
 */
export type SelectionPath = 'relevance' | 'continuity' | 'both' | 'anchor'

export const PATH_ORDER: readonly SelectionPath[] = [
  'relevance',
  'both',
  'continuity',
  'anchor',
] as const

/** The one-letter pips: N continuity, K relevance, B both, A anchor. */
export const PATH_CODES: Record<SelectionPath, string> = {
  relevance: 'K',
  continuity: 'N',
  both: 'B',
  anchor: 'A',
}

export const PATH_LABELS: Record<SelectionPath, string> = {
  relevance: 'RELEVANT',
  continuity: 'RECENT',
  both: 'RELEVANT + RECENT',
  anchor: 'ANCHOR',
}

export const PATH_DESCRIPTIONS: Record<SelectionPath, string> = {
  relevance:
    'Cleared the relevance threshold on raw cosine against the query, measured over the complete store. No ranking, no capacity: every episode at or above the threshold is delivered.',
  continuity:
    'Inside the last recency_window_n exchanges by source order. Delivered as continuity regardless of how it scored, so the immediate conversation is never lost to a low cosine.',
  both:
    'Qualified on relevance and fell inside the continuity window. Delivered once; the timeline is a union, not a concatenation.',
  anchor:
    'An explicitly protected exchange, admitted regardless of relevance or recency because the caller named its turn.',
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

  /** Measured cosine against the query, float64 over float32 vectors. */
  cosine: number
  /** cosine - relevance_threshold. Negative means it missed. */
  margin: number

  render_chars: number

  /** Within the caller's source-order horizon; false only under through_turn. */
  eligible: boolean
  /** Cosine at or above the threshold. Independent of the window. */
  relevant: boolean
  /** Inside the trailing recency_window_n slice of eligible episodes. */
  in_recency_window: boolean
  /** Named by the caller's anchor_turn, so protected either way. */
  is_anchor: boolean
  /**
   * Cleared the threshold and would have been delivered, but the deployment
   * ceiling excluded it. Kept distinct from a near miss because the two mean
   * opposite things: one was not relevant enough, the other was relevant and
   * did not fit.
   */
  withheld: boolean

  delivered: boolean
  /** Which condition admitted it. `both` when relevance and continuity agree. */
  delivered_via: SelectionPath | null
}

/**
 * The selection this turn made, and the settings that produced it.
 *
 * No ranking, no capacity and no drops means there is no decision sequence
 * to show - only the two conditions and what each admitted. The overlap is
 * the part worth reading: when everything relevant was already recent, the
 * threshold contributed nothing the window would not have carried anyway.
 */
export interface TimelineDetail {
  read_policy: string
  relevance_threshold: number
  recency_window_n: number
  /** Episodes inside the horizon; the whole store unless through_turn was set. */
  eligible_count: number
  /** Cleared the threshold, in source order. Includes any also in the window. */
  relevant_ids: string[]
  /** The trailing continuity slice, in source order. */
  recent_ids: string[]
  /** The delivered union, chronologically - the order the block renders. */
  selected_ids: string[]
  /** Qualified on both counts. */
  overlap_ids: string[]
  /** What the threshold added that continuity would not have delivered. */
  relevance_only_count: number
  through_turn: number | null
  anchor_turn: number | null
}

/**
 * Recollect's hardware ceiling — which the library's mechanism has not.
 *
 * Deliberately separate from `TimelineDetail` because it is a deviation.
 * The library delivers every episode at or above the threshold and caps
 * nothing. That is unrunnable on a 32K-context local model, so Recollect
 * decides which episodes are *handed to* the library; the library then does
 * exactly what it always does with the set it is given. Trimming the
 * payload afterwards would make shadow and authority disagree byte-for-byte
 * and refuse every turn.
 *
 * Continuity is never withheld, so the ceiling can be exceeded by the
 * recency window alone rather than dropping what was just said.
 */
export interface CeilingTrace {
  /** The ceiling in characters, or null when disabled. */
  ceiling_chars: number | null
  /** True only when it actually withheld something. */
  engaged: boolean
  /** Episodes in the store. report.pool_size counts only what the library saw. */
  store_episodes: number
  considered_episodes: number
  /** Threshold-qualified episodes excluded, lowest cosine first. */
  withheld_ids: string[]
  withheld_chars: number
}

export interface ContextBlockTrace {
  payload: string
  chars: number
  sha256: string
  recent_episode_count: number
  retrieved_episode_count: number
}

/**
 * The library's own ContextReport. Only the fields the timeline populates
 * are carried: the budget/drop/aspect columns are structurally constant on
 * this path and are asserted in `shadow._verify` rather than stored, so a
 * reader is never shown a column of zeros to interpret.
 */
export interface ReportTrace {
  chars_delivered: number
  chars_wanted: number
  episodes_delivered: number
  /** Legacy names, carried verbatim: stm = continuity, k = relevance-only. */
  stm_count: number
  k_count: number
  latency_ms: number
  /** The whole store. */
  pool_size: number
  read_policy: string
  relevance_threshold: number
  eligible_count: number
  selected_ids: string[]
  retrieval_chars_delivered: number | null
  recency_count: number
  semantic_count: number
  recent_ids: string[]
  recency_additive: boolean
  through_turn: number | null
  anchor_turn: number | null
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
  /** Supplemental task input is outside the verified retrieval payload. */
  task_context_chars?: number
  task_ids?: string[]
  model_queue_ms?: number | null

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
  schema_version: 3
  turn_id: string
  session_id: string
  turn_index: number
  /** ISO-8601. */
  started_at: string
  total_ms: number | null

  query: QueryTrace
  store: StoreTrace

  candidates: CandidateTrace[]
  timeline: TimelineDetail
  ceiling: CeilingTrace

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
  chars_delivered: number
  stm_count: number
  k_count: number
  eligible_count: number
  trace_trustworthy: boolean
  recency_count: number
  semantic_count: number
  relevance_only_count: number
  /** The ceiling withheld episodes the mechanism would have delivered. */
  ceiling_engaged: boolean
}
