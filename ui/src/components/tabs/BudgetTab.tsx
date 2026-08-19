/**
 * Where the characters went, and every admission decision in order.
 *
 * The research this deploys concluded that budget was never the binding
 * constraint — selection and packing order were. This tab is how you check
 * that on your own data: if the bar is far from full while episodes were
 * dropped, the budget was not what stopped them.
 */
import { chars, pct } from '../../lib/format.ts'
import { orderedTiers } from '../../lib/derive.ts'
import type { TurnTrace } from '../../types/trace.ts'

export function BudgetTab({ trace }: { trace: TurnTrace }) {
  const budget = Math.max(trace.report.budget_chars, 1)
  const tiers = orderedTiers(trace)
  const slack = trace.report.chars_available
  const droppedWithSlack = trace.report.episodes_dropped > 0 && slack > 0

  return (
    <div className="stack">
      <div className="statgrid">
        <Stat label="delivered" value={chars(trace.report.chars_delivered)} sub="characters" />
        <Stat label="budget" value={chars(trace.report.budget_chars)} sub="hard ceiling" />
        <Stat
          label="unused"
          value={chars(slack)}
          sub={pct(slack / budget)}
          tone={droppedWithSlack ? 'warn' : undefined}
        />
        <Stat
          label="wanted"
          value={chars(trace.report.chars_wanted)}
          sub={
            trace.report.shortfall_chars > 0
              ? `${chars(trace.report.shortfall_chars)} short`
              : 'all of it fit'
          }
        />
      </div>

      {droppedWithSlack && (
        <div className="callout callout--warn">
          <span className="callout__mark">!</span>
          <div className="callout__body">
            <div className="callout__title">
              {trace.report.episodes_dropped} episodes dropped with {chars(slack)}{' '}
              characters still free
            </div>
            The budget was not the binding constraint on this turn. Each dropped
            episode was individually too large for the space left at the moment it
            was considered — the packer skips and continues rather than stopping, so
            cheaper later candidates got in while these did not.
          </div>
        </div>
      )}

      <section className="card">
        <div className="card__head">
          <span className="card__title">Spend by path</span>
          <span className="card__note mono">{pct(trace.report.chars_delivered / budget)} used</span>
        </div>
        <div className="card__body">
          <div className="stackbar">
            {tiers.map((tier) => (
              <div
                key={tier.name}
                className="stackbar__seg"
                data-tier={tier.name}
                style={{ width: `${(tier.chars_delivered / budget) * 100}%` }}
                title={`${tier.label}: ${chars(tier.chars_delivered)}`}
              />
            ))}
            <div
              className="stackbar__seg"
              data-tier="none"
              style={{ width: `${(slack / budget) * 100}%` }}
              title={`unused: ${chars(slack)}`}
            />
          </div>

          {tiers.map((tier) => (
            <div key={tier.name} className="gauge">
              <span className="gauge__label">
                <span className="swatch" data-tier={tier.name} /> {tier.label}
              </span>
              <span className="gauge__track">
                {/* Measured against proposed, matching the "X of Y proposed"
                    reading beside it; the budget view is the stackbar above. */}
                <span
                  className="gauge__fill"
                  data-tier={tier.name}
                  style={{
                    width: `${(tier.chars_delivered / Math.max(tier.chars_proposed, 1)) * 100}%`,
                  }}
                />
              </span>
              <span className="gauge__value mono">
                {chars(tier.chars_delivered)} of {chars(tier.chars_proposed)} proposed
              </span>
            </div>
          ))}
        </div>
      </section>

      <section className="card">
        <div className="card__head">
          <span className="card__title">Admission walk</span>
          <span className="card__note mono">
            {trace.packing.decisions.length} decisions · floor{' '}
            {chars(trace.packing.empty_payload_chars)} chars
          </span>
        </div>
        <div className="card__body card__body--flush">
          <div className="tablewrap">
            <table className="grid">
              <thead>
                <tr>
                  <th>#</th>
                  <th>path</th>
                  <th>cost</th>
                  <th>running</th>
                  <th>result</th>
                  <th>reason</th>
                </tr>
              </thead>
              <tbody>
                {trace.packing.decisions.map((decision) => (
                  <tr key={decision.order} data-delivered={decision.admitted}>
                    <td className="n faint">{decision.order}</td>
                    <td>
                      <span className="tiermark" data-tier={decision.tier}>
                        <span className="tiermark__code">{decision.tier}</span>
                      </span>
                    </td>
                    <td className="n">{chars(decision.cost_chars)}</td>
                    <td className="n">{chars(decision.payload_chars_after)}</td>
                    <td>
                      {decision.admitted ? (
                        <span className="badge badge--ok">admitted</span>
                      ) : (
                        <span className="badge badge--warn">skipped</span>
                      )}
                    </td>
                    <td className="wide faint">{decision.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  )
}

function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string
  sub?: string
  tone?: 'ok' | 'warn' | 'bad'
}) {
  return (
    <div className={tone ? `stat stat--${tone}` : 'stat'}>
      <span className="stat__label">{label}</span>
      <span className="stat__value">{value}</span>
      {sub && <span className="stat__sub">{sub}</span>}
    </div>
  )
}
