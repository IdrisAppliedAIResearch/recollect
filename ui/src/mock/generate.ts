/**
 * A mock TurnTrace, simulated rather than hard-coded.
 *
 * The deployed timeline read path is replayed here in TypeScript against
 * the corpus: raw cosine over the complete store, unioned with the last 32
 * exchanges, rendered chronologically. No ranking, no allowance, no drops.
 * Every number in the resulting trace agrees with every other number, and
 * `context_block.payload` really is what the reported selection produced.
 *
 * What is stood in for:
 *   - cosine: a topic-affinity model calibrated against the pinned encoder
 *     (Qwen3-Embedding-0.6B-Q8_0), measured 2026-09-19 on graded pairs —
 *     paraphrase 0.89, same topic 0.74, same entity 0.46, unrelated 0.23.
 *     The threshold sits in the gap between the third and fourth, so the
 *     mock must span that range or the demo would show a mechanism that
 *     never fires. An earlier version of this file was scaled to a 0.2779
 *     ceiling carried from the CC80 research; under a 0.48 threshold that
 *     would have retrieved nothing, on every turn, forever.
 */
import type {
  CandidateTrace,
  GenerationTrace,
  ReportTrace,
  SelectionPath,
  TimelineDetail,
  TurnTrace,
} from '../types/trace.ts'
import { additiveWeight, renderStmPayload } from '../lib/render.ts'
import { TOPICS, buildCorpus, mulberry32, type MockEpisode } from './corpus.ts'

// -- mechanism constants, from the store-pinned EpisodicConfig ---------------
const RECENCY_WINDOW_N = 32
const TIMELINE_THRESHOLD = 0.48
const READ_POLICY = 'timeline'
const LIBRARY_VERSION = '0.3.0'
const CARRIED_EMBEDDER_SHA256 =
  '06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439'

/**
 * The deployment ceiling, scaled to this corpus.
 *
 * NOT the deployed 64,000: these synthetic episodes run several thousand
 * characters each, where real ones measured a median of 367 (2026-09-19,
 * across the 18 local stores). At 64,000 the continuity window alone would
 * exceed the ceiling on every mock turn, which would demonstrate the
 * degenerate case and nothing else. Scaled here so the ceiling engages and
 * still reaches its target, which is the behaviour worth showing.
 */
const MOCK_CONTEXT_CEILING = 150_000

const STORE_SIZE = 120

export type MockScenario = 'deployed' | 'diverged'

// ---------------------------------------------------------------------------
// Pseudo-embeddings and pseudo-lexical scores
// ---------------------------------------------------------------------------

/** Fixed cross-topic affinity. Same every run, so cosines are reproducible. */
const TOPIC_AFFINITY: number[][] = (() => {
  const random = mulberry32(0x5eed)
  const size = TOPICS.length
  const matrix: number[][] = Array.from({ length: size }, () =>
    new Array<number>(size).fill(0),
  )
  for (let i = 0; i < size; i += 1) {
    matrix[i]![i] = 1
    for (let j = i + 1; j < size; j += 1) {
      const value = 0.05 + random() * 0.55
      matrix[i]![j] = value
      matrix[j]![i] = value
    }
  }
  return matrix
})()

/**
 * Cosines for one query, on the pinned encoder's measured scale.
 *
 * Affinity 1 (same topic) lands near the measured 0.74, and the floor sits
 * near the measured 0.23 for unrelated content, so the 0.48 threshold falls
 * where it really falls: admitting same-topic episodes and refusing the
 * merely adjacent. The jitter is what puts a handful of episodes either
 * side of the line, which is the whole point of the demo.
 */
const COSINE_FLOOR = 0.21
const COSINE_CEILING = 0.92

function cosinesFor(
  episodes: MockEpisode[],
  queryTopic: number,
  seed: number,
): Map<string, number> {
  const random = mulberry32(seed)
  const result = new Map<string, number>()
  episodes.forEach((episode) => {
    const affinity = TOPIC_AFFINITY[queryTopic]![episode.topic]!
    // Recency has a mild lexical pull in real embeddings; keep a little of it.
    const drift = 1 - Math.min(0.25, (episodes.length - episode.turn_number) / 900)
    const shaped = Math.max(0, Math.min(1, affinity * drift + (random() - 0.5) * 0.3))
    const score = COSINE_FLOOR + shaped * (COSINE_CEILING - COSINE_FLOOR)
    result.set(episode.id, Number(score.toFixed(6)))
  })
  return result
}

const PREVIEW_CHARS = 240

function preview(value: string): string {
  const text = value.trim().replace(/\n/g, ' ')
  if (text.length <= PREVIEW_CHARS) return text
  return `${text.slice(0, PREVIEW_CHARS - 1)}…`
}

/** Not a real SHA-256 — a stable stand-in of the right shape and length. */
function fakeSha(input: string): string {
  let h1 = 0x811c9dc5
  let h2 = 0x01000193
  for (let i = 0; i < input.length; i += 1) {
    h1 = Math.imul(h1 ^ input.charCodeAt(i), 0x01000193) >>> 0
    h2 = Math.imul(h2 + input.charCodeAt(i) * (i + 1), 0x85ebca6b) >>> 0
  }
  let out = ''
  let state = (h1 ^ h2) >>> 0
  const random = mulberry32(state)
  for (let i = 0; i < 64; i += 1) {
    out += Math.floor(random() * 16).toString(16)
  }
  return out
}

/**
 * The store's config pin, as EpisodicConfig serializes it.
 *
 * The legacy CC80/ASPECT fields are still present because the library still
 * carries them — they keep its historical checks runnable and are unused by
 * the timeline. A pin that omitted them would not round-trip, and a store
 * whose pin does not round-trip is one the app refuses to open.
 */
function storeConfigJson(): string {
  return JSON.stringify({
    recency_window_n: RECENCY_WINDOW_N,
    retrieval_budget_chars: 32_000,
    semantic_dense_weight: 0.8,
    bm25_k1: 1.2,
    bm25_b: 0.75,
    aspect_enabled: false,
    aspect_share: 0.5,
    aspect_model: 'en_core_web_sm',
    k_threshold: 0.48,
    candidate_policy: 'full_store',
    unsafe_cosine_top_n: 100,
    selector: 'A3',
    selector_lambda: 0.1,
    selector_cost_exponent: 0.0,
    selector_cluster_count: 16,
    budget_accounting: 'exact_serialized',
    embedder_sha256: CARRIED_EMBEDDER_SHA256,
    embed_call_shape: 'solo',
    seed: 5005,
    read_policy: READ_POLICY,
    timeline_threshold: TIMELINE_THRESHOLD,
  })
}

export interface MockTurnInput {
  sessionId: string
  turnIndex: number
  queryText: string
  queryTopic: number
  storeSize?: number
  scenario?: MockScenario
  withGeneration?: boolean
  responseText?: string
}

export function generateTurn(input: MockTurnInput): TurnTrace {
  const {
    sessionId,
    turnIndex,
    queryText,
    queryTopic,
    storeSize = STORE_SIZE,
    scenario = 'deployed',
    withGeneration = true,
  } = input

  const episodes = buildCorpus(storeSize)
  const seed = 5005 + turnIndex * 17
  const cosines = cosinesFor(episodes, queryTopic, seed)

  // -- the two conditions -------------------------------------------------
  // Source order is (turn_number, id); the corpus is already built that way,
  // and the continuity slice is a tail of it.
  const eligible = episodes
  const recent = eligible.slice(Math.max(0, eligible.length - RECENCY_WINDOW_N))
  const recentIds = new Set(recent.map((e) => e.id))
  const relevant = eligible.filter(
    (episode) => cosines.get(episode.id)! >= TIMELINE_THRESHOLD,
  )
  const relevantIds = new Set(relevant.map((e) => e.id))

  // -- the union, chronologically ------------------------------------------
  const wouldDeliver = eligible.filter(
    (episode) => recentIds.has(episode.id) || relevantIds.has(episode.id),
  )

  // -- the deployment ceiling, applied BEFORE the library sees anything ----
  // A deviation, taken for hardware; see CeilingTrace. Continuity is never
  // shed, so the ceiling can be exceeded rather than losing what was just
  // said. Relevance-qualified episodes go lowest-cosine first, one at a
  // time against an exact re-render.
  const withheldIds = new Set<string>()
  if (MOCK_CONTEXT_CEILING > 0) {
    const sheddable = wouldDeliver
      .filter((episode) => !recentIds.has(episode.id))
      .sort((a, b) => cosines.get(a.id)! - cosines.get(b.id)!)
    let surviving = wouldDeliver
    for (const episode of sheddable) {
      if (renderStmPayload([], surviving).length <= MOCK_CONTEXT_CEILING) break
      withheldIds.add(episode.id)
      surviving = surviving.filter((kept) => kept.id !== episode.id)
    }
  }

  const selected = wouldDeliver.filter((e) => !withheldIds.has(e.id))
  const selectedIds = selected.map((e) => e.id)
  const deliveredIds = new Set(selectedIds)
  // What the library was shown, not what the store holds: a withheld
  // episode was never offered to it.
  const shownRelevant = relevant.filter((e) => !withheldIds.has(e.id))
  const overlapIds = eligible
    .filter((e) => recentIds.has(e.id) && relevantIds.has(e.id))
    .map((e) => e.id)
  const relevanceOnlyCount = shownRelevant.filter(
    (e) => !recentIds.has(e.id),
  ).length

  // -- the payload the model saw ------------------------------------------
  // Recency is inside the union here, not rendered as a separate block: the
  // timeline delivers one chronological run.
  const payload = renderStmPayload([], selected)

  // -- candidate rows -------------------------------------------------------
  const candidates: CandidateTrace[] = episodes.map((episode) => {
    const isRecent = recentIds.has(episode.id)
    const isRelevant = relevantIds.has(episode.id)
    const delivered = deliveredIds.has(episode.id)
    const via: SelectionPath | null = !delivered
      ? null
      : isRelevant && isRecent
        ? 'both'
        : isRelevant
          ? 'relevance'
          : 'continuity'
    const cosine = cosines.get(episode.id)!
    return {
      id: episode.id,
      turn_number: episode.turn_number,
      preview: preview(episode.user_message),
      assistant_preview: preview(episode.assistant_message),
      cosine,
      margin: Number((cosine - TIMELINE_THRESHOLD).toFixed(6)),
      render_chars: additiveWeight(episode),
      eligible: true,
      relevant: isRelevant,
      in_recency_window: isRecent,
      is_anchor: false,
      withheld: withheldIds.has(episode.id),
      delivered,
      delivered_via: via,
    }
  })

  const timeline: TimelineDetail = {
    read_policy: READ_POLICY,
    relevance_threshold: TIMELINE_THRESHOLD,
    recency_window_n: RECENCY_WINDOW_N,
    eligible_count: eligible.length - withheldIds.size,
    relevant_ids: shownRelevant.map((e) => e.id),
    recent_ids: recent.map((e) => e.id),
    selected_ids: selectedIds,
    overlap_ids: overlapIds,
    relevance_only_count: relevanceOnlyCount,
    through_turn: null,
    anchor_turn: null,
  }


  // -- verification ---------------------------------------------------------
  const authoritySha = fakeSha(payload)
  const diverged = scenario === 'diverged'
  const shadowSha = diverged ? fakeSha(`${payload} drift`) : authoritySha

  const startedAt = new Date(Date.parse('2026-08-17T14:03:00Z') + turnIndex * 96_000)

  const generation: GenerationTrace | null = withGeneration
    ? {
        model: 'qwen3-8b-instruct',
        base_url: 'http://127.0.0.1:8000/v1',
        system_prompt_chars: 612,
        context_block_chars: payload.length,
        total_prompt_chars: 612 + payload.length + queryText.length,
        response_text: input.responseText ?? mockResponse(queryTopic, turnIndex),
        response_chars: (input.responseText ?? mockResponse(queryTopic, turnIndex)).length,
        reasoning_text:
          turnIndex % 2 === 0
            ? 'The recent block covers the same subject, so I can answer from it directly. Nothing older was delivered, so I should not claim to recall anything beyond what is here.'
            : '',
        thinking_enabled: turnIndex % 2 === 0,
        ttft_ms: 1180 + turnIndex * 37,
        total_ms: 6120 + turnIndex * 210,
        tokens_out: 214 + turnIndex * 9,
        tokens_per_sec: 41.7,
        prompt_cache: {
          prompt_tokens: 9040 + Math.floor(payload.length / 4),
          // The block is rebuilt every turn by design, so most of the prefix
          // is forfeit. That is the point of recording it.
          cached_tokens: 612,
          processed_tokens: 9040 + Math.floor(payload.length / 4) - 612,
          prefill_ms: 5870,
        },
        finish_reason: 'stop',
        error: null,
        tool_calls: [],
      }
    : null

  const report: ReportTrace = {
    chars_delivered: payload.length,
    // Nothing is held back, so what was wanted is what was delivered.
    chars_wanted: payload.length,
    episodes_delivered: deliveredIds.size,
    stm_count: recent.length,
    k_count: relevanceOnlyCount,
    latency_ms: 71.4 + (turnIndex % 4) * 2.6,
    pool_size: episodes.length,
    read_policy: READ_POLICY,
    relevance_threshold: TIMELINE_THRESHOLD,
    eligible_count: eligible.length - withheldIds.size,
    selected_ids: selectedIds,
    retrieval_chars_delivered: payload.length,
    recency_count: recent.length,
    semantic_count: relevanceOnlyCount,
    recent_ids: recent.map((e) => e.id),
    recency_additive: true,
    through_turn: null,
    anchor_turn: null,
  }

  return {
    schema_version: 3,
    turn_id: `turn-${sessionId}-${String(turnIndex).padStart(3, '0')}`,
    session_id: sessionId,
    turn_index: turnIndex,
    started_at: startedAt.toISOString(),
    total_ms: withGeneration ? 6420 + turnIndex * 210 : null,

    query: {
      text: queryText,
      chars: queryText.length,
      embedding_sha256: fakeSha(`query:${queryText}`),
      embedding_norm: 1.0,
      embed_latency_ms: 54.8 + (turnIndex % 5) * 1.3,
      embed_cache_hit: false,
    },
    store: {
      path: `var/sessions/${sessionId}/episodes.sqlite`,
      episode_count: episodes.length,
      config_json: storeConfigJson(),
      sentinel_sha256: fakeSha('sentinel'),
      embedder_model_sha256: fakeSha('Qwen3-Embedding-0.6B-Q8_0.gguf'),
    },

    candidates,
    timeline,
    ceiling: {
      ceiling_chars: MOCK_CONTEXT_CEILING || null,
      engaged: withheldIds.size > 0,
      store_episodes: episodes.length,
      considered_episodes: episodes.length - withheldIds.size,
      withheld_ids: [...withheldIds],
      withheld_chars: episodes
        .filter((e) => withheldIds.has(e.id))
        .reduce((sum, e) => sum + additiveWeight(e), 0),
    },

    context_block: {
      payload,
      chars: payload.length,
      sha256: authoritySha,
      recent_episode_count: recent.length,
      retrieved_episode_count: selectedIds.length,
    },
    report,
    verification: {
      payload_identical: !diverged,
      report_fields_identical: !diverged,
      authority_payload_sha256: authoritySha,
      shadow_payload_sha256: shadowSha,
      mismatched_fields: diverged
        ? [
            'chars_delivered: shadow=31284 authority=31996',
            'semantic_count: shadow=1 authority=0',
          ]
        : [],
      library_version: LIBRARY_VERSION,
      shadow_latency_ms: 8.4 + (turnIndex % 3) * 0.7,
    },
    generation,
    subagent: null,
  }
}

function mockResponse(topicIndex: number, turnIndex: number): string {
  const topic = TOPICS[topicIndex % TOPICS.length]!
  return [
    `Going on what we worked through before: the thing to check first is ${topic.nouns[turnIndex % topic.nouns.length]}.`,
    `You mentioned ${topic.issues[turnIndex % topic.issues.length]} last time, and that is the same shape of problem.`,
    '',
    `Try this: ${topic.actions[turnIndex % topic.actions.length]}. If it works you should see ${topic.outcomes[turnIndex % topic.outcomes.length]} fairly quickly.`,
    '',
    'If that does not move it, tell me what changed and we will narrow it down from there.',
  ].join('\n')
}

// ---------------------------------------------------------------------------
// A whole session
// ---------------------------------------------------------------------------

export interface MockTurnSeed {
  query: string
  topic: number
  storeSize: number
}

export const MOCK_TURN_SEEDS: MockTurnSeed[] = [
  { query: 'remind me what we decided about the compost heap', topic: 6, storeSize: 116 },
  { query: 'is the achilles thing something I should see someone about', topic: 1, storeSize: 117 },
  { query: 'what was the storage advice for the puerh again', topic: 13, storeSize: 118 },
  { query: 'did we settle on mirrors or raidz for the eight drives', topic: 11, storeSize: 119 },
  {
    query: 'pulling this together — what are the open threads across everything we have talked about?',
    topic: 7,
    storeSize: STORE_SIZE,
  },
]

export function generateMockSession(
  sessionId: string,
  scenario: MockScenario = 'deployed',
): TurnTrace[] {
  return MOCK_TURN_SEEDS.map((seed, index) =>
    generateTurn({
      sessionId,
      turnIndex: index + 1,
      queryText: seed.query,
      queryTopic: seed.topic,
      storeSize: seed.storeSize,
      // Only the last turn carries the divergence, so the healthy badge is
      // still reachable in the diverged scenario.
      scenario: scenario === 'diverged' && index === MOCK_TURN_SEEDS.length - 1
        ? 'diverged'
        : 'deployed',
    }),
  )
}
