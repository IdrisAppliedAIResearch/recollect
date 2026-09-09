import { useCallback, useEffect, useRef, useState } from 'react'

import { takeVoiceDraft, type VoiceSnapshot } from '../lib/voice-draft.ts'
import { initialVoiceState, VoiceClient } from './client.ts'

export function useVoice(
  sessionId: string | null,
  enabled: boolean,
  send: (
    message: string,
    signal?: AbortSignal,
    inputMode?: 'text' | 'voice',
  ) => Promise<string | null>,
) {
  const [snapshot, setSnapshot] = useState<VoiceSnapshot>({
    sessionId: null, state: initialVoiceState,
  })
  const snapshotRef = useRef(snapshot)
  const currentSession = useRef(sessionId)
  currentSession.current = sessionId
  const client = useRef<VoiceClient | null>(null)
  const sendRef = useRef(send)
  sendRef.current = send

  const stop = useCallback(() => {
    client.current?.stop()
    client.current = null
    snapshotRef.current = { sessionId: null, state: initialVoiceState }
    setSnapshot(snapshotRef.current)
  }, [])

  const takeRecoveredDraft = useCallback(() => {
    const recovery = takeVoiceDraft(snapshotRef.current, sessionId)
    if (recovery.text !== null) {
      snapshotRef.current = recovery.snapshot
      setSnapshot(recovery.snapshot)
    }
    return recovery.text
  }, [sessionId])

  const pauseMicrophone = useCallback(() => client.current?.pauseMicrophone(), [])
  const resumeMicrophone = useCallback(() => client.current?.resumeMicrophone(), [])
  const stopReply = useCallback(() => client.current?.stopReply(), [])
  const replayLast = useCallback(() => client.current?.replayLast(), [])
  const sendCaptured = useCallback(() => client.current?.sendCaptured(), [])
  const discardCaptured = useCallback(() => client.current?.discardCaptured(), [])
  const speakNotification = useCallback((notificationId: string) => {
    return sessionId !== null && currentSession.current === sessionId &&
      (client.current?.speakNotification(sessionId, notificationId) ?? false)
  }, [sessionId])
  const stopNotification = useCallback(() => client.current?.stopNotification(), [])

  useEffect(() => stop, [sessionId, enabled, stop])

  const start = useCallback(() => {
    if (!enabled || !sessionId) return
    client.current?.stop()
    const next = new VoiceClient({
      workletUrl: `${import.meta.env.BASE_URL}assets/voice-capture.js`,
      onState: (state) => {
        if (client.current !== next || currentSession.current !== sessionId) return
        snapshotRef.current = { sessionId, state }
        setSnapshot(snapshotRef.current)
      },
      send: (message, signal, inputMode) =>
        client.current === next && currentSession.current === sessionId
          ? sendRef.current(message, signal, inputMode) : Promise.resolve(null),
    })
    client.current = next
    void next.start()
  }, [enabled, sessionId])

  const state = snapshot.sessionId === sessionId && enabled
    ? snapshot.state : initialVoiceState
  return {
    ...state, active: state.phase !== 'off', start, stop,
    pauseMicrophone, resumeMicrophone, stopReply, replayLast, sendCaptured, discardCaptured,
    takeRecoveredDraft,
    speakNotification, stopNotification,
  }
}

export type VoiceControl = ReturnType<typeof useVoice>
