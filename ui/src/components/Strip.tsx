/**
 * The always-visible headline. Whatever tab is open, these numbers stay put,
 * because they are the ones you want to notice changing between turns.
 */
import { chars, ms } from '../lib/format.ts'
import { headline } from '../lib/derive.ts'
import { EMPTY_PAYLOAD_CHARS } from '../lib/render.ts'
import type { TurnTrace } from '../types/trace.ts'

export function Strip({ trace }: { trace: TurnTrace }) {
  const head = headline(trace)
  const addedNothing = head.relevanceOnly === 0 && head.eligible > 0
  const reason =
    trace.store.episode_count === 0
      ? 'no episodes yet'
      : head.recency >= head.eligible
        ? 'the window holds the whole store'
        : 'nothing cleared the threshold'

  return (
    <div className="strip mono">
      <Cell
        label="delivered"
        value={head.charsDelivered <= EMPTY_PAYLOAD_CHARS ? '—' : chars(head.charsDelivered)}
        sub={`${head.delivered} of ${head.eligible} episodes`}
      />
      <Cell
        label="from memory"
        value={addedNothing ? '—' : String(head.relevanceOnly)}
        sub={addedNothing ? reason : `beyond the last ${head.window}`}
      />
      <div className="strip__cell">
        <span className="strip__label">admitted by</span>
        <span className="strip__tiers">
          <Pip code="N" n={head.recency} tier="continuity" />
          <Pip code="K" n={head.relevanceOnly} tier="relevance" />
          <Pip code="B" n={head.overlap} tier="both" />
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
          {addedNothing && (
            <span
              className="badge badge--warn"
              title={
                'Every episode that cleared the threshold was already inside ' +
                `the last ${head.window} exchanges. Long-term memory added ` +
                'nothing the continuity window would not have supplied.'
              }
            >
              no long-term gain
            </span>
          )}
          {trace.ceiling.engaged && (
            <span
              className="badge badge--warn"
              title={
                `The deployment ceiling withheld ${trace.ceiling.withheld_ids.length} ` +
                'episode(s) that cleared the threshold, to fit the model context. ' +
                'A hardware limit of this deployment, not the mechanism.'
              }
            >
              ceiling −{trace.ceiling.withheld_ids.length}
            </span>
          )}
          {head.continuityShare === 1 && head.delivered > 0 && (
            <span
              className="badge badge--warn"
              title="The block is exactly the continuity window; retrieval contributed no episode of its own."
            >
              continuity only
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
