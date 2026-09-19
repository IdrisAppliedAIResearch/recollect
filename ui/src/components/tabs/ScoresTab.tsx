/**
 * Every episode in the store against the threshold, not just the ones that
 * arrived.
 *
 * The undelivered rows are the point. Nothing competes for room on this
 * path, so an episode that did not arrive has exactly one reason — it
 * scored below the threshold — and the only remaining question is by how
 * much. That is what `margin` is for, and it exists nowhere in the
 * delivered set.
 */
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'

import { chars, cosine, int, truncate } from '../../lib/format.ts'
import { qualifyingPaths } from '../../lib/derive.ts'
import type { DataSource, EpisodeBody } from '../../types/api.ts'
import { PATH_CODES } from '../../types/trace.ts'
import type { CandidateTrace, SelectionPath, TurnTrace } from '../../types/trace.ts'

type SortKey = 'cosine' | 'margin' | 'turn_number' | 'render_chars'
type Filter = 'all' | 'delivered' | 'missed' | 'withheld'

export function ScoresTab({
  trace,
  source,
}: {
  trace: TurnTrace
  source: DataSource
}) {
  const [sort, setSort] = useState<SortKey>('cosine')
  const [descending, setDescending] = useState(true)
  const [filter, setFilter] = useState<Filter>('all')
  const [pathFilter, setPathFilter] = useState<SelectionPath | 'any'>('any')
  const [search, setSearch] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [episodeBody, setEpisodeBody] = useState<EpisodeBody | null>(null)
  const [bodyLoading, setBodyLoading] = useState(false)
  const [bodyError, setBodyError] = useState<string | null>(null)
  const bodyRequests = useRef(0)
  const threshold = trace.timeline.relevance_threshold

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
      // "missed" means the mechanism turned it away, which withheld is not.
      if (filter === 'missed' && (candidate.delivered || candidate.withheld)) {
        return false
      }
      if (filter === 'withheld' && !candidate.withheld) return false
      if (pathFilter !== 'any' && !qualifyingPaths(candidate).includes(pathFilter)) {
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
  }, [trace.candidates, sort, descending, filter, pathFilter, search])

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
        {(['all', 'delivered', 'missed', 'withheld'] as Filter[]).map((option) => (
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
        {(['any', 'relevance', 'continuity'] as const).map((option) => (
          <button
            key={option}
            type="button"
            className={pathFilter === option ? 'ctl ctl--on' : 'ctl'}
            onClick={() => setPathFilter(option)}
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
              <Header label="cosine" active={sort === 'cosine'} desc={descending} onClick={() => toggle('cosine')} />
              <Header label="margin" active={sort === 'margin'} desc={descending} onClick={() => toggle('margin')} />
              <Header label="turn" active={sort === 'turn_number'} desc={descending} onClick={() => toggle('turn_number')} />
              <th>admitted by</th>
              <Header label="chars" active={sort === 'render_chars'} desc={descending} onClick={() => toggle('render_chars')} />
              <th>outcome</th>
              <th>episode</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((candidate) => (
              <Fragment key={candidate.id}>
                <tr data-delivered={candidate.delivered}>
                  <td>
                    <span
                      className="relbar"
                      title={
                        `cosine ${candidate.cosine.toFixed(4)} against a ` +
                        `threshold of ${threshold.toFixed(2)}; measured, not scaled`
                      }
                    >
                      <span
                        className="relbar__fill"
                        style={{ width: `${clamp(candidate.cosine) * 100}%` }}
                      />
                      {/* Where the threshold falls on the same scale, so a
                          near miss is visible without reading the number. */}
                      <span
                        className="relbar__cap"
                        style={{ left: `${clamp(threshold) * 100}%` }}
                      />
                    </span>
                    {cosine(candidate.cosine)}
                  </td>
                  <td
                    className={candidate.margin >= 0 ? 'n' : 'n faint'}
                    title="cosine minus the threshold; negative means it missed"
                  >
                    {candidate.margin >= 0 ? '+' : ''}
                    {candidate.margin.toFixed(4)}
                  </td>
                  <td className="n">{candidate.turn_number}</td>
                  <td>
                    {qualifyingPaths(candidate).map((path) => (
                      <span key={path} className="tiermark" data-tier={path}>
                        <span className="tiermark__code">{PATH_CODES[path]}</span>
                      </span>
                    ))}
                    {qualifyingPaths(candidate).length === 0 && (
                      <span className="faint">—</span>
                    )}
                  </td>
                  <td className="n">{chars(candidate.render_chars)}</td>
                  <td>
                    {candidate.delivered ? (
                      <span className="tiermark" data-tier={candidate.delivered_via ?? 'none'}>
                        <span className="tiermark__code">
                          {candidate.delivered_via
                            ? PATH_CODES[candidate.delivered_via]
                            : '—'}
                        </span>
                      </span>
                    ) : candidate.withheld ? (
                      <span
                        className="badge badge--bad"
                        title={
                          'Cleared the threshold by ' +
                          `${candidate.margin.toFixed(4)} and would have been ` +
                          'delivered, but the deployment ceiling excluded it ' +
                          'before the library saw it. Not a relevance decision.'
                        }
                      >
                        withheld
                      </span>
                    ) : (
                      <span
                        className="badge badge--warn"
                        title={
                          candidate.eligible
                            ? `Missed the threshold by ${Math.abs(candidate.margin).toFixed(4)}.`
                            : 'Outside the caller’s source-order horizon.'
                        }
                      >
                        {candidate.eligible ? 'below threshold' : 'out of horizon'}
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
                    <td colSpan={7}>
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

/** Cosine runs [-1, 1]; the bar is a [0, 1] box, so negatives read as empty. */
function clamp(value: number): number {
  return Math.max(0, Math.min(1, value))
}

function value(candidate: CandidateTrace, key: SortKey): number {
  switch (key) {
    case 'cosine':
      return candidate.cosine
    case 'margin':
      return candidate.margin
    case 'turn_number':
      return candidate.turn_number
    case 'render_chars':
      return candidate.render_chars
  }
}
