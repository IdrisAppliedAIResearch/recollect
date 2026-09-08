/**
 * The conversation. Each assistant message is a handle onto its own turn -
 * click it and the inspector rewinds to what memory did for that reply.
 */
import { useEffect, useRef, useState } from 'react'

import { chars, clock, int, stamp } from '../lib/format.ts'
import type { SessionInfo } from '../types/api.ts'
import type { Exchange } from '../App.tsx'
import { Markdown } from './Markdown.tsx'
import { Workspace } from './Workspace.tsx'

interface Props {
  exchanges: Exchange[]
  selectedId: string | null
  onSelect: (id: string) => void
  onSend: (message: string) => void
  busy: boolean
  session: SessionInfo | null
  readOnly: boolean
}

export function Chat({
  exchanges,
  selectedId,
  onSelect,
  onSend,
  busy,
  session,
  readOnly,
}: Props) {
  const [draft, setDraft] = useState('')
  const logRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const log = logRef.current
    if (log) log.scrollTop = log.scrollHeight
  }, [exchanges])

  // Going `disabled` while busy throws focus away, and it never comes back on
  // its own — so re-focus as soon as the composer is usable again, which is
  // what "ready to keep typing" means after each reply.
  useEffect(() => {
    if (!busy && !readOnly) inputRef.current?.focus()
  }, [busy, readOnly])

  const submit = () => {
    const message = draft.trim()
    if (!message || busy || readOnly) return
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
        {exchanges.length === 0 && (
          <div className="empty">
            <div className="empty__title">Nothing remembered yet</div>
            <p>
              Say something. Every reply is built from a context window
              reassembled from scratch, and the panel on the right shows
              exactly how it was chosen.
            </p>
          </div>
        )}

        {exchanges.map((exchange) => (
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
        ))}
      </div>

      {workspace && <Workspace workspace={workspace} />}

      <div className="composer">
        <div className="composer__row">
          <textarea
            ref={inputRef}
            className="composer__input"
            value={draft}
            rows={1}
            placeholder={
              readOnly ? 'Mock data is read-only' : 'Say something worth remembering…'
            }
            disabled={busy || readOnly}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                submit()
              }
            }}
          />
          <button
            type="button"
            className="btn"
            onClick={submit}
            disabled={busy || readOnly || !draft.trim()}
          >
            {busy
              ? workspace?.phase === 'synthesizing'
                ? 'Answering…'
                : researching
                  ? 'Working…'
                  : 'Thinking…'
              : 'Send'}
          </button>
        </div>
        <div className="composer__hint">
          {readOnly
            ? 'Mock data is read-only · click any reply to inspect its turn'
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
