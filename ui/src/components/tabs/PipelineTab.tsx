/**
 * What happened this turn, as the mechanism runs it.
 *
 * The one thing this view exists to make unmissable: a path that proposed
 * episodes and delivered none. That is invisible in every "retrieved N
 * episodes" summary, and it is the failure the underlying research spent
 * eleven studies finding.
 */
import { chars, int, ms, truncate } from '../../lib/format.ts'
import {
  charsAvailable,
  hasContributed,
  isFullyOverlapped,
  isStarved,
  orderedTiers,
  shortfallChars,
} from '../../lib/derive.ts'
import type { SubagentTrace, TierTrace, TurnTrace } from '../../types/trace.ts'

export function PipelineTab({ trace }: { trace: TurnTrace }) {
  const tiers = orderedTiers(trace)
  const starved = tiers.filter(isStarved)
  const aspect = trace.aspect_detail
  const report = trace.report
  const unused = charsAvailable(report)
  const shortfall = shortfallChars(report)
  const recencyChars = tiers.find((t) => t.name === 'recency')?.chars_delivered ?? 0

  return (
    <div className="stack">
      {starved.length > 0 && (
        <div className="callout callout--warn">
          <span className="callout__mark">!</span>
          <div className="callout__body">
            <div className="callout__title">
              {starved.map((t) => t.label).join(' and ')} proposed episodes that never
              arrived
            </div>
            The allowance was spent by an earlier path before these were considered.
            They were not outscored — they were never weighed.
          </div>
        </div>
      )}

      {aspect.mode === 'fallback' && (
        <div className="callout callout--warn">
          <span className="callout__mark">!</span>
          <div className="callout__body">
            <div className="callout__title">The protected 50/50 split did not run</div>
            The initial half admitted nothing, so one CC80 walk owned the whole
            allowance. There is no ASPECT block on this turn — the semantic claim is
            the entire long-term selection.
          </div>
        </div>
      )}

      <section className="card">
        <div className="card__head">
          <span className="card__title">Store</span>
          <span className="card__note mono">
            {int(trace.store.episode_count)} episodes · pool{' '}
            {int(trace.report.pool_size)}
          </span>
        </div>
        <div className="card__body">
          <div className="pipe-title">
            RECENT renders additively, outside the long-term allowance. SEMANTIC and
            ASPECT divide the allowance half-and-half.
          </div>
          <div className="two-col">
            {tiers.map((tier) => (
              <TierCard key={tier.name} tier={tier} />
            ))}
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card__head">
          <span className="card__title">Packing</span>
          <span className="card__note mono">{trace.packing.policy}</span>
        </div>
        <div className="card__body">
          <p className="section-note">
            The long-term allowance is walked in phases — initial CC80, then the
            ASPECT spread, then a slack return; when the split did not run, a single
            full walk owns it. Each candidate is charged the exact characters it
            serializes to. One that does not fit is skipped, and the walk continues
            to the next.
          </p>
          <div className="statgrid">
            <Stat label="admitted" value={int(report.episodes_delivered)} />
            <Stat label="dropped" value={int(report.episodes_dropped)} />
            <Stat
              label="phases"
              value={trace.packing.phases.join(' → ')}
              sub="in this order"
            />
            <Stat
              label="decisions"
              value={int(trace.packing.decisions.length)}
              sub="attempts made"
            />
            <Stat
              label="duplicates"
              value={int(trace.packing.duplicate_ids.length)}
              sub="charged once"
            />
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card__head">
          <span className="card__title">Context window</span>
          <span className="card__note mono">
            {chars(trace.context_block.chars)} total · {chars(unused)} unused allowance
          </span>
        </div>
        <div className="card__body">
          <div className="statgrid">
            <Stat
              label="recent block"
              value={int(trace.context_block.recent_episode_count)}
              sub={`${chars(recencyChars)} additive`}
            />
            <Stat
              label="retrieved block"
              value={int(trace.context_block.retrieved_episode_count)}
              sub="long-term episodes"
            />
            <Stat
              label="unused allowance"
              value={chars(unused)}
              sub={`of ${chars(report.budget_chars)}`}
            />
            {shortfall > 0 && (
              <Stat
                label="shortfall"
                value={chars(shortfall)}
                sub="more was wanted"
                tone="warn"
              />
            )}
          </div>
        </div>
      </section>

      {trace.subagent && (
        <ResearchCard
          sub={trace.subagent}
          answered={Boolean(trace.generation?.response_text.trim())}
        />
      )}
    </div>
  )
}

/**
 * The ephemeral subagent's one-line record, if this turn ran one.
 * The full arc (steps, observations) lives for the turn in the chat pane and
 * nowhere else; this card is what the persisted trace keeps.
 */
function ResearchCard({ sub, answered }: { sub: SubagentTrace; answered: boolean }) {
  return (
    <section className="card">
      <div className="card__head">
        <span className="card__title">Subagent</span>
        <span className="card__note mono">
          {sub.effort} · {sub.backend}/{sub.isolation} · {int(sub.steps)} steps ·{' '}
          {int(sub.sources.length)} sources · {ms(sub.total_ms)}
        </span>
      </div>
      <div className="card__body">
        <div className="research__task mono" title={sub.task}>
          {truncate(sub.task, 140)}
        </div>

        {!answered && (
          <div className="callout callout--bad">
            <span className="callout__mark">!</span>
            <div className="callout__body">
              <div className="callout__title">The subagent returned no answer</div>
              {sub.error ?? 'The subagent run ended without usable output.'}
            </div>
          </div>
        )}

        <div className="statgrid">
          <Stat label="steps" value={int(sub.steps)} sub="tool calls made" />
          <Stat label="sources" value={int(sub.sources.length)} sub="collected" />
          <Stat label="returned" value={chars(sub.returned_chars)} sub="evidence characters" />
          <Stat label="tools" value={sub.tools_used.join(', ') || '—'} />
          <Stat
            label="context"
            value={sub.fresh_context ? 'fresh' : 'reused'}
            sub={sub.server_reused ? 'warm server' : 'new server'}
          />
        </div>

        {sub.sources.length > 0 && (
          <details className="research__sources">
            <summary>
              {int(sub.sources.length)} source{sub.sources.length === 1 ? '' : 's'}
            </summary>
            <ul>
              {sub.sources.map((url) => (
                <li key={url} className="mono">
                  {url}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </section>
  )
}

function TierCard({ tier }: { tier: TierTrace }) {
  const starved = isStarved(tier)
  const overlapped = isFullyOverlapped(tier)
  const contributed = hasContributed(tier)

  const state = starved
    ? 'starved'
    : overlapped
      ? 'all already claimed'
      : contributed
        ? 'contributed'
        : 'proposed nothing'

  return (
    <div
      className={'tier-card' + (starved ? ' tier-card--dashed' : '')}
      data-tier={tier.name}
    >
      <div className="tier-card__head">
        <span className="tiermark" data-tier={tier.name}>
          <span className="tiermark__code">{tier.label}</span>
        </span>
        <span className={starved ? 'badge badge--warn' : 'badge'}>{state}</span>
      </div>

      <div className="tier-card__nums">
        <div>
          <span className="tier-card__num">{int(tier.proposed_ids.length)}</span>
          <span className="strip__label">proposed</span>
        </div>
        <div>
          <span className="tier-card__num">{int(tier.delivered_ids.length)}</span>
          <span className="strip__label">delivered</span>
        </div>
        <div>
          <span className="tier-card__num">{int(tier.overlapped_ids.length)}</span>
          <span className="strip__label">overlapped</span>
        </div>
        <div>
          <span className="tier-card__num">{int(tier.skipped_ids.length)}</span>
          <span className="strip__label">skipped</span>
        </div>
      </div>

      <div className="tier-card__desc">{tier.description}</div>
      <div className="card__note mono">
        {chars(tier.chars_delivered)} of {chars(tier.chars_proposed)} chars landed
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
