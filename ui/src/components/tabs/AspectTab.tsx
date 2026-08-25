/**
 * The protected ASPECT half: what the facet saturation chose, step by step,
 * and why it stopped.
 *
 * Each step shows the arithmetic that drove it: the episode's CC80 score, the
 * unsaturated facet mass it would add on top of what the initial half already
 * covered, and the per-character ratio it won that round on. Steps whose
 * episode never reached the model are marked — that gap between "chosen" and
 * "delivered" is the architecture's known fault, and it is only visible by
 * putting the two side by side.
 */
import { chars, cosine, gain, int, ms } from '../../lib/format.ts'
import { candidateIndex, selectedButDropped } from '../../lib/derive.ts'
import type { TurnTrace } from '../../types/trace.ts'

export function AspectTab({ trace }: { trace: TurnTrace }) {
  const detail = trace.aspect_detail
  const half = trace.packing.half_chars

  if (detail.mode === 'off') {
    return (
      <div className="empty">
        <div className="empty__title">ASPECT is off in this store's config</div>
        <p>
          Every long-term admission went through the single CC80 walk against the
          full allowance. There is no facet saturation to inspect.
        </p>
      </div>
    )
  }

  if (detail.mode === 'fallback') {
    return (
      <div className="stack">
        <div className="callout callout--warn">
          <span className="callout__mark">!</span>
          <div className="callout__body">
            <div className="callout__title">
              The protected split fell back to a single walk
            </div>
            The initial half admitted nothing, so one CC80 walk owned the whole
            allowance. There is no spread and no facet arithmetic on this turn.
          </div>
        </div>
        <div className="statgrid">
          <Stat
            label="long-term admitted"
            value={int(detail.initial_ids.length)}
            sub="single CC80 walk over the full allowance"
          />
          <Stat label="spread" value="0" sub="never ran" />
          <Stat label="returned" value={int(detail.returned_ids.length)} sub="slack back" />
        </div>
      </div>
    )
  }

  const steps = detail.steps
  const dropped = selectedButDropped(trace)
  const index = candidateIndex(trace)

  return (
    <div className="stack">
      <div className="statgrid">
        <Stat label="initial" value={int(detail.initial_ids.length)} sub="CC80 half" />
        <Stat label="spread" value={int(detail.spread_ids.length)} sub="facet half" />
        <Stat label="returned" value={int(detail.returned_ids.length)} sub="slacked back" />
        <Stat
          label="solo spend"
          value={chars(detail.solo_chars)}
          sub={`of ${chars(half)} half`}
        />
        <Stat
          label="stopped"
          value={detail.stopping_reason ?? '—'}
          sub={
            detail.facet_latency_ms === null
              ? 'facet parse not recorded'
              : `facets parsed in ${ms(detail.facet_latency_ms)}`
          }
          tone={
            detail.stopping_reason === 'no_complete_candidate_fits' ? 'warn' : undefined
          }
        />
      </div>

      {dropped.size > 0 && (
        <div className="callout callout--warn">
          <span className="callout__mark">!</span>
          <div className="callout__body">
            <div className="callout__title">
              The spread chose {dropped.size} episode{dropped.size === 1 ? '' : 's'}
              that never arrived
            </div>
            It saturates facets as though its half were its own, and is only then
            packed against it. The set below is not the set that reached the model.
          </div>
        </div>
      )}

      {steps.length === 0 ? (
        <div className="empty">
          <div className="empty__title">The spread made no step</div>
          <p>
            The initial admissions already accounted for every facet with positive
            gain, or nothing else fit — the stopping reason above says which.
          </p>
        </div>
      ) : (
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
                    <span className="badge">{step.covered_total} facets</span>
                    {wasDropped && (
                      <span className="badge badge--warn">never delivered</span>
                    )}
                  </div>

                  <div className="step__preview">
                    {candidate?.preview ?? step.candidate_id}
                  </div>

                  <div className="step__math mono">
                    score {cosine(step.score)} · unsaturated {gain(step.marginal)} ={' '}
                    {gain(step.ratio)}/char
                  </div>
                </div>

                <div className="step__right mono">
                  <div>+{chars(step.additive_chars)}</div>
                  <div className="faint">{chars(step.cumulative_chars)} of half</div>
                </div>
              </div>
            )
          })}
        </div>
      )}
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
