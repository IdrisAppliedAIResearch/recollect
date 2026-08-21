/**
 * The shell: chat on the left, inspector on the right, one resizable split.
 *
 * All turn state lives here because both panes are views of the same thing.
 * The inspector shows whichever turn is *selected*, which defaults to the
 * one in flight but becomes any past turn the moment you click its reply -
 * inspecting history is the normal case, not a debug detour.
 */
import { Fragment, useCallback, useEffect, useRef, useState } from 'react'

import { liveSource } from './api/live.ts'
import { createMockSource } from './api/mock.ts'
import { Chat } from './components/Chat.tsx'
import { Inspector } from './components/Inspector.tsx'
import { chars, int, ms, pct } from './lib/format.ts'
import type { ChatEvent, DataSource, HealthResponse, SessionInfo } from './types/api.ts'
import type { TurnTrace } from './types/trace.ts'

/** One subagent step as streamed mid-turn. Never persisted. */
export interface WorkspaceStep {
  index: number
  tool: string
  args: Record<string, unknown>
  observation: string
  ms: number
}

/**
 * The ephemeral subagent's live arc, while its turn is running and
 * just after. React state only: reload the page and it is gone, while the
 * chat and the trace's one-line summary remain.
 */
export interface Workspace {
  run_id: string
  task: string
  effort: 'focused' | 'deep'
  steps: WorkspaceStep[]
  phase: 'researching' | 'synthesizing' | 'complete' | 'failed'
  sources: string[]
  researchNote: string | null
  failure: string | null
}

export interface Exchange {
  id: string
  user: string
  assistant: string
  reasoning: string
  trace: TurnTrace | null
  streaming: boolean
  error: string | null
  workspace: Workspace | null
}

const MIN_CHAT = 200
const MIN_INSPECTOR = 240
const DETAILS_KEY = 'recollect.details'

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
  const [detailsOpen, setDetailsOpen] = useState(
    () => localStorage.getItem(DETAILS_KEY) !== '0',
  )

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
              workspace: null,
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
          workspace: null,
        },
      ])
      setSelectedId(localId)

      const patch = (id: string, change: Partial<Exchange>) =>
        setExchanges((current) =>
          current.map((item) => (item.id === id ? { ...item, ...change } : item)),
        )

      // The in-flight exchange's id, flipped from the local id to the real
      // turn id when retrieval lands. Track it here rather than re-deriving
      // it per item inside the updater: an item with a loaded trace matches
      // its own id, so the derivation would append every token to every
      // historical bubble and destroy the chat history.
      let activeId = localId

      try {
        await source.chat(session.session_id, message, (event: ChatEvent) => {
          switch (event.type) {
            case 'retrieval':
              // Retrieval lands before the model says a word. Adopt the real
              // turn id now so the inspector is live for the whole generation.
              activeId = event.trace.turn_id
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
                  item.id === activeId
                    ? { ...item, assistant: item.assistant + event.text }
                    : item,
                ),
              )
              break
            case 'reasoning':
              setExchanges((current) =>
                current.map((item) =>
                  item.id === activeId
                    ? { ...item, reasoning: item.reasoning + event.text }
                    : item,
                ),
              )
              break
            case 'subagent_start':
              // The main model paused to delegate. Open the workspace on the
              // in-flight exchange; nothing here leaves React state.
              patch(activeId, {
                workspace: {
                  run_id: event.run_id,
                  task: event.task,
                  effort: event.effort,
                  steps: [],
                  phase: 'researching',
                  sources: [],
                  researchNote: null,
                  failure: null,
                },
              })
              break
            case 'subagent_step':
              setExchanges((current) =>
                current.map((item) =>
                  item.id === activeId && item.workspace
                    ? {
                        ...item,
                        workspace: {
                          ...item.workspace,
                          steps: [...item.workspace.steps, event.step],
                        },
                      }
                    : item,
                ),
              )
              break
            case 'subagent_done':
              setExchanges((current) =>
                current.map((item) =>
                  item.id === activeId && item.workspace
                    ? {
                        ...item,
                        workspace: {
                          ...item.workspace,
                          phase: 'synthesizing',
                          sources: event.sources,
                          researchNote: event.error ?? null,
                        },
                      }
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
                        workspace: item.workspace
                          ? {
                              ...item.workspace,
                              phase: event.generation.response_text.trim()
                                ? 'complete'
                                : 'failed',
                              failure: event.generation.response_text.trim()
                                ? null
                                : (event.generation.error ??
                                  item.workspace.researchNote ??
                                  'The subagent returned no answer.'),
                            }
                          : null,
                      }
                    : item,
                ),
              )
              // The trace on the live turn was snapshotted when retrieval ran,
              // which is before any subagent; merging totals into it could
              // never gain that. The server writes the complete trace before
              // it sends `done` (and the mock pushes its turn first), so pull
              // the persisted one back in whole.
              void (async () => {
                try {
                  const trace = await source.getTurn(event.turn_id)
                  setExchanges((current) =>
                    current.map((item) =>
                      item.id === event.turn_id ? { ...item, trace } : item,
                    ),
                  )
                } catch (error) {
                  setBanner((error as Error).message)
                }
              })()
              break
            case 'error':
              setBanner(event.message)
              break
          }
        })
      } catch (error) {
        patch(activeId, { error: (error as Error).message })
        setBanner((error as Error).message)
      } finally {
        setBusy(false)
        setExchanges((current) =>
          current.map((item) => {
            if (!item.streaming) return item
            const answered = Boolean(item.assistant.trim())
            return {
              ...item,
              streaming: false,
              workspace: item.workspace
                ? {
                    ...item.workspace,
                    phase: answered ? 'complete' : 'failed',
                    failure: answered
                      ? null
                      : (item.error ??
                        item.workspace.researchNote ??
                        'The subagent returned no answer.'),
                  }
                : null,
            }
          }),
        )
        setSession((current) =>
          current ? { ...current, turn_count: current.turn_count + 1 } : current,
        )
      }
    },
    [session, busy, source],
  )

  // -- details pane ------------------------------------------------------

  const toggleDetails = useCallback(() => {
    setDetailsOpen((open) => !open)
  }, [])

  // Persist outside the updater: updater functions may re-run, so a write in
  // there could fire twice; the effect runs once per committed value, never.
  useEffect(() => {
    localStorage.setItem(DETAILS_KEY, detailsOpen ? '1' : '0')
  }, [detailsOpen])

  // -- splitter ---------------------------------------------------------

  const dragging = useRef(false)
  useEffect(() => {
    const move = (event: MouseEvent) => {
      if (!dragging.current) return
      // The inspector is a flex child with no other lower bound, so give it
      // its own floor as well as the chat's: a fixed chat max would leave the
      // details pane stuck at a fraction of wide windows.
      const maxChat = Math.max(MIN_CHAT, window.innerWidth - MIN_INSPECTOR)
      setChatWidth(Math.min(maxChat, Math.max(MIN_CHAT, event.clientX)))
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
          <div className="topbar__group topbar__group--chip">
            <ConfigChip health={health} />
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
            className={detailsOpen ? 'ctl ctl--on' : 'ctl'}
            aria-pressed={detailsOpen}
            onClick={toggleDetails}
            title="Show or hide the memory details pane"
          >
            Details
          </button>
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

      <div className={'workspace' + (detailsOpen ? '' : ' workspace--collapsed')}>
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
          <Inspector trace={selected?.trace ?? null} source={source} />
        </div>
      </div>
    </div>
  )
}

function embedderOk(health: HealthResponse): boolean | null {
  const value = health.embedder?.sentinel_matches_research
  return typeof value === 'boolean' ? value : null
}

function generatorOk(health: HealthResponse): boolean | null {
  const value = health.generator?.reachable
  return typeof value === 'boolean' ? value : null
}

/**
 * The active configuration and embedder health, in one quiet chip.
 *
 * Every segment renders only if its field is present, so a health response
 * that carries fewer fields (mock data, an older server) degrades to showing
 * whatever it does carry instead of blanks.
 */
function ConfigChip({ health }: { health: HealthResponse }) {
  const cfg = health.episodic_config ?? {}

  const configParts: string[] = []
  const n = numOf(cfg, 'recency_window_n')
  if (n !== null) configParts.push(`N=${int(n)}`)
  const threshold = numOf(cfg, 'k_threshold')
  if (threshold !== null) configParts.push(`K≥${threshold}`)
  const lambda = numOf(cfg, 'selector_lambda')
  const clusters = numOf(cfg, 'selector_cluster_count')
  if (lambda !== null && clusters !== null) {
    configParts.push(`${strOf(cfg, 'selector') ?? 'A3'} λ=${lambda} k=${int(clusters)}`)
  }
  const budget = numOf(health, 'budget_chars')
  if (budget !== null) configParts.push(`budget ${chars(budget)}`)

  const embedderParts: string[] = []
  const calls = numOf(health.embedder, 'calls')
  if (calls !== null) embedderParts.push(`${int(calls)} call${calls === 1 ? '' : 's'}`)
  const hitRatio = numOf(health.embedder, 'hit_ratio')
  if (hitRatio !== null) embedderParts.push(`${pct(hitRatio, 0)} hit`)
  const coldLoad = numOf(health.embedder, 'cold_load_ms')
  if (coldLoad !== null) embedderParts.push(`${ms(coldLoad)} cold`)
  const nCtx = numOf(health.embedder, 'n_ctx')
  if (nCtx !== null) embedderParts.push(`ctx ${int(nCtx)}`)
  const dimension = numOf(health.embedder, 'embedding_dimension')
  if (dimension !== null) embedderParts.push(`d${int(dimension)}`)

  // Live reports the model under configured_model; the mock under model. A
  // deployment may name it by file path, so show only the last segment.
  const model =
    (strOf(health.generator, 'configured_model') ?? strOf(health.generator, 'model'))?.split(
      /[\\/]/,
    )
      .pop() ?? null

  const segments: string[] = [...configParts]
  if (embedderParts.length) segments.push(embedderParts.join(' '))
  if (model) segments.push(model)
  if (segments.length === 0) return null

  const title = [
    health.episodic_config
      ? `config ${keyValues(health.episodic_config, [
          'recency_window_n',
          'k_threshold',
          'selector',
          'selector_lambda',
          'selector_cluster_count',
        ])}`
      : null,
    health.budget_chars !== undefined ? `budget_chars=${health.budget_chars}` : null,
    keyValues(health.embedder, [
      'calls',
      'hit_ratio',
      'cold_load_ms',
      'n_ctx',
      'embedding_dimension',
    ])
      ? `embedder ${keyValues(health.embedder, [
          'calls',
          'hit_ratio',
          'cold_load_ms',
          'n_ctx',
          'embedding_dimension',
        ])}`
      : null,
    model ? `generator ${model}` : null,
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <span className="chip mono" title={title}>
      {segments.map((segment, index) => (
        <Fragment key={`${index}-${segment}`}>
          {index > 0 && (
            <span className="chip__sep" aria-hidden>
              ·
            </span>
          )}
          {/* The wide tail segments (embedder stats, model) are the ones that
              ellipsize when the bar runs out of room, so the action buttons
              to the right never get pushed off-screen. Full text stays in
              the chip's title. */}
          <span
            className={
              index >= segments.length - 2 ? 'chip__seg chip__seg--shrink' : 'chip__seg'
            }
          >
            {segment}
          </span>
        </Fragment>
      ))}
    </span>
  )
}

function numOf(record: object | undefined, key: string): number | null {
  const value = (record as Record<string, unknown> | undefined)?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function strOf(record: object | undefined, key: string): string | null {
  const value = (record as Record<string, unknown> | undefined)?.[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}

function keyValues(record: object | undefined, keys: string[]): string | null {
  if (!record) return null
  const map = record as Record<string, unknown>
  const parts = keys
    .filter((key) => map[key] !== undefined)
    .map((key) => `${key}=${String(map[key])}`)
  return parts.length ? parts.join(' ') : null
}
