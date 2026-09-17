export interface ImplementationActivity {
  at: string
  kind: string
  text: string
}

export interface ImplementationStatus {
  enabled: boolean
  state?: 'idle' | 'awaiting_approval' | 'running' | 'finished' | 'failed' |
    'stopped' | 'declined'
  session_id?: string
  task_id?: string
  attempt?: number
  missing_capability?: string | null
  milestone?: string
  updated_at?: string
  activity?: ImplementationActivity[]
}

export async function implementationStatus(signal?: AbortSignal): Promise<ImplementationStatus> {
  const response = await fetch('/api/selfmod/status', {
    headers: { Accept: 'application/json' }, cache: 'no-store', signal,
  })
  if (!response.ok) throw new Error(`Implementation status failed (${response.status}).`)
  return response.json() as Promise<ImplementationStatus>
}

export async function stopImplementation(signal?: AbortSignal): Promise<boolean> {
  const response = await fetch('/api/selfmod/stop', { method: 'POST', signal })
  if (!response.ok) throw new Error(`Stop failed (${response.status}).`)
  return (await response.json()).stopped === true
}
