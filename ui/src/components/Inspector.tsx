/**
 * The glass. Seven views over one turn, and a headline strip that never moves.
 *
 * Tab order is the order the mechanism runs, not the order of importance:
 * Pipeline (what happened) → Context (what the model saw) → Scores, Clusters,
 * Selector (how it was chosen) → Budget (what it cost) → Verify (whether any
 * of this can be believed).
 */
import { useState } from 'react'

import { Strip } from './Strip.tsx'
import { BudgetTab } from './tabs/BudgetTab.tsx'
import { ClustersTab } from './tabs/ClustersTab.tsx'
import { ContextTab } from './tabs/ContextTab.tsx'
import { PipelineTab } from './tabs/PipelineTab.tsx'
import { ScoresTab } from './tabs/ScoresTab.tsx'
import { SelectorTab } from './tabs/SelectorTab.tsx'
import { VerifyTab } from './tabs/VerifyTab.tsx'
import { isTrustworthy, starvedTiers } from '../lib/derive.ts'
import type { DataSource } from '../types/api.ts'
import type { TurnTrace } from '../types/trace.ts'

type TabId =
  | 'pipeline'
  | 'context'
  | 'scores'
  | 'clusters'
  | 'selector'
  | 'budget'
  | 'verify'

const TABS: { id: TabId; label: string }[] = [
  { id: 'pipeline', label: 'Pipeline' },
  { id: 'context', label: 'Context' },
  { id: 'scores', label: 'Scores' },
  { id: 'clusters', label: 'Clusters' },
  { id: 'selector', label: 'Selector' },
  { id: 'budget', label: 'Budget' },
  { id: 'verify', label: 'Verify' },
]

export function Inspector({ trace, source }: { trace: TurnTrace | null; source: DataSource }) {
  const [tab, setTab] = useState<TabId>('pipeline')

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
    if (id === 'pipeline' && starvedTiers(trace).length > 0) return '!'
    if (id === 'budget' && trace.report.truncated) return '!'
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
        {tab === 'pipeline' && <PipelineTab trace={trace} />}
        {tab === 'context' && <ContextTab trace={trace} />}
        {tab === 'scores' && <ScoresTab trace={trace} source={source} />}
        {tab === 'clusters' && <ClustersTab trace={trace} />}
        {tab === 'selector' && <SelectorTab trace={trace} />}
        {tab === 'budget' && <BudgetTab trace={trace} />}
        {tab === 'verify' && <VerifyTab trace={trace} />}
      </div>
    </div>
  )
}
