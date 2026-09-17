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

/** Everything the assistant says between two user messages, in order. */
export interface ConversationBundle {
  id: string
  /** The user message that opened this bundle; null before the first one. */
  user: string | null
  exchange: Exchange | null
  updates: TaskNotification[]
}

/**
 * The assistant's reply and every later worker update share one bundle, which
 * only a new user message closes. A task direction sent from its card counts
 * as a user message.
 */
export function conversationBundles(
  exchanges: Exchange[], notifications: TaskNotification[],
): ConversationBundle[] {
  const bundles: ConversationBundle[] = []
  for (const event of conversationEvents(exchanges, notifications)) {
    if (event.kind === 'turn') {
      bundles.push({ id: event.id, user: event.exchange.user, exchange: event.exchange,
        updates: [] })
      continue
    }
    const update = event.notification
    if (update.user_message || bundles.length === 0) {
      bundles.push({ id: event.id, user: update.user_message || null, exchange: null,
        updates: [update] })
    } else {
      bundles[bundles.length - 1].updates.push(update)
    }
  }
  return bundles
}
