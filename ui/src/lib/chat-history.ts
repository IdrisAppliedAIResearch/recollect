import type { Exchange } from '../App.tsx'
import type { ChatTurn } from '../types/api.ts'

export function restoreHistory(turns: ChatTurn[]): Exchange[] {
  return turns.map((turn) => ({
    id: turn.turn_id,
    ...(turn.started_at ? { startedAt: turn.started_at } : {}),
    user: turn.user_message,
    assistant: turn.assistant_message,
    reasoning: turn.reasoning_text,
    error: turn.error,
    trace: null,
    streaming: false,
    workspace: null,
  }))
}
