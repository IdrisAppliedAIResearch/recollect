/**
 * What happened this turn, as the mechanism runs it.
 *
 * The one thing this view exists to make unmissable: a path that proposed
 * episodes and delivered none. That is invisible in every "retrieved N
 * episodes" summary, and it is the failure the underlying research spent
 * eleven studies finding.
 */
import { chars, cosine, int } from '../../lib/format.ts'
import { hasContributed, isFullyOverlapped, isStarved, orderedTiers } from '../../lib/derive.ts'
import type { TierTrace, TurnTrace } from '../../types/trace.ts'

export function PipelineTab({ trace }: { trace: TurnTrace }) {
  const tiers = orderedTiers(trace)
  const starved = tiers.filter(isStarved)

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
            The budget was spent by an earlier path before these were considered.
            They were not outscored — they were never weighed.
          </div>
        </div>
      )}

      {trace.similarity_detail.inert && (
        <div className="callout callout--warn">
          <span className="callout__mark">!</span>
          <div className="callout__body">
            <div className="callout__title">The RELATED path could not fire</div>
            Its threshold is {trace.similarity_detail.threshold}, and the best cosine
            anything in this store reached was{' '}
            {cosine(trace.similarity_detail.max_relevance_observed)} — short by{' '}
            {cosine(trace.similarity_detail.margin_to_threshold)}. This is a bar set
            above what relevance reaches, not a quiet turn.
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
          <div className="pipe-title">Three paths compete for one budget</div>
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
            Candidates are considered in that order — RECENT, then RELATED, then
            SPREAD — and charged the exact characters they serialize to. One that
            does not fit is skipped, and the walk continues to the next.
          </p>
          <div className="rowflex">
            <Stat label="admitted" value={int(trace.report.episodes_delivered)} />
            <Stat label="dropped" value={int(trace.report.episodes_dropped)} />
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
            {chars(trace.context_block.chars)} / {chars(trace.report.budget_chars)} chars
          </span>
        </div>
        <div className="card__body">
          <div className="rowflex">
            <Stat
              label="recent block"
              value={int(trace.context_block.recent_episode_count)}
              sub="episodes"
            />
            <Stat
              label="retrieved block"
              value={int(trace.context_block.retrieved_episode_count)}
              sub="episodes"
            />
            <Stat
              label="unused budget"
              value={chars(trace.report.chars_available)}
              sub="characters"
            />
            {trace.report.shortfall_chars > 0 && (
              <Stat
                label="shortfall"
                value={chars(trace.report.shortfall_chars)}
                sub="more was wanted"
                tone="warn"
              />
            )}
          </div>
        </div>
      </section>
    </div>
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
