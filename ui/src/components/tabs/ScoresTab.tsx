/**
 * What the embeddings scored — for every episode in the store, not just the
 * ones that arrived.
 *
 * The undelivered rows are the point. An episode that ranked third by cosine
 * and never reached the model is the single most informative thing this
 * screen can show, and it exists nowhere in the delivered set.
 */
import { useMemo, useState } from 'react'

import { chars, cosine, truncate } from '../../lib/format.ts'
import { proposingTiers } from '../../lib/derive.ts'
import type { CandidateTrace, TierName, TurnTrace } from '../../types/trace.ts'

type SortKey = 'relevance' | 'turn_number' | 'render_chars' | 'cluster'
type Filter = 'all' | 'delivered' | 'dropped' | 'proposed'

export function ScoresTab({ trace }: { trace: TurnTrace }) {
  const [sort, setSort] = useState<SortKey>('relevance')
  const [descending, setDescending] = useState(true)
  const [filter, setFilter] = useState<Filter>('all')
  const [tierFilter, setTierFilter] = useState<TierName | 'any'>('any')
  const [search, setSearch] = useState('')

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase()
    let list = trace.candidates.filter((candidate) => {
      if (filter === 'delivered' && !candidate.delivered) return false
      if (filter === 'dropped' && candidate.delivered) return false
      if (filter === 'proposed' && proposingTiers(candidate).length === 0) return false
      if (tierFilter !== 'any' && !proposingTiers(candidate).includes(tierFilter)) {
        return false
      }
      if (needle) {
        const haystack = `${candidate.preview} ${candidate.assistant_preview}`.toLowerCase()
        if (!haystack.includes(needle)) return false
      }
      return true
    })

    list = [...list].sort((a, b) => {
      const left = value(a, sort)
      const right = value(b, sort)
      const order = left === right ? a.turn_number - b.turn_number : left - right
      return descending ? -order : order
    })
    return list
  }, [trace.candidates, sort, descending, filter, tierFilter, search])

  const toggle = (key: SortKey) => {
    if (key === sort) setDescending((value) => !value)
    else {
      setSort(key)
      setDescending(true)
    }
  }

  return (
    <div className="stack">
      <div className="filterbar">
        <input
          className="search"
          placeholder="Search episode text…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        {(['all', 'delivered', 'dropped', 'proposed'] as Filter[]).map((option) => (
          <button
            key={option}
            type="button"
            className={filter === option ? 'ctl ctl--on' : 'ctl'}
            onClick={() => setFilter(option)}
          >
            {option}
          </button>
        ))}
        <span className="faint">·</span>
        {(['any', 'recency', 'similarity', 'coverage'] as const).map((option) => (
          <button
            key={option}
            type="button"
            className={tierFilter === option ? 'ctl ctl--on' : 'ctl'}
            onClick={() => setTierFilter(option)}
          >
            {option}
          </button>
        ))}
        <span className="filterbar__count mono">
          {rows.length} of {trace.candidates.length}
        </span>
      </div>

      <div className="tablewrap">
        <table className="grid">
          <thead>
            <tr>
              <th>#</th>
              <Header label="cosine" active={sort === 'relevance'} desc={descending} onClick={() => toggle('relevance')} />
              <th>relevance</th>
              <Header label="turn" active={sort === 'turn_number'} desc={descending} onClick={() => toggle('turn_number')} />
              <Header label="cluster" active={sort === 'cluster'} desc={descending} onClick={() => toggle('cluster')} />
              <th>paths</th>
              <Header label="chars" active={sort === 'render_chars'} desc={descending} onClick={() => toggle('render_chars')} />
              <th>outcome</th>
              <th>episode</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((candidate) => (
              <tr key={candidate.id} data-delivered={candidate.delivered}>
                <td className="n faint">{candidate.relevance_rank}</td>
                <td className="n">{cosine(candidate.relevance)}</td>
                <td>
                  <span className="relbar">
                    <span
                      className="relbar__fill"
                      style={{ width: `${Math.max(0, Math.min(1, candidate.relevance)) * 100}%` }}
                    />
                  </span>
                </td>
                <td className="n">{candidate.turn_number}</td>
                <td className="n">{candidate.cluster ?? '—'}</td>
                <td>
                  {proposingTiers(candidate).map((tier) => (
                    <span key={tier} className="tiermark" data-tier={tier}>
                      <span className="tiermark__code">{tier.slice(0, 3)}</span>
                    </span>
                  ))}
                  {proposingTiers(candidate).length === 0 && <span className="faint">—</span>}
                </td>
                <td className="n">{chars(candidate.render_chars)}</td>
                <td>
                  {candidate.delivered ? (
                    <span className="tiermark" data-tier={candidate.delivered_via ?? 'none'}>
                      <span className="tiermark__code">
                        {candidate.delivered_via ?? 'delivered'}
                      </span>
                    </span>
                  ) : (
                    <span className="badge badge--warn" title={candidate.drop_reason ?? ''}>
                      dropped
                    </span>
                  )}
                </td>
                <td
                  className="wide"
                  title={`${candidate.preview}\n\n${candidate.assistant_preview}`}
                >
                  {truncate(candidate.preview, 110)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="empty__title">Nothing matches those filters</div>}
      </div>
    </div>
  )
}

function Header({
  label,
  active,
  desc,
  onClick,
}: {
  label: string
  active: boolean
  desc: boolean
  onClick: () => void
}) {
  return (
    <th
      onClick={onClick}
      className={active ? 'sortable is-sorted' : 'sortable'}
      role="button"
    >
      {label}
      {active && <span className="sortcue">{desc ? '▾' : '▴'}</span>}
    </th>
  )
}

function value(candidate: CandidateTrace, key: SortKey): number {
  switch (key) {
    case 'relevance':
      return candidate.relevance
    case 'turn_number':
      return candidate.turn_number
    case 'render_chars':
      return candidate.render_chars
    case 'cluster':
      return candidate.cluster ?? -1
  }
}
