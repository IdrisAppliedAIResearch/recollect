/**
 * The CC80 ranking — for every episode in the store, not just the ones that
 * arrived.
 *
 * The undelivered rows are the point. An episode that ranked third by fused
 * score and never reached the model is the single most informative thing this
 * screen can show, and it exists nowhere in the delivered set.
 */
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'

import { chars, cosine, gain, int, truncate } from '../../lib/format.ts'
import { proposingTiers } from '../../lib/derive.ts'
import type { DataSource, EpisodeBody } from '../../types/api.ts'
import { TIER_CODES } from '../../types/trace.ts'
import type { CandidateTrace, TierName, TurnTrace } from '../../types/trace.ts'

type SortKey = 'cc80' | 'dense' | 'bm25' | 'turn_number' | 'render_chars'
type Filter = 'all' | 'delivered' | 'dropped' | 'proposed'

export function ScoresTab({
  trace,
  source,
}: {
  trace: TurnTrace
  source: DataSource
}) {
  const [sort, setSort] = useState<SortKey>('cc80')
  const [descending, setDescending] = useState(true)
  const [filter, setFilter] = useState<Filter>('all')
  const [tierFilter, setTierFilter] = useState<TierName | 'any'>('any')
  const [search, setSearch] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [episodeBody, setEpisodeBody] = useState<EpisodeBody | null>(null)
  const [bodyLoading, setBodyLoading] = useState(false)
  const [bodyError, setBodyError] = useState<string | null>(null)
  const bodyRequests = useRef(0)

  // A new turn is a new table: any fetched body belongs to the old one.
  useEffect(() => {
    setExpandedId(null)
    setEpisodeBody(null)
    setBodyError(null)
    setBodyLoading(false)
  }, [trace])

  const toggleBody = (episodeId: string) => {
    if (expandedId === episodeId) {
      setExpandedId(null)
      setEpisodeBody(null)
      setBodyError(null)
      return
    }
    setExpandedId(episodeId)
    setEpisodeBody(null)
    setBodyError(null)
    setBodyLoading(true)
    const request = ++bodyRequests.current
    void source
      .getEpisode(trace.session_id, episodeId)
      .then((episode) => {
        if (bodyRequests.current !== request) return
        setEpisodeBody(episode)
      })
      .catch((error) => {
        if (bodyRequests.current !== request) return
        setBodyError((error as Error).message)
      })
      .finally(() => {
        if (bodyRequests.current === request) setBodyLoading(false)
      })
  }

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
        {(['any', 'recency', 'semantic', 'aspect'] as const).map((option) => (
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
              <Header label="dense" active={sort === 'dense'} desc={descending} onClick={() => toggle('dense')} />
              <Header label="bm25" active={sort === 'bm25'} desc={descending} onClick={() => toggle('bm25')} />
              <Header label="cc80" active={sort === 'cc80'} desc={descending} onClick={() => toggle('cc80')} />
              <Header label="turn" active={sort === 'turn_number'} desc={descending} onClick={() => toggle('turn_number')} />
              <th>paths</th>
              <Header label="chars" active={sort === 'render_chars'} desc={descending} onClick={() => toggle('render_chars')} />
              <th>outcome</th>
              <th>episode</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((candidate) => (
              <Fragment key={candidate.id}>
                <tr data-delivered={candidate.delivered}>
                  <td className="n faint">{candidate.cc80_rank}</td>
                  <td className="n" title="raw cosine, before per-query min-max scaling">
                    {cosine(candidate.dense_cosine)}
                  </td>
                  <td className="n" title="raw Robertson BM25, before per-query min-max scaling">
                    {gain(candidate.bm25_score)}
                  </td>
                  <td>
                    <span
                      className="relbar"
                      title={`fused ${candidate.cc80_score.toFixed(4)} = ${trace.cc80_detail.dense_weight} dense + ${1 - trace.cc80_detail.dense_weight} bm25, each min-max normalized`}
                    >
                      <span
                        className="relbar__fill"
                        style={{ width: `${Math.max(0, Math.min(1, candidate.cc80_score)) * 100}%` }}
                      />
                    </span>
                    {cosine(candidate.cc80_score)}
                  </td>
                  <td className="n">{candidate.turn_number}</td>
                  <td>
                    {proposingTiers(candidate).map((tier) => (
                      <span key={tier} className="tiermark" data-tier={tier}>
                        <span className="tiermark__code">{TIER_CODES[tier]}</span>
                      </span>
                    ))}
                    {proposingTiers(candidate).length === 0 && <span className="faint">—</span>}
                  </td>
                  <td className="n">{chars(candidate.render_chars)}</td>
                  <td>
                    {candidate.delivered ? (
                      <span className="tiermark" data-tier={candidate.delivered_via ?? 'none'}>
                        <span className="tiermark__code">
                          {candidate.delivered_via ? TIER_CODES[candidate.delivered_via] : '—'}
                        </span>
                      </span>
                    ) : (
                      <span className="badge badge--warn" title={candidate.drop_reason ?? ''}>
                        dropped
                      </span>
                    )}
                  </td>
                  <td className="wide">
                    <span
                      className="epbody-cell"
                      title={
                        expandedId === candidate.id
                          ? ''
                          : `${candidate.preview}\n\n${candidate.assistant_preview}`
                      }
                    >
                      <button
                        type="button"
                        className="epbody-toggle"
                        aria-expanded={expandedId === candidate.id}
                        title="Show the full episode text"
                        onClick={() => toggleBody(candidate.id)}
                      >
                        {expandedId === candidate.id ? '▾' : '▸'}
                      </button>
                      {truncate(candidate.preview, 100)}
                    </span>
                  </td>
                </tr>
                {expandedId === candidate.id && (
                  <tr className="epbody-row">
                    <td colSpan={10}>
                      <div className="epbody">
                        {bodyLoading && <div className="epbody__state faint">fetching full body…</div>}
                        {bodyError && <div className="callout callout--bad">{bodyError}</div>}
                        {episodeBody && (
                          <>
                            <div className="epbody__block">
                              <span className="epbody__role">
                                user · turn {int(episodeBody.turn_number)}
                              </span>
                              <div className="epbody__text">{episodeBody.user_message}</div>
                            </div>
                            <div className="epbody__block">
                              <span className="epbody__role">assistant</span>
                              <div className="epbody__text">{episodeBody.assistant_message}</div>
                            </div>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
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
    case 'cc80':
      return candidate.cc80_score
    case 'dense':
      return candidate.dense_cosine
    case 'bm25':
      return candidate.bm25_score
    case 'turn_number':
      return candidate.turn_number
    case 'render_chars':
      return candidate.render_chars
  }
}
