import type { GenerationTrace, TurnSummary, TurnTrace } from './trace.ts'

export interface HealthResponse {
  ok: boolean
  embedder: Record<string, unknown>
  generator: Record<string, unknown>
  library_version: string
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
  created_at: string
}

/** SSE events emitted by POST /api/chat, in the order they arrive. */
export type ChatEvent =
  | { type: 'retrieval'; trace: TurnTrace }
  | { type: 'token'; text: string }
  | { type: 'reasoning'; text: string }
  | { type: 'done'; turn_id: string; generation: GenerationTrace }
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
  ): Promise<void>
}
