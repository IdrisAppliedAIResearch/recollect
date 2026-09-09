export type TaskState = 'queued' | 'running' | 'blocked' | 'cancel-requested' |
  'completed' | 'canceled' | 'interrupted'

export interface TaskArtifact {
  artifact_id: string
  filename: string
  media_type: string
  size_bytes: number
  revision: number
  version: number
  download_path?: string
  download_error?: string
}

export interface ResearchTask {
  task_id: string
  session_id: string
  objective: string
  original_message: string
  parent_task_id?: string | null
  state: TaskState
  revision: number
  accepted_revision: number
  result_revision: number | null
  progress: string
  findings: string[]
  sources: string[]
  result?: string
  error: string | null
  partial: boolean
  quiet: boolean
  artifacts: TaskArtifact[]
}

export interface TaskNotification {
  notification_id: string
  session_id: string
  task_id: string
  text: string
  kind: string
  user_message?: string | null
  created_at: string
  seq: number
  source_refs: string[]
}

export interface TaskSnapshot {
  enabled: boolean
  tasks: ResearchTask[]
  notifications: TaskNotification[]
  cursor: number
}

export interface TaskCommand {
  request_id: string
  operation: 'steer' | 'cancel' | 'continue' | 'quiet'
  text?: string
  quiet?: boolean
  reply_to?: string
}
