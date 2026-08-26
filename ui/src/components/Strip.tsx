/**
 * The always-visible headline. Whatever tab is open, these numbers stay put,
 * because they are the ones you want to notice changing between turns.
 */
import { chars, ms, pct } from '../lib/format.ts'
import { headline } from '../lib/derive.ts'
import type { TurnTrace } from '../types/trace.ts'

export function Strip({ trace }: { trace: TurnTrace }) {
  const head = headline(trace)

  return (
    <div className="strip mono">
      <Cell
        label="delivered"
        value={chars(head.charsDelivered)}
        sub={`${head.delivered} episodes · ${head.dropped} dropped`}
      />
      <Cell
        label="long-term"
        value={`${chars(head.retrievalCharsDelivered)} / ${chars(head.budget)}`}
        sub={pct(head.utilization)}
      />
      <div className="strip__cell">
        <span className="strip__label">tiers</span>
        <span className="strip__tiers">
          <Pip code="N" n={head.recency} tier="recency" />
          <Pip code="K" n={head.semantic} tier="semantic" />
          <Pip code="A" n={head.aspect} tier="aspect" />
        </span>
      </div>
      <Cell label="store" value={String(trace.store.episode_count)} sub="episodes" />
      <Cell label="pool" value={String(trace.report.pool_size)} sub="considered" />
      <Cell
        label="latency"
        value={ms(head.totalMs)}
        sub={`embed ${ms(trace.query.embed_latency_ms)}`}
      />

      <div className="strip__cell strip__cell--grow">
        <span className="strip__label">state</span>
        <span className="strip__value">
          {head.trustworthy ? (
            <span className="badge badge--ok">verified</span>
          ) : (
            <span className="badge badge--bad">NOT VERIFIED</span>
          )}
          {head.aspectMode === 'fallback' && (
            <span
              className="badge badge--warn"
              title="The initial half admitted nothing; one CC80 walk owned the whole allowance."
            >
              ASPECT fallback
            </span>
          )}
          {head.starved.map((name) => (
            <span key={name} className="badge badge--warn" title="Proposed episodes the budget never admitted">
              {name} starved
            </span>
          ))}
          {trace.report.truncated && (
            <span
              className="badge badge--warn"
              title={
                `The paths wanted ${chars(trace.report.chars_wanted)} characters; only ` +
                `${chars(trace.report.chars_delivered)} fit the budget.`
              }
            >
              truncated
            </span>
          )}
        </span>
      </div>
    </div>
  )
}

function Cell({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="strip__cell">
      <span className="strip__label">{label}</span>
      <span className="strip__value">{value}</span>
      {sub && <span className="strip__sub">{sub}</span>}
    </div>
  )
}

function Pip({ code, n, tier }: { code: string; n: number; tier: string }) {
  return (
    <span className="tiermark" data-tier={tier}>
      <span className="tiermark__code">{code}</span>
      {n}
    </span>
  )
}
