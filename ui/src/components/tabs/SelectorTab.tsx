/**
 * The coverage selector's greedy walk, with the arithmetic that drove it.
 *
 * Each step shows the gain it was chosen on: relevance, plus the novelty
 * bonus if it entered a cluster nothing had reached yet. Steps whose episode
 * was later dropped by packing are marked — that gap between "selected" and
 * "delivered" is the architecture's known fault, and it is only visible by
 * putting the two side by side.
 */
import { chars, cosine, gain, int } from '../../lib/format.ts'
import { candidateIndex, selectedButDropped } from '../../lib/derive.ts'
import type { TurnTrace } from '../../types/trace.ts'

export function SelectorTab({ trace }: { trace: TurnTrace }) {
  const steps = trace.selector_steps
  const dropped = selectedButDropped(trace)
  const index = candidateIndex(trace)

  if (steps.length === 0) {
    return (
      <div className="empty">
        <div className="empty__title">The coverage selector did not run</div>
        <p>
          Either the candidate pool was empty, or the budget was below the cost of
          the empty block tags — the point past which no payload can be expressed
          at all.
        </p>
      </div>
    )
  }

  const novel = steps.filter((step) => step.entered_new_cluster).length

  return (
    <div className="stack">
      <div className="rowflex">
        <Stat label="steps" value={int(steps.length)} sub="episodes chosen" />
        <Stat label="entered new cluster" value={int(novel)} sub="novelty bonus paid" />
        <Stat
          label="selected then dropped"
          value={int(dropped.size)}
          sub="never reached the model"
          tone={dropped.size > 0 ? 'warn' : undefined}
        />
        <Stat
          label="final cost"
          value={chars(steps[steps.length - 1]?.cumulative_chars ?? 0)}
          sub="if packed alone"
        />
      </div>

      {dropped.size > 0 && (
        <div className="callout callout--warn">
          <span className="callout__mark">!</span>
          <div className="callout__body">
            <div className="callout__title">
              The selector chose {dropped.size} episode{dropped.size === 1 ? '' : 's'} that
              never arrived
            </div>
            It optimises as though the whole budget were its own, and is then packed
            last. The set below is not the set it would have chosen had it known what
            it would actually be given.
          </div>
        </div>
      )}

      <div className="walk">
        {steps.map((step) => {
          const candidate = index.get(step.candidate_id)
          const wasDropped = dropped.has(step.candidate_id)
          return (
            <div key={step.step} className="step" data-dropped={wasDropped}>
              <div className="step__n mono">{step.step}</div>

              <div className="step__main">
                <div className="step__top mono">
                  <span>turn {step.source_turn}</span>
                  {step.cluster !== null && (
                    <span className="badge">cluster {step.cluster}</span>
                  )}
                  {step.entered_new_cluster && (
                    <span className="badge badge--accent">new cluster</span>
                  )}
                  {wasDropped && <span className="badge badge--warn">dropped by packing</span>}
                </div>

                <div className="step__preview">
                  {candidate?.preview ?? step.candidate_id}
                </div>

                <div className="step__math mono">
                  relevance {cosine(step.relevance)}
                  {step.entered_new_cluster ? ' + novelty' : ''} = gain{' '}
                  {gain(step.objective_gain)}
                  {step.scaled_gain !== step.objective_gain && (
                    <> · scaled {gain(step.scaled_gain)}</>
                  )}
                </div>
              </div>

              <div className="step__right mono">
                <div>+{chars(step.additive_chars)}</div>
                <div className="faint">{chars(step.cumulative_chars)} total</div>
              </div>
            </div>
          )
        })}
      </div>
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
