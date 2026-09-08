import assert from 'node:assert/strict'
import test from 'node:test'

import { appendVoiceDraft, takeVoiceDraft } from '../src/lib/voice-draft.ts'
import { initialVoiceState } from '../src/voice/client.ts'

function recovery(text = 'Move my appointment to four thirty.') {
  return {
    sessionId: 'original',
    state: { ...initialVoiceState, recoveredDraft: text, error: 'Microphone disconnected.' },
  }
}

test('an unexpected voice failure transfers unsent text into an editable draft once', () => {
  const first = takeVoiceDraft(recovery(), 'original')
  const drafts = appendVoiceDraft({}, 'original', first.text)
  assert.equal(drafts.original, 'Move my appointment to four thirty.')
  assert.equal(first.snapshot.state.recoveredDraft, null)
  assert.equal(first.snapshot.state.error, 'Microphone disconnected.')
  assert.equal(first.snapshot.state.phase, 'off')
  assert.equal(takeVoiceDraft(first.snapshot, 'original').text, null)
})

test('recovery preserves an existing typed draft and unrelated session drafts', () => {
  const original = { original: 'A note I was already typing.', another: 'Other conversation.' }
  const result = appendVoiceDraft(original, 'original', 'Captured voice words.')
  assert.equal(result.original, 'A note I was already typing.\n\nCaptured voice words.')
  assert.equal(result.another, original.another)
  assert.equal(original.original, 'A note I was already typing.')
})

test('a session switch cannot consume another conversation\'s recovered speech', () => {
  const pending = recovery()
  for (const sessionId of ['another', null]) {
    const result = takeVoiceDraft(pending, sessionId)
    assert.equal(result.text, null)
    assert.equal(result.snapshot, pending)
  }
})

test('explicit stop or a fresh voice session has no recovered text to append', () => {
  const reset = { sessionId: 'original', state: { ...initialVoiceState } }
  assert.equal(takeVoiceDraft(reset, 'original').text, null)
  assert.equal(takeVoiceDraft(recovery(''), 'original').text, null)
})
