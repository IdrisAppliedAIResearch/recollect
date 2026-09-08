import type { ChatEvent } from '../types/api.ts'

interface SendOutcome {
  completedId: string | null
  speechTurnId: string | null
  committed: boolean
  failed: boolean
}

export const initialSendOutcome: SendOutcome = {
  completedId: null, speechTurnId: null, committed: false, failed: false,
}

export function updateSendOutcome(
  current: SendOutcome,
  event: Extract<ChatEvent, { type: 'done' | 'error' }>,
): SendOutcome {
  if (event.type === 'error') {
    return current.completedId ? current : { ...current, failed: true }
  }
  const generation = event.generation
  const canSpeak = !generation.error && Boolean(generation.response_text.trim())
  return {
    completedId: event.turn_id,
    speechTurnId: canSpeak ? event.turn_id : null,
    // Current servers report the actual write. Older servers and the mock
    // supplied only the final response, so retain their nonempty-success rule.
    committed: event.committed ?? (!generation.error && Boolean(generation.response_text)),
    failed: !canSpeak,
  }
}
