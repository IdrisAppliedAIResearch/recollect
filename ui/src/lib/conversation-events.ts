import type { Exchange } from '../App.tsx'
import type { TaskNotification } from '../types/tasks.ts'

export type ConversationEvent =
  | { kind: 'turn'; id: string; at: number; exchange: Exchange }
  | { kind: 'notification'; id: string; at: number; notification: TaskNotification }

export function conversationEvents(
  exchanges: Exchange[], notifications: TaskNotification[],
): ConversationEvent[] {
  const events: ConversationEvent[] = exchanges.map((exchange) => ({
    kind: 'turn', id: exchange.id,
    at: Date.parse(exchange.startedAt ?? exchange.trace?.started_at ?? '') || 0,
    exchange,
  }))
  const seen = new Set<string>()
  for (const notification of notifications) {
    if (seen.has(notification.notification_id)) continue
    seen.add(notification.notification_id)
    events.push({ kind: 'notification', id: `notification-${notification.notification_id}`,
      at: Date.parse(notification.created_at) || 0, notification })
  }
  return events.sort((a, b) => a.at - b.at)
}
