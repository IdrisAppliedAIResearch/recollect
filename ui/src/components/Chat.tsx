/**
 * The conversation. Each assistant message is a handle onto its own turn -
 * click it and the inspector rewinds to what memory did for that reply.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import { chars, clock, int, stamp } from '../lib/format.ts'
import { appendVoiceDraft } from '../lib/voice-draft.ts'
import { conversationEvents } from '../lib/conversation-events.ts'
import type { SessionInfo } from '../types/api.ts'
import type { TaskSnapshot } from '../types/tasks.ts'
import type { Exchange } from '../App.tsx'
import type { VoiceControl } from '../voice/useVoice.ts'
import { Markdown } from './Markdown.tsx'
import { Workspace } from './Workspace.tsx'
import { Sources, Tasks } from './Tasks.tsx'

interface Props {
  exchanges: Exchange[]
  selectedId: string | null
  onSelect: (id: string) => void
  onSend: (message: string) => void
  busy: boolean
  session: SessionInfo | null
  readOnly: boolean
  loading: boolean
  voice: VoiceControl
  tasks: TaskSnapshot
  taskError: string | null
}

export function Chat({
  exchanges,
  selectedId,
  onSelect,
  onSend,
  busy,
  session,
  readOnly,
  loading,
  voice,
  tasks,
  taskError,
}: Props) {
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const sessionId = session?.session_id ?? null
  const draft = sessionId ? drafts[sessionId] ?? '' : ''
  const setDraft = (text: string) => {
    if (sessionId) setDrafts((current) => ({ ...current, [sessionId]: text }))
  }
  const logRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const composerText = voice.active ? voice.partial : draft

  useEffect(() => {
    if (!sessionId) return
    const text = voice.takeRecoveredDraft()
    if (text !== null) setDrafts((current) => appendVoiceDraft(current, sessionId, text))
  }, [sessionId, voice.recoveredDraft, voice.takeRecoveredDraft])

  useLayoutEffect(() => {
    const input = inputRef.current
    if (!input) return
    input.style.height = '40px'
    input.style.height = `${Math.min(144, Math.max(40, input.scrollHeight + 2))}px`
    if (voice.active) input.scrollTop = input.scrollHeight
  }, [composerText, voice.active])

  useEffect(() => {
    const log = logRef.current
    if (log) log.scrollTop = log.scrollHeight
  }, [exchanges, tasks.notifications.at(-1)?.seq])

  // Going `disabled` while busy throws focus away, and it never comes back on
  // its own — so re-focus as soon as the composer is usable again, which is
  // what "ready to keep typing" means after each reply.
  useEffect(() => {
    if (!busy && !readOnly && !voice.active && !loading) inputRef.current?.focus()
  }, [busy, readOnly, voice.active, loading])

  const submit = () => {
    const message = draft.trim()
    if (!message || busy || readOnly || loading || voice.active) return
    setDraft('')
    onSend(message)
  }

  // Only one turn is in flight at a time, so the newest exchange that carries
  // a workspace is the live subagent arc (or the one that just finished).
  const workspace = exchanges.at(-1)?.workspace ?? null
  const researching =
    workspace?.phase === 'researching' || workspace?.phase === 'synthesizing'

  return (
    <div className="chat">
      <div className="chat__head">
        <span className="chat__title">{session?.title ?? 'No session'}</span>
        {session && (
          <span className="faint mono">
            {int(session.turn_count)} turn{session.turn_count === 1 ? '' : 's'}
          </span>
        )}
      </div>

      <div className="chat__log" ref={logRef}>
        {exchanges.length === 0 && tasks.notifications.length === 0 && (
          <div className="empty">
            <div className="empty__title">Nothing remembered yet</div>
            <p>
              Say something. Every reply is built from a context window
              reassembled from scratch, and the panel on the right shows
              exactly how it was chosen.
            </p>
          </div>
        )}

        {conversationEvents(exchanges, tasks.notifications).map((event) => {
          if (event.kind === 'notification') {
            const update = event.notification
            return <div key={event.id} className="turn task-update">
              {update.user_message && <div className="msg msg--user"><div className="msg__bubble">
                <Markdown text={update.user_message} />
              </div></div>}
              <div className="msg msg--assistant"><div className="msg__bubble">
                <div className="task-update__label">Research update · {update.kind}</div>
                <Markdown text={update.text} />
                {update.source_refs.length > 0 && <Sources sources={update.source_refs} />}
              </div><div className="msg__meta">
                <time dateTime={update.created_at} title={stamp(update.created_at)}>{clock(update.created_at)}</time>
                <span>{tasks.tasks.find((task) => task.task_id === update.task_id)?.objective ?? 'Saved task'}</span>
              </div></div>
            </div>
          }
          const exchange = event.exchange
          return (
          <div key={exchange.id} className="turn">
            <div className="msg msg--user">
              <div className="msg__bubble">
                <Markdown text={exchange.user} />
              </div>
            </div>

            {exchange.reasoning && (
              <div className="reasoning">
                <div className="reasoning__label">thinking</div>
                <Markdown text={exchange.reasoning} />
              </div>
            )}

            <div
              className={
                'msg msg--assistant' + (exchange.id === selectedId ? ' is-selected' : '')
              }
              onClick={() => onSelect(exchange.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') onSelect(exchange.id)
              }}
              title="Inspect this turn"
            >
              <div className="msg__bubble">
                {exchange.assistant ? (
                  <Markdown text={exchange.assistant} />
                ) : (
                  (exchange.streaming ? '…' : '')
                )}
                {exchange.error && <div className="callout callout--bad">{exchange.error}</div>}
              </div>

              {exchange.trace && (
                <div className="msg__meta mono">
                  <span title={stamp(exchange.trace.started_at)}>
                    {clock(exchange.trace.started_at)}
                  </span>
                  <span>{int(exchange.trace.report.episodes_delivered)} episodes</span>
                  <span>
                    {chars(
                      exchange.trace.report.retrieval_chars_delivered ??
                        exchange.trace.report.chars_delivered,
                    )}/
                    {chars(exchange.trace.report.budget_chars)}
                  </span>
                  <span className="msg__flags">
                    <TierPip code="N" n={exchange.trace.report.recency_count} tier="recency" />
                    <TierPip code="K" n={exchange.trace.report.semantic_count} tier="semantic" />
                    <TierPip code="A" n={exchange.trace.report.aspect_count} tier="aspect" />
                  </span>
                  {!exchange.trace.verification.payload_identical && (
                    <span className="badge badge--bad">unverified</span>
                  )}
                </div>
              )}
            </div>
          </div>
          )
        })}
      </div>

      {workspace && <Workspace workspace={workspace} />}
      <Tasks snapshot={tasks} error={taskError} />

      <div className="composer">
        <div className="voice" data-active={voice.active}>
          <div className="voice__status" role="status" aria-live="polite">
            <span className="voice__label">
              {voice.phase === 'off'
                ? 'Voice off'
                : voice.phase === 'starting'
                  ? 'Starting voice…'
                  : voice.phase === 'waiting'
                    ? `Say “${voice.wakePhrase}”`
                    : voice.phase === 'listening'
                      ? 'Listening — continue speaking'
                      : voice.phase === 'thinking'
                        ? voice.playbackKind === 'notification' ? 'Preparing research update…' : 'Thinking…'
                        : voice.phase === 'speaking'
                          ? voice.playbackKind === 'notification' ? 'Sharing research update…' : 'Speaking…'
                          : 'Microphone muted'}
            </span>
            <span className="voice__detail">
              {voice.error ||
                (voice.active
                  ? voice.micPaused
                    ? voice.pendingTranscript !== null
                      ? 'Microphone muted · captured text needs your review'
                      : voice.micResuming
                      ? 'Unmuting microphone…'
                      : 'Microphone muted · replies keep playing'
                    : voice.phase === 'waiting'
                    ? 'Say the wake phrase once to start a conversation'
                    : 'Microphone on · speak anytime to interrupt'
                  : 'Enable your microphone for hands-free conversation')}
            </span>
          </div>
          <button
            type="button"
            className={voice.active ? 'btn' : 'btn btn--ghost'}
            aria-pressed={voice.active}
            onClick={voice.active ? voice.stop : voice.start}
            disabled={!voice.active && (busy || readOnly || loading || !session)}
          >
            {voice.active ? 'Stop voice' : 'Enable voice'}
          </button>
          {voice.active && (
            <div className="voice__controls" role="group" aria-label="Voice controls">
              <button
                type="button"
                className="btn btn--ghost"
                onClick={voice.micPaused ? voice.resumeMicrophone : voice.pauseMicrophone}
                aria-pressed={voice.micPaused}
                disabled={voice.phase === 'starting' || voice.micResuming ||
                  voice.pendingTranscript !== null}
                title={voice.micPaused
                  ? 'Unmute your microphone to continue speaking'
                  : 'Mute your microphone so background noise cannot interrupt the reply'}
              >
                {voice.micResuming ? 'Unmuting…' : voice.micPaused ? 'Unmute mic' : 'Mute mic'}
              </button>
              <button
                type="button"
                className="btn btn--ghost"
                onClick={voice.stopReply}
                disabled={voice.phase !== 'thinking' && voice.phase !== 'speaking'}
                title="Stop this reply and keep voice enabled"
              >
                Stop reply
              </button>
              <button
                type="button"
                className="btn btn--ghost"
                onClick={voice.replayLast}
                disabled={!voice.canReplay || voice.pendingTranscript !== null}
                title="Speak the last completed reply again without asking the model again"
              >
                Replay reply
              </button>
            </div>
          )}
        </div>
        {voice.pendingTranscript !== null && (
          <div className="voice__review" role="status">
            <p>
              This request reached the {voice.limitSeconds}-second recording limit.
              Nothing has been sent. Review the captured text below.
            </p>
            <div className="voice__controls">
              <button type="button" className="btn" onClick={voice.sendCaptured}
                disabled={!voice.pendingTranscript.trim()}>
                Send captured text
              </button>
              <button type="button" className="btn btn--ghost" onClick={voice.discardCaptured}>
                Discard and resume
              </button>
            </div>
          </div>
        )}
        <div className="composer__row">
          <textarea
            ref={inputRef}
            className="composer__input"
            value={composerText}
            aria-label={voice.active ? 'Live voice transcription' : 'Message'}
            readOnly={voice.active}
            rows={1}
            placeholder={
              readOnly
                ? 'Mock data is read-only'
                : voice.active
                  ? voice.micPaused
                    ? 'Microphone is muted'
                    : voice.phase === 'waiting'
                    ? `Say “${voice.wakePhrase}” to begin…`
                    : 'Your words appear here as you speak…'
                  : 'Say something worth remembering…'
            }
            disabled={!voice.active && (busy || readOnly || loading)}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                submit()
              }
            }}
          />
          {!voice.active && <button
            type="button"
            className="btn"
            onClick={submit}
            disabled={busy || readOnly || loading || voice.active || !draft.trim()}
          >
            {busy
              ? workspace?.phase === 'synthesizing'
                ? 'Answering…'
                : researching
                  ? 'Working…'
                  : 'Thinking…'
              : 'Send'}
          </button>}
        </div>
        <div className="composer__hint">
          {readOnly
            ? 'Mock data is read-only · click any reply to inspect its turn'
            : voice.active
              ? voice.pendingTranscript !== null
                ? 'Choose Send captured text or Discard and resume'
                : voice.micPaused
                  ? 'Unmute mic to speak again · replies continue while muted'
                  : 'Say “stop talking”, “repeat that”, “pause microphone”, or “stop listening”'
              : 'Enter sends · Shift+Enter for a new line · click any reply to inspect its turn'}
        </div>
      </div>
    </div>
  )
}

function TierPip({ code, n, tier }: { code: string; n: number; tier: string }) {
  return (
    <span className="tiermark" data-tier={tier} title={`${code} tier delivered ${n}`}>
      <span className="tiermark__code">{code}</span>
      {n}
    </span>
  )
}
