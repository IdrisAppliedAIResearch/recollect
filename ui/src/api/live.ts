/**
 * The live data source. Same-origin `/api`; Vite proxies it to 127.0.0.1:8080
 * in dev.
 */
import type {
  ChatEvent,
  DataSource,
  EpisodeBody,
  HealthResponse,
  SessionInfo,
} from '../types/api.ts'
import type { GenerationTrace, TurnSummary, TurnTrace } from '../types/trace.ts'

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  })
  if (!response.ok) {
    const body = await response.text().catch(() => '')
    throw new Error(`${init?.method ?? 'GET'} ${path} -> ${response.status} ${body.slice(0, 240)}`)
  }
  return (await response.json()) as T
}

/**
 * Minimal SSE reader over fetch. EventSource is not used because the chat
 * endpoint is a POST with a JSON body.
 */
async function readEventStream(
  response: Response,
  onEvent: (name: string, data: string) => void,
): Promise<void> {
  const body = response.body
  if (!body) throw new Error('the chat response carried no body')
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      const raw = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      boundary = buffer.indexOf('\n\n')

      let name = 'message'
      const dataLines: string[] = []
      for (const line of raw.split('\n')) {
        if (line.startsWith('event:')) name = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
      }
      if (dataLines.length) onEvent(name, dataLines.join('\n'))
    }
  }
}

export const liveSource: DataSource = {
  kind: 'live',

  health: () => json<HealthResponse>('/api/health'),

  listSessions: () => json<SessionInfo[]>('/api/sessions'),

  createSession: (title?: string) =>
    json<SessionInfo>('/api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(title ? { title } : {}),
    }),

  listTurns: (sessionId) =>
    json<TurnSummary[]>(`/api/sessions/${encodeURIComponent(sessionId)}/turns`),

  getTurn: (turnId) => json<TurnTrace>(`/api/turns/${encodeURIComponent(turnId)}`),

  getEpisode: (sessionId, episodeId) =>
    json<EpisodeBody>(
      `/api/sessions/${encodeURIComponent(sessionId)}/episodes/${encodeURIComponent(episodeId)}`,
    ),

  async chat(sessionId, message, onEvent, signal, inputMode = 'text') {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({
        session_id: sessionId,
        message,
        ...(inputMode === 'voice' ? { input_mode: inputMode } : {}),
      }),
      signal,
    })
    if (!response.ok) {
      const body = await response.text().catch(() => '')
      throw new Error(`POST /api/chat -> ${response.status} ${body.slice(0, 240)}`)
    }

    await readEventStream(response, (name, data) => {
      let parsed: unknown
      try {
        parsed = JSON.parse(data)
      } catch {
        return
      }
      const event = toChatEvent(name, parsed)
      if (event) onEvent(event)
    })
  },
}

function toChatEvent(name: string, payload: unknown): ChatEvent | null {
  const record = payload as Record<string, unknown>
  switch (name) {
    case 'retrieval':
      return { type: 'retrieval', trace: payload as TurnTrace }
    case 'token':
      return { type: 'token', text: String(record.text ?? '') }
    case 'reasoning':
      return { type: 'reasoning', text: String(record.text ?? '') }
    case 'subagent_start':
      return {
        type: 'subagent_start',
        run_id: String(record.run_id ?? ''),
        task: String(record.task ?? ''),
        effort: record.effort === 'deep' ? 'deep' : 'focused',
      }
    case 'subagent_step': {
      const step = record.step as Record<string, unknown> | undefined
      if (!step) return null
      return {
        type: 'subagent_step',
        run_id: String(record.run_id ?? ''),
        step: {
          index: typeof step.index === 'number' ? step.index : 0,
          tool: String(step.tool ?? ''),
          args: (step.args as Record<string, unknown>) ?? {},
          observation: String(step.observation ?? ''),
          ms: typeof step.ms === 'number' ? step.ms : 0,
        },
      }
    }
    case 'subagent_done':
      return {
        type: 'subagent_done',
        run_id: String(record.run_id ?? ''),
        ok: Boolean(record.ok),
        steps: typeof record.steps === 'number' ? record.steps : 0,
        sources: Array.isArray(record.sources) ? record.sources.map(String) : [],
        returned_chars: typeof record.returned_chars === 'number' ? record.returned_chars : 0,
        error: typeof record.error === 'string' ? record.error : undefined,
      }
    case 'done':
      return {
        type: 'done',
        turn_id: String(record.turn_id ?? ''),
        generation: record.generation as GenerationTrace,
        total_ms: typeof record.total_ms === 'number' ? record.total_ms : null,
        committed: typeof record.committed === 'boolean' ? record.committed : undefined,
      }
    case 'error':
      return { type: 'error', message: String(record.message ?? 'unknown error') }
    default:
      return null
  }
}
