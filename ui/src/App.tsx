/**
 * The shell: chat on the left, inspector on the right, one resizable split.
 *
 * All turn state lives here because both panes are views of the same thing.
 * The inspector shows whichever turn is *selected*, which defaults to the
 * one in flight but becomes any past turn the moment you click its reply -
 * inspecting history is the normal case, not a debug detour.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { liveSource } from './api/live.ts'
import { createMockSource } from './api/mock.ts'
import { Chat } from './components/Chat.tsx'
import { Inspector } from './components/Inspector.tsx'
import type { ChatEvent, DataSource, HealthResponse, SessionInfo } from './types/api.ts'
import type { TurnTrace } from './types/trace.ts'

export interface Exchange {
  id: string
  user: string
  assistant: string
  reasoning: string
  trace: TurnTrace | null
  streaming: boolean
  error: string | null
}

const MIN_CHAT = 320
const MAX_CHAT = 900

export function App() {
  const [useMock, setUseMock] = useState(false)
  const [source, setSource] = useState<DataSource>(() => liveSource)
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [exchanges, setExchanges] = useState<Exchange[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [banner, setBanner] = useState<string | null>(null)
  const [chatWidth, setChatWidth] = useState(480)

  // -- data source ------------------------------------------------------

  useEffect(() => {
    const next = useMock ? createMockSource() : liveSource
    setSource(next)
    setExchanges([])
    setSelectedId(null)
    setBanner(null)

    let cancelled = false
    void (async () => {
      try {
        const [info, sessions] = await Promise.all([next.health(), next.listSessions()])
        if (cancelled) return
        setHealth(info)
        const existing = sessions[0]
        const active = existing ?? (await next.createSession())
        if (cancelled) return
        setSession(active)

        // Backfill the session's history. Summaries are cheap; the full
        // trace for a turn is fetched only when that turn is selected,
        // because a long session holds a lot of them.
        const turns = await next.listTurns(active.session_id)
        if (cancelled) return
        if (turns.length) {
          setExchanges(
            turns.map((summary) => ({
              id: summary.turn_id,
              user: summary.query_preview,
              assistant: summary.response_preview,
              reasoning: '',
              trace: null,
              streaming: false,
              error: null,
            })),
          )
          setSelectedId(turns[turns.length - 1]?.turn_id ?? null)
        }
      } catch (error) {
        if (!cancelled) {
          setHealth(null)
          setBanner(
            `${(error as Error).message}. Is the server running? ` +
              `Try "recollect serve", or switch on Mock data to explore the UI.`,
          )
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [useMock])

  // -- sending ----------------------------------------------------------

  const send = useCallback(
    async (message: string) => {
      if (!session || busy) return
      const localId = `pending-${Date.now()}`
      setBusy(true)
      setBanner(null)
      setExchanges((current) => [
        ...current,
        {
          id: localId,
          user: message,
          assistant: '',
          reasoning: '',
          trace: null,
          streaming: true,
          error: null,
        },
      ])
      setSelectedId(localId)

      const patch = (id: string, change: Partial<Exchange>) =>
        setExchanges((current) =>
          current.map((item) => (item.id === id ? { ...item, ...change } : item)),
        )

      try {
        await source.chat(session.session_id, message, (event: ChatEvent) => {
          switch (event.type) {
            case 'retrieval':
              // Retrieval lands before the model says a word. Adopt the real
              // turn id now so the inspector is live for the whole generation.
              setExchanges((current) =>
                current.map((item) =>
                  item.id === localId
                    ? { ...item, id: event.trace.turn_id, trace: event.trace }
                    : item,
                ),
              )
              setSelectedId((current) => (current === localId ? event.trace.turn_id : current))
              break
            case 'token':
              setExchanges((current) =>
                current.map((item) =>
                  item.id === event_id(item, localId)
                    ? { ...item, assistant: item.assistant + event.text }
                    : item,
                ),
              )
              break
            case 'reasoning':
              setExchanges((current) =>
                current.map((item) =>
                  item.id === event_id(item, localId)
                    ? { ...item, reasoning: item.reasoning + event.text }
                    : item,
                ),
              )
              break
            case 'done':
              setExchanges((current) =>
                current.map((item) =>
                  item.id === event.turn_id
                    ? {
                        ...item,
                        streaming: false,
                        trace: item.trace
                          ? { ...item.trace, generation: event.generation }
                          : item.trace,
                      }
                    : item,
                ),
              )
              break
            case 'error':
              setBanner(event.message)
              break
          }
        })
      } catch (error) {
        patch(localId, { error: (error as Error).message })
        setBanner((error as Error).message)
      } finally {
        setBusy(false)
        setExchanges((current) =>
          current.map((item) => (item.streaming ? { ...item, streaming: false } : item)),
        )
        setSession((current) =>
          current ? { ...current, turn_count: current.turn_count + 1 } : current,
        )
      }
    },
    [session, busy, source],
  )

  // -- splitter ---------------------------------------------------------

  const dragging = useRef(false)
  useEffect(() => {
    const move = (event: MouseEvent) => {
      if (!dragging.current) return
      setChatWidth(Math.min(MAX_CHAT, Math.max(MIN_CHAT, event.clientX)))
    }
    const stop = () => {
      dragging.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', stop)
    return () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', stop)
    }
  }, [])

  // Pull the full trace for whatever is selected, once.
  useEffect(() => {
    if (!selectedId) return
    const target = exchanges.find((item) => item.id === selectedId)
    if (!target || target.trace || target.streaming || selectedId.startsWith('pending-')) {
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const trace = await source.getTurn(selectedId)
        if (cancelled) return
        setExchanges((current) =>
          current.map((item) =>
            item.id === selectedId
              ? {
                  ...item,
                  trace,
                  assistant: item.assistant || (trace.generation?.response_text ?? ''),
                  reasoning: item.reasoning || (trace.generation?.reasoning_text ?? ''),
                }
              : item,
          ),
        )
      } catch (error) {
        if (!cancelled) setBanner((error as Error).message)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [selectedId, exchanges, source])

  const selected = exchanges.find((item) => item.id === selectedId) ?? null

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand__name">Recollect</span>
          <span className="brand__tag">episodic memory, visible</span>
        </div>

        <div className="topbar__spacer" />

        {health && (
          <div className="topbar__group">
            <span className="badge badge--ok">episodic {health.library_version}</span>
            {embedderOk(health) === false && (
              <span className="badge badge--bad">embedder drifted</span>
            )}
            {generatorOk(health) === false && (
              <span className="badge badge--warn">generator unreachable</span>
            )}
          </div>
        )}

        <div className="topbar__group">
          <button
            type="button"
            className={useMock ? 'ctl ctl--on' : 'ctl'}
            onClick={() => setUseMock((value) => !value)}
            title="Explore the whole UI with a realistic generated trace and no server"
          >
            Mock data
          </button>
        </div>
      </header>

      {banner && <div className="errorbar">{banner}</div>}

      <div className="workspace">
        <div className="pane pane--chat" style={{ width: chatWidth }}>
          <Chat
            exchanges={exchanges}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onSend={send}
            busy={busy}
            session={session}
            readOnly={source.kind === 'mock'}
          />
        </div>

        <div
          className="splitter"
          role="separator"
          aria-orientation="vertical"
          onMouseDown={() => {
            dragging.current = true
            document.body.style.cursor = 'col-resize'
            document.body.style.userSelect = 'none'
          }}
        />

        <div className="pane pane--inspector">
          <Inspector trace={selected?.trace ?? null} />
        </div>
      </div>
    </div>
  )
}

/** During a turn the exchange id flips from the local id to the real turn id. */
function event_id(item: Exchange, localId: string): string {
  return item.trace ? item.id : localId
}

function embedderOk(health: HealthResponse): boolean | null {
  const value = health.embedder?.sentinel_matches_research
  return typeof value === 'boolean' ? value : null
}

function generatorOk(health: HealthResponse): boolean | null {
  const value = health.generator?.reachable
  return typeof value === 'boolean' ? value : null
}
