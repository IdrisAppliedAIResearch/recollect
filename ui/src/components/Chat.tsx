/**
 * The conversation. Each assistant message is a handle onto its own turn -
 * click it and the inspector rewinds to what memory did for that reply.
 */
import { useEffect, useRef, useState } from 'react'

import { chars, int } from '../lib/format.ts'
import type { SessionInfo } from '../types/api.ts'
import type { Exchange } from '../App.tsx'

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

  useEffect(() => {
    const log = logRef.current
    if (log) log.scrollTop = log.scrollHeight
  }, [exchanges])

  const submit = () => {
    const message = draft.trim()
    if (!message || busy || readOnly) return
    setDraft('')
    onSend(message)
  }

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
          <div key={exchange.id}>
            <div className="msg msg--user">
              <div className="msg__bubble">{exchange.user}</div>
            </div>

            {exchange.reasoning && (
              <div className="reasoning">
                <div className="reasoning__label">thinking</div>
                {exchange.reasoning}
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
                {exchange.assistant || (exchange.streaming ? '…' : '')}
                {exchange.error && <div className="callout callout--bad">{exchange.error}</div>}
              </div>

              {exchange.trace && (
                <div className="msg__meta mono">
                  <span>{int(exchange.trace.report.episodes_delivered)} episodes</span>
                  <span>
                    {chars(exchange.trace.report.chars_delivered)}/
                    {chars(exchange.trace.report.budget_chars)}
                  </span>
                  <span className="msg__flags">
                    <TierPip code="N" n={exchange.trace.report.stm_count} tier="recency" />
                    <TierPip code="K" n={exchange.trace.report.k_count} tier="similarity" />
                    <TierPip
                      code="A3"
                      n={exchange.trace.report.coverage_count}
                      tier="coverage"
                    />
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

      <div className="composer">
        <div className="composer__row">
          <textarea
            className="composer__input"
            value={draft}
            rows={2}
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
            {busy ? 'Thinking…' : 'Send'}
          </button>
        </div>
        <div className="composer__hint">
          Enter sends · Shift+Enter for a new line · click any reply to inspect its turn
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
