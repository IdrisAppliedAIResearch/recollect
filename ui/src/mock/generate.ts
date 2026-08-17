/**
 * A mock TurnTrace, simulated rather than hard-coded.
 *
 * The retrieval pipeline is replayed here in TypeScript against the same
 * rules the library uses — the same greedy selector (lambda 0.1, r 0, k 16),
 * the same skip-on-overflow packing walk in recency→similarity→coverage
 * order, the same renderer. Every number in the resulting trace therefore
 * agrees with every other number, and `context_block.payload` really is what
 * the reported packing decisions produced.
 *
 * The measured constants it reproduces:
 *   - similarity threshold 0.48, best cosine ~0.27, so the path is inert
 *   - recency window 32, packed first, spending the budget
 *   - coverage selecting as though it owns all 32,000 characters
 */
import type {
  CandidateTrace,
  ClusterTrace,
  GenerationTrace,
  PackingDecision,
  SelectorStepTrace,
  TierName,
  TierTrace,
  TurnTrace,
} from '../types/trace.ts'
import { TIER_DESCRIPTIONS, TIER_LABELS } from '../types/trace.ts'
import {
  EMPTY_PAYLOAD_CHARS,
  additiveWeight,
  renderStmPayload,
} from '../lib/render.ts'
import { TOPICS, buildCorpus, mulberry32, type MockEpisode } from './corpus.ts'

// -- mechanism constants, from EpisodicConfig -------------------------------
const RECENCY_WINDOW_N = 32
const K_THRESHOLD = 0.48
const SELECTOR_LAMBDA = 0.1
const SELECTOR_COST_EXPONENT = 0.0
const CLUSTER_COUNT = 16
const BUDGET_CHARS = 32_000
const DROP_POLICY = 'marginal_gain_order_skip_on_overflow'
const LIBRARY_VERSION = '0.1.0'
/** _selection.wrapper_chars(): fixed two-block cost of a non-empty selection. */
const WRAPPER_CHARS = 51

const STORE_SIZE = 120

export type MockScenario = 'deployed' | 'diverged'

// ---------------------------------------------------------------------------
// Pseudo-embedding: a topic-similarity model scaled to the measured ceiling
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
 * Cosines for one query. Scaled so the best in the store lands near 0.2779 —
 * the highest score the research ever recorded for known-relevant content.
 */
function relevanceFor(
  episodes: MockEpisode[],
  queryTopic: number,
  seed: number,
): Map<string, number> {
  const random = mulberry32(seed)
  const raw = episodes.map((episode) => {
    const affinity = TOPIC_AFFINITY[queryTopic]![episode.topic]!
    // Recency has a mild lexical pull in real embeddings; keep a little of it.
    const drift = 1 - Math.min(0.25, (episodes.length - episode.turn_number) / 900)
    return Math.max(0.005, affinity * drift + (random() - 0.5) * 0.22)
  })
  const peak = Math.max(...raw)
  const ceiling = 0.2779 - (seed % 7) * 0.004
  const scale = ceiling / peak
  const result = new Map<string, number>()
  episodes.forEach((episode, index) => {
    result.set(episode.id, Number((raw[index]! * scale).toFixed(6)))
  })
  return result
}

/**
 * Cluster assignment. Real `deterministic_clusters` is farthest-first plus
 * Lloyd over the actual vectors; here topic is the signal with a realistic
 * bleed, so cluster sizes come out uneven the way k-means output does.
 */
function clusterAssignments(episodes: MockEpisode[], seed: number): Map<string, number> {
  const random = mulberry32(seed ^ 0x9e37)
  const assignments = new Map<string, number>()
  for (const episode of episodes) {
    let cluster = episode.topic
    if (random() < 0.14) {
      cluster = (cluster + 1 + Math.floor(random() * 3)) % CLUSTER_COUNT
    }
    assignments.set(episode.id, cluster)
  }
  return assignments
}

// ---------------------------------------------------------------------------
// The mechanism, replayed
// ---------------------------------------------------------------------------

interface SelectionOutcome {
  selectedIds: string[]
  steps: SelectorStepTrace[]
}

/** `_selection.select` with ClusterDiversitySelector. */
function runSelector(
  pool: MockEpisode[],
  relevance: Map<string, number>,
  clusters: Map<string, number>,
  budget: number,
): SelectionOutcome {
  const costs = new Map(pool.map((e) => [e.id, additiveWeight(e)]))
  const remaining = new Set(pool.map((e) => e.id));
  const byId = new Map(pool.map((e) => [e.id, e]))
  const covered = new Set<number>()
  const selectedIds: string[] = []
  const steps: SelectorStepTrace[] = []
  let spent = 0

  for (;;) {
    const affordable = pool.filter(
      (e) => remaining.has(e.id) && WRAPPER_CHARS + spent + costs.get(e.id)! <= budget,
    )
    if (affordable.length === 0) break

    let best: MockEpisode | null = null
    let bestKey: [number, number, number, string] | null = null
    for (const episode of affordable) {
      const rel = Math.max(relevance.get(episode.id) ?? 0, 0)
      const novel = covered.has(clusters.get(episode.id)!) ? 0 : 1
      const objective = rel + SELECTOR_LAMBDA * novel
      // r = 0, so cost^r is 1 and cost does not influence the choice.
      const scaled = objective / Math.pow(costs.get(episode.id)!, SELECTOR_COST_EXPONENT)
      const key: [number, number, number, string] = [
        -scaled,
        costs.get(episode.id)!,
        episode.turn_number,
        episode.id,
      ]
      if (bestKey === null || compareKey(key, bestKey) < 0) {
        best = episode
        bestKey = key
      }
    }
    if (!best) break

    const rel = Math.max(relevance.get(best.id) ?? 0, 0)
    const cluster = clusters.get(best.id)!
    const enteredNew = !covered.has(cluster)
    const objective = rel + SELECTOR_LAMBDA * (enteredNew ? 1 : 0)
    const cost = costs.get(best.id)!
    spent += cost
    covered.add(cluster)
    remaining.delete(best.id)
    selectedIds.push(best.id)
    steps.push({
      step: steps.length + 1,
      candidate_id: best.id,
      source_turn: byId.get(best.id)!.turn_number,
      relevance: rel,
      objective_gain: objective,
      scaled_gain: objective / Math.pow(cost, SELECTOR_COST_EXPONENT),
      additive_chars: cost,
      cumulative_chars: WRAPPER_CHARS + spent,
      entered_new_cluster: enteredNew,
      cluster,
    })
  }
  return { selectedIds, steps }
}

function compareKey(
  a: [number, number, number, string],
  b: [number, number, number, string],
): number {
  if (a[0] !== b[0]) return a[0] - b[0]
  if (a[1] !== b[1]) return a[1] - b[1]
  if (a[2] !== b[2]) return a[2] - b[2]
  return a[3] < b[3] ? -1 : a[3] > b[3] ? 1 : 0
}

interface PackingOutcome {
  decisions: PackingDecision[]
  packedRecent: MockEpisode[]
  packedStm: MockEpisode[]
  duplicates: string[]
}

/** `shadow._replay_packing`. Skip-on-overflow, not stop-on-overflow. */
function replayPacking(
  recent: MockEpisode[],
  stmCandidates: MockEpisode[],
  budget: number,
): PackingOutcome {
  const packedRecent: MockEpisode[] = []
  const packedStm: MockEpisode[] = []
  const decisions: PackingDecision[] = []
  const duplicates: string[] = []
  const seen = new Set<string>()

  const walk: Array<[MockEpisode, TierName]> = [
    ...recent.map((e) => [e, 'recency'] as [MockEpisode, TierName]),
    ...stmCandidates.map((e) => [e, 'coverage'] as [MockEpisode, TierName]),
  ]

  let order = 0
  for (const [candidate, tier] of walk) {
    order += 1
    const cost = additiveWeight(candidate)

    if (seen.has(candidate.id)) {
      duplicates.push(candidate.id)
      decisions.push({
        order,
        candidate_id: candidate.id,
        tier,
        cost_chars: cost,
        payload_chars_after: renderStmPayload(packedRecent, packedStm).length,
        admitted: false,
        reason: 'already admitted by an earlier path; charged once',
      })
      continue
    }

    const target = tier === 'recency' ? packedRecent : packedStm
    target.push(candidate)
    const payload = renderStmPayload(packedRecent, packedStm)
    if (payload.length <= budget) {
      seen.add(candidate.id)
      decisions.push({
        order,
        candidate_id: candidate.id,
        tier,
        cost_chars: cost,
        payload_chars_after: payload.length,
        admitted: true,
        reason: 'fits',
      })
      continue
    }
    target.pop()
    decisions.push({
      order,
      candidate_id: candidate.id,
      tier,
      cost_chars: cost,
      payload_chars_after: renderStmPayload(packedRecent, packedStm).length,
      admitted: false,
      reason: `would reach ${payload.length} characters, past the ${budget} budget; skipped and the walk continued`,
    })
  }
  return { decisions, packedRecent, packedStm, duplicates }
}

// ---------------------------------------------------------------------------
// Turn assembly
// ---------------------------------------------------------------------------

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
  const relevance = relevanceFor(episodes, queryTopic, seed)
  const clusters = clusterAssignments(episodes, seed)

  // -- ranks -----------------------------------------------------------
  const ranked = [...episodes].sort((a, b) => {
    const diff = (relevance.get(b.id) ?? 0) - (relevance.get(a.id) ?? 0)
    if (diff !== 0) return diff
    return a.turn_number - b.turn_number
  })
  const rankOf = new Map(ranked.map((e, i) => [e.id, i + 1]))

  // -- tiers -----------------------------------------------------------
  const recent = episodes.slice(Math.max(0, episodes.length - RECENCY_WINDOW_N))
  const recentIds = new Set(recent.map((e) => e.id))

  const similarityHits = episodes.filter((e) => (relevance.get(e.id) ?? 0) >= K_THRESHOLD)
  const similarityIds = new Set(similarityHits.map((e) => e.id))

  const pool = episodes // candidate_policy = full_store
  const selection = runSelector(pool, relevance, clusters, BUDGET_CHARS)
  const byId = new Map(episodes.map((e) => [e.id, e]))
  const coverage = selection.selectedIds.map((id) => byId.get(id)!)
  const coverageIds = new Set(selection.selectedIds)

  // -- packing ---------------------------------------------------------
  const stmCandidates = [...similarityHits, ...coverage]
  const packed = replayPacking(recent, stmCandidates, BUDGET_CHARS)
  const payload = renderStmPayload(packed.packedRecent, packed.packedStm)
  const deliveredIds = new Set([
    ...packed.packedRecent.map((e) => e.id),
    ...packed.packedStm.map((e) => e.id),
  ])

  // -- what the paths wanted -------------------------------------------
  const wantedStm: MockEpisode[] = []
  const wantedSeen = new Set(recentIds)
  for (const episode of stmCandidates) {
    if (wantedSeen.has(episode.id)) continue
    wantedSeen.add(episode.id)
    wantedStm.push(episode)
  }
  const charsWanted = renderStmPayload(recent, wantedStm).length
  const droppedIds = [...recent, ...wantedStm]
    .filter((e) => !deliveredIds.has(e.id))
    .map((e) => e.id)

  // -- candidate rows ---------------------------------------------------
  const dropReasonOf = new Map<string, string>()
  for (const decision of packed.decisions) {
    if (!decision.admitted && !dropReasonOf.has(decision.candidate_id)) {
      dropReasonOf.set(decision.candidate_id, decision.reason)
    }
  }

  const candidates: CandidateTrace[] = episodes.map((episode) => {
    const delivered = deliveredIds.has(episode.id)
    const via: TierName | null = !delivered
      ? null
      : recentIds.has(episode.id)
        ? 'recency'
        : similarityIds.has(episode.id)
          ? 'similarity'
          : 'coverage'
    const proposed =
      recentIds.has(episode.id) || similarityIds.has(episode.id) || coverageIds.has(episode.id)
    const dropReason = delivered
      ? null
      : proposed
        ? (dropReasonOf.get(episode.id) ?? 'proposed but not admitted')
        : 'not proposed by any path this turn'

    return {
      id: episode.id,
      turn_number: episode.turn_number,
      preview: preview(episode.user_message),
      assistant_preview: preview(episode.assistant_message),
      relevance: relevance.get(episode.id) ?? 0,
      relevance_rank: rankOf.get(episode.id)!,
      cluster: clusters.get(episode.id) ?? null,
      render_chars: additiveWeight(episode),
      in_recency_window: recentIds.has(episode.id),
      passes_similarity_threshold: similarityIds.has(episode.id),
      selected_by_coverage: coverageIds.has(episode.id),
      delivered,
      delivered_via: via,
      drop_reason: dropReason,
    }
  })

  // -- cluster rows -----------------------------------------------------
  const membersByCluster = new Map<number, string[]>()
  for (const episode of pool) {
    const cluster = clusters.get(episode.id)!
    const bucket = membersByCluster.get(cluster) ?? []
    bucket.push(episode.id)
    membersByCluster.set(cluster, bucket)
  }
  const clusterRows: ClusterTrace[] = [...membersByCluster.keys()]
    .sort((a, b) => a - b)
    .map((id) => {
      const members = membersByCluster.get(id)!
      const scores = members.map((memberId) => relevance.get(memberId) ?? 0)
      return {
        id,
        size: members.length,
        member_ids: members,
        mean_relevance: scores.reduce((a, b) => a + b, 0) / (scores.length || 1),
        max_relevance: scores.length ? Math.max(...scores) : 0,
        selected_ids: members.filter((memberId) => coverageIds.has(memberId)),
        delivered_ids: members.filter((memberId) => deliveredIds.has(memberId)),
      }
    })

  // -- tier rows --------------------------------------------------------
  const similarityClaim = new Set([...similarityIds].filter((id) => !recentIds.has(id)))
  const coverageClaim = new Set(
    [...coverageIds].filter((id) => !recentIds.has(id) && !similarityIds.has(id)),
  )

  function buildTier(name: TierName, proposed: MockEpisode[], claim: Set<string>): TierTrace {
    const proposedIds = proposed.map((e) => e.id)
    const delivered = proposedIds.filter((id) => deliveredIds.has(id) && claim.has(id))
    // In the context, but credited to an earlier path that also proposed it.
    const overlapped = proposedIds.filter((id) => deliveredIds.has(id) && !claim.has(id))
    const skipped = proposedIds.filter((id) => !deliveredIds.has(id))
    return {
      name,
      label: TIER_LABELS[name],
      description: TIER_DESCRIPTIONS[name],
      proposed_ids: proposedIds,
      delivered_ids: delivered,
      overlapped_ids: overlapped,
      skipped_ids: skipped,
      chars_delivered: delivered.reduce((sum, id) => sum + additiveWeight(byId.get(id)!), 0),
      chars_proposed: proposed.reduce((sum, e) => sum + additiveWeight(e), 0),
    }
  }

  const tiers: TierTrace[] = [
    buildTier('recency', recent, recentIds),
    buildTier('similarity', similarityHits, similarityClaim),
    buildTier('coverage', coverage, coverageClaim),
  ]

  // -- similarity detail ------------------------------------------------
  const bestCosine = Math.max(...episodes.map((e) => relevance.get(e.id) ?? 0))

  // -- verification -----------------------------------------------------
  const authoritySha = fakeSha(payload)
  const diverged = scenario === 'diverged'
  const shadowSha = diverged ? fakeSha(`${payload} drift`) : authoritySha

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
      }
    : null

  return {
    schema_version: 1,
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
      config_json: JSON.stringify(
        {
          recency_window_n: RECENCY_WINDOW_N,
          k_threshold: K_THRESHOLD,
          candidate_policy: 'full_store',
          selector_lambda: SELECTOR_LAMBDA,
          selector_cost_exponent: SELECTOR_COST_EXPONENT,
          selector_cluster_count: CLUSTER_COUNT,
          embedding_model: 'Qwen3-Embedding-0.6B-Q8_0',
          embedding_dimension: 1024,
        },
        null,
        2,
      ),
      sentinel_sha256: fakeSha('sentinel'),
      embedder_model_sha256: fakeSha('Qwen3-Embedding-0.6B-Q8_0.gguf'),
    },

    candidates,
    clusters: clusterRows,
    tiers,
    similarity_detail: {
      threshold: K_THRESHOLD,
      hit_count: similarityHits.length,
      max_relevance_observed: bestCosine,
      margin_to_threshold: K_THRESHOLD - bestCosine,
      inert: similarityHits.length === 0,
    },
    selector_steps: selection.steps,
    packing: {
      policy: DROP_POLICY,
      tier_order: ['recency', 'similarity', 'coverage'],
      budget_chars: BUDGET_CHARS,
      empty_payload_chars: EMPTY_PAYLOAD_CHARS,
      decisions: packed.decisions,
      duplicate_ids: packed.duplicates,
    },

    context_block: {
      payload,
      chars: payload.length,
      sha256: authoritySha,
      recent_episode_count: packed.packedRecent.length,
      retrieved_episode_count: packed.packedStm.length,
    },
    report: {
      chars_delivered: payload.length,
      chars_wanted: charsWanted,
      chars_available: BUDGET_CHARS - payload.length,
      shortfall_chars: Math.max(0, charsWanted - payload.length),
      episodes_delivered: deliveredIds.size,
      episodes_dropped: droppedIds.length,
      truncated: droppedIds.length > 0,
      stm_count: [...deliveredIds].filter((id) => recentIds.has(id)).length,
      k_count: [...deliveredIds].filter((id) => similarityIds.has(id) && !recentIds.has(id))
        .length,
      coverage_count: [...deliveredIds].filter(
        (id) => !recentIds.has(id) && !similarityIds.has(id),
      ).length,
      latency_ms: 71.4 + (turnIndex % 4) * 2.6,
      pool_size: pool.length,
      dropped_ids: droppedIds,
      drop_policy: DROP_POLICY,
      budget_chars: BUDGET_CHARS,
    },
    verification: {
      payload_identical: !diverged,
      report_fields_identical: !diverged,
      authority_payload_sha256: authoritySha,
      shadow_payload_sha256: shadowSha,
      mismatched_fields: diverged
        ? [
            "chars_delivered: shadow=31284 authority=31996",
            "coverage_count: shadow=1 authority=0",
          ]
        : [],
      library_version: LIBRARY_VERSION,
      shadow_latency_ms: 8.4 + (turnIndex % 3) * 0.7,
    },
    generation,
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
