/**
 * The glass. Four views over one turn, and a headline strip that never moves.
 *
 * Tab order is the order the mechanism runs, not the order of importance:
 * Selection (what was admitted, and what nearly was) → Context (what the
 * model saw) → Scores (every cosine against the threshold) → Verify
 * (whether any of this can be believed).
 *
 * The Aspect and Budget tabs were removed with the timeline adoption: there
 * is no facet spread and no allowance to spend, so both would have rendered
 * a permanent column of zeros.
 */
import { useState } from 'react'

import { Strip } from './Strip.tsx'
import { ContextTab } from './tabs/ContextTab.tsx'
import { ScoresTab } from './tabs/ScoresTab.tsx'
import { SelectionTab } from './tabs/SelectionTab.tsx'
import { VerifyTab } from './tabs/VerifyTab.tsx'
import { isTrustworthy } from '../lib/derive.ts'
import type { DataSource } from '../types/api.ts'
import type { TurnTrace } from '../types/trace.ts'

type TabId = 'selection' | 'context' | 'scores' | 'verify'

const TABS: { id: TabId; label: string }[] = [
  { id: 'selection', label: 'Selection' },
  { id: 'context', label: 'Context' },
  { id: 'scores', label: 'Scores' },
  { id: 'verify', label: 'Verify' },
]

export function Inspector({ trace, source }: { trace: TurnTrace | null; source: DataSource }) {
  const [tab, setTab] = useState<TabId>('selection')

  if (!trace) {
    return (
      <div className="inspector">
        <div className="empty">
          <div className="empty__title">No turn selected</div>
          <p>
            Send a message, or click any reply on the left. The moment retrieval
            finishes — before the model has written a word — everything it
            decided appears here.
          </p>
        </div>
      </div>
    )
  }

  const flag = (id: TabId): string | null => {
    if (id === 'verify' && !isTrustworthy(trace.verification)) return '!'
    // Nothing cleared the threshold, so the block is pure continuity: the
    // model saw only the last N exchanges and long-term memory sat this
    // turn out. Not an error, but the thing most worth noticing.
    if (id === 'selection' && trace.timeline.relevance_only_count === 0) {
      return '!'
    }
    return null
  }

  return (
    <div className="inspector">
      <Strip trace={trace} />

      <nav className="tabs" role="tablist">
        {TABS.map((entry) => {
          const mark = flag(entry.id)
          return (
            <button
              key={entry.id}
              type="button"
              role="tab"
              aria-selected={tab === entry.id}
              className={tab === entry.id ? 'tab is-active' : 'tab'}
              onClick={() => setTab(entry.id)}
            >
              {entry.label}
              {mark && <span className="tab__flag">{mark}</span>}
            </button>
          )
        })}
      </nav>

      <div className="tabpanel" role="tabpanel">
        {tab === 'selection' && <SelectionTab trace={trace} />}
        {tab === 'context' && <ContextTab trace={trace} />}
        {tab === 'scores' && <ScoresTab trace={trace} source={source} />}
        {tab === 'verify' && <VerifyTab trace={trace} />}
      </div>
    </div>
  )
}
