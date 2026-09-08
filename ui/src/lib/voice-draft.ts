import type { VoiceState } from '../voice/client.ts'

export interface VoiceSnapshot {
  sessionId: string | null
  state: VoiceState
}

export function takeVoiceDraft(snapshot: VoiceSnapshot, sessionId: string | null) {
  if (!sessionId || snapshot.sessionId !== sessionId || !snapshot.state.recoveredDraft) {
    return { snapshot, text: null }
  }
  return {
    snapshot: { ...snapshot, state: { ...snapshot.state, recoveredDraft: null } },
    text: snapshot.state.recoveredDraft,
  }
}

export function appendVoiceDraft(
  drafts: Record<string, string>, sessionId: string, text: string,
): Record<string, string> {
  const existing = drafts[sessionId] ?? ''
  return { ...drafts, [sessionId]: existing ? `${existing}\n\n${text}` : text }
}
