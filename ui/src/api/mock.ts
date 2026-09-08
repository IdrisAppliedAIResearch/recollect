/**
 * The mock data source.
 *
 * Implements the same `DataSource` contract as `live.ts` against generated
 * traces, including a token-by-token fake stream with the same event order
 * the server promises: `retrieval` first and complete, then tokens, then
 * `done`. That ordering is the thing the inspector is built around, so the
 * mock has to honour it.
 */
import type {
  ChatEvent,
  DataSource,
  EpisodeBody,
  HealthResponse,
  SessionInfo,
} from '../types/api.ts'
import type { TurnSummary, TurnTrace } from '../types/trace.ts'
import { starvedTiers } from '../lib/derive.ts'
import { buildCorpus } from '../mock/corpus.ts'
import {
  MOCK_TURN_SEEDS,
  generateMockSession,
  generateTurn,
  type MockScenario,
} from '../mock/generate.ts'

export const MOCK_SESSION_ID = 'mock-0001'

function summarize(trace: TurnTrace): TurnSummary {
  return {
    turn_id: trace.turn_id,
    session_id: trace.session_id,
    turn_index: trace.turn_index,
    started_at: trace.started_at,
    query_preview: trace.query.text.slice(0, 160),
    response_preview: (trace.generation?.response_text ?? '').slice(0, 160),
    episodes_delivered: trace.report.episodes_delivered,
    episodes_dropped: trace.report.episodes_dropped,
    chars_delivered: trace.report.chars_delivered,
    budget_chars: trace.report.budget_chars,
    stm_count: trace.report.stm_count,
    k_count: trace.report.k_count,
    coverage_count: trace.report.coverage_count,
    starved_tiers: starvedTiers(trace),
    trace_trustworthy:
      trace.verification.payload_identical && trace.verification.report_fields_identical,
    recency_count: trace.report.recency_count,
    semantic_count: trace.report.semantic_count,
    aspect_count: trace.report.aspect_count,
  }
}

const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

export function createMockSource(scenario: MockScenario = 'deployed'): DataSource {
  const turns: TurnTrace[] = generateMockSession(MOCK_SESSION_ID, scenario)

  const session: SessionInfo = {
    session_id: MOCK_SESSION_ID,
    created_at: '2026-08-14T09:12:00.000Z',
    turn_count: turns.length,
    title: 'Long-running assistant (mock)',
  }

  return {
    kind: 'mock',

    async health(): Promise<HealthResponse> {
      await delay(40)
      return {
        ok: true,
        // Same field names the live health endpoint reports, so the top bar
        // chip renders identically in both data sources.
        embedder: {
          model: 'Qwen3-Embedding-0.6B-Q8_0.gguf',
          n_threads: 8,
          n_ctx: 512,
          embedding_dimension: 1024,
          in_process: true,
          cold_load_ms: 754.2,
          calls: 37,
          cache_hits: 4,
          cache_entries: 33,
          hit_ratio: 4 / 37,
        },
        generator: {
          base_url: 'http://127.0.0.1:8000/v1',
          model: 'qwen3-8b-instruct',
          reachable: false,
          note: 'mock data source — no generator contacted',
        },
        library_version: '0.2.0',
        episodic_config: {
          aspect_enabled: true,
          aspect_model: 'en_core_web_sm',
          aspect_share: 0.5,
          bm25_b: 0.75,
          bm25_k1: 1.2,
          budget_accounting: 'exact_serialized',
          candidate_policy: 'full_store',
          embed_call_shape: 'solo',
          k_threshold: 0.48,
          recency_window_n: 32,
          retrieval_budget_chars: 32_000,
          semantic_dense_weight: 0.8,
          seed: 5005,
          selector: 'A3',
          selector_cluster_count: 16,
          selector_cost_exponent: 0,
          selector_lambda: 0.1,
          unsafe_cosine_top_n: 100,
        },
        budget_chars: 32_000,
      }
    },

    async listSessions() {
      await delay(30)
      return [{ ...session, turn_count: turns.length }]
    },

    async createSession(title?: string) {
      await delay(30)
      return { ...session, title: title ?? session.title }
    },

    async listTurns() {
      await delay(30)
      return turns.map(summarize)
    },

    async chatHistory() {
      await delay(30)
      return turns.map((trace) => ({
        turn_id: trace.turn_id,
        user_message: trace.query.text,
        assistant_message: trace.generation?.response_text ?? '',
        reasoning_text: trace.generation?.reasoning_text ?? '',
        error: trace.generation?.error ?? null,
      }))
    },

    async getTurn(turnId: string) {
      await delay(20)
      const found = turns.find((t) => t.turn_id === turnId)
      if (!found) throw new Error(`no mock trace for ${turnId}`)
      return found
    },

    async getEpisode(_sessionId: string, episodeId: string): Promise<EpisodeBody> {
      await delay(20)
      const episodes = buildCorpus(200)
      const found = episodes.find((e) => e.id === episodeId)
      if (!found) throw new Error(`no mock episode ${episodeId}`)
      return {
        id: found.id,
        turn_number: found.turn_number,
        user_message: found.user_message,
        assistant_message: found.assistant_message,
      }
    },

    async chat(
      _sessionId: string,
      message: string,
      onEvent: (event: ChatEvent) => void,
      signal?: AbortSignal,
    ) {
      const turnIndex = turns.length + 1
      const seed = MOCK_TURN_SEEDS[(turnIndex - 1) % MOCK_TURN_SEEDS.length]!
      const trace = generateTurn({
        sessionId: MOCK_SESSION_ID,
        turnIndex,
        queryText: message,
        queryTopic: seed.topic,
        storeSize: 120 + (turnIndex - MOCK_TURN_SEEDS.length),
        scenario,
        withGeneration: true,
      })

      // Retrieval is complete before a single token exists. That is the
      // contract, and the inspector depends on it.
      await delay(320)
      if (signal?.aborted) return
      const retrievalOnly: TurnTrace = { ...trace, generation: null }
      onEvent({ type: 'retrieval', trace: retrievalOnly })

      const generation = trace.generation!
      if (generation.reasoning_text) {
        for (const chunk of chunks(generation.reasoning_text, 14)) {
          if (signal?.aborted) return
          await delay(18)
          onEvent({ type: 'reasoning', text: chunk })
        }
      }
      for (const chunk of chunks(generation.response_text, 9)) {
        if (signal?.aborted) return
        await delay(22)
        onEvent({ type: 'token', text: chunk })
      }

      turns.push(trace)
      onEvent({ type: 'done', turn_id: trace.turn_id, generation, total_ms: trace.total_ms })
    },
  }
}

function chunks(text: string, size: number): string[] {
  const parts: string[] = []
  for (let i = 0; i < text.length; i += size) parts.push(text.slice(i, i + size))
  return parts
}
