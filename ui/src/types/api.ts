import type { GenerationTrace, TurnSummary, TurnTrace } from './trace.ts'

export interface HealthResponse {
  ok: boolean
  embedder: Record<string, unknown>
  generator: Record<string, unknown>
  library_version: string
  /** The EpisodicConfig the server runs under, as the library serializes it. */
  episodic_config?: Record<string, unknown>
  /** The deployed context budget in characters (RecollectConfig, not the library). */
  budget_chars?: number
}

export interface SessionInfo {
  session_id: string
  created_at: string
  turn_count: number
  title: string
}

export interface EpisodeBody {
  id: string
  turn_number: number
  user_message: string
  assistant_message: string
}

/** One subagent step, as streamed mid-turn. Ephemeral: never persisted. */
export interface SubagentStepEvent {
  index: number
  tool: string
  args: Record<string, unknown>
  observation: string
  ms: number
}

/**
 * SSE events emitted by POST /api/chat, in the order they arrive. The three
 * `subagent_*` events appear only when the main model delegated a task;
 * they stream between the two main-model generations.
 */
export type ChatEvent =
  | { type: 'retrieval'; trace: TurnTrace }
  | { type: 'token'; text: string }
  | { type: 'reasoning'; text: string }
  | {
      type: 'subagent_start'
      run_id: string
      task: string
      effort: 'focused' | 'deep'
    }
  | { type: 'subagent_step'; run_id: string; step: SubagentStepEvent }
  | {
      type: 'subagent_done'
      run_id: string
      ok: boolean
      steps: number
      sources: string[]
      returned_chars: number
      error?: string
    }
  | {
      type: 'done'
      turn_id: string
      generation: GenerationTrace
      total_ms: number | null
      committed?: boolean
    }
  | { type: 'error'; message: string }

export interface DataSource {
  readonly kind: 'live' | 'mock'
  health(): Promise<HealthResponse>
  listSessions(): Promise<SessionInfo[]>
  createSession(title?: string): Promise<SessionInfo>
  listTurns(sessionId: string): Promise<TurnSummary[]>
  getTurn(turnId: string): Promise<TurnTrace>
  getEpisode(sessionId: string, episodeId: string): Promise<EpisodeBody>
  chat(
    sessionId: string,
    message: string,
    onEvent: (event: ChatEvent) => void,
    signal?: AbortSignal,
    inputMode?: 'text' | 'voice',
  ): Promise<void>
}
