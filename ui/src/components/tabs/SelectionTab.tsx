/**
 * What was admitted this turn, and what nearly was.
 *
 * The one thing this view exists to make unmissable: a block that is
 * nothing but the last N exchanges. Nothing is dropped on this path and
 * nothing competes for room, so "retrieved N episodes" always looks
 * healthy — even on a turn where the threshold admitted nothing at all and
 * long-term memory contributed exactly zero. The overlap between the two
 * conditions is what tells those turns apart, and it is invisible in any
 * delivered count.
 */
import { chars, int, ms, truncate } from '../../lib/format.ts'
import { nearMisses, relevanceOnlyIds } from '../../lib/derive.ts'
import { PATH_DESCRIPTIONS, PATH_LABELS } from '../../types/trace.ts'
import type { CandidateTrace, SubagentTrace, TurnTrace } from '../../types/trace.ts'

export function SelectionTab({ trace }: { trace: TurnTrace }) {
  const timeline = trace.timeline
  const report = trace.report
  const ceiling = trace.ceiling
  const relevanceOnly = relevanceOnlyIds(trace)
  const misses = nearMisses(trace)
  const overlap = timeline.overlap_ids.length
  const overCeiling =
    ceiling.ceiling_chars !== null &&
    report.chars_delivered > ceiling.ceiling_chars

  return (
    <div className="stack">
      {ceiling.engaged && (
        <div className="callout callout--warn">
          <span className="callout__mark">!</span>
          <div className="callout__body">
            <div className="callout__title">
              The deployment ceiling withheld {int(ceiling.withheld_ids.length)}{' '}
              episode{ceiling.withheld_ids.length === 1 ? '' : 's'} the
              mechanism would have delivered
            </div>
            These cleared the relevance threshold. They were excluded before
            the library saw them, lowest cosine first, to fit{' '}
            {chars(ceiling.ceiling_chars ?? 0)} — a hardware limit of this
            deployment, not a property of the mechanism, which caps nothing.
            Continuity is never withheld.
          </div>
        </div>
      )}

      {overCeiling && (
        <div className="callout callout--warn">
          <span className="callout__mark">!</span>
          <div className="callout__body">
            <div className="callout__title">
              The continuity window alone exceeds the ceiling
            </div>
            {chars(report.chars_delivered)} delivered against a{' '}
            {chars(ceiling.ceiling_chars ?? 0)} ceiling. Losing what was just
            said would be the worse failure, so the last{' '}
            {int(timeline.recency_window_n)} exchanges are delivered anyway and
            the ceiling is exceeded deliberately. If this persists, the window
            is too large for the deployed context.
          </div>
        </div>
      )}

      {timeline.relevance_only_count === 0 && timeline.eligible_count > 0 && (
        <div className="callout callout--warn">
          <span className="callout__mark">!</span>
          <div className="callout__body">
            <div className="callout__title">
              Long-term memory contributed nothing this turn
            </div>
            Every episode that cleared the threshold was already inside the
            continuity window. The model saw exactly what the last{' '}
            {int(timeline.recency_window_n)} exchanges would have given it on
            their own — retrieval added no episode it would otherwise have
            missed.
          </div>
        </div>
      )}

      <section className="card">
        <div className="card__head">
          <span className="card__title">Store</span>
          <span className="card__note mono">
            {int(ceiling.store_episodes)} stored ·{' '}
            {int(ceiling.considered_episodes)} shown to the library ·{' '}
            {int(timeline.eligible_count)} eligible
          </span>
        </div>
        <div className="card__body">
          <div className="pipe-title">
            Two conditions admit an episode. The block is their union, in
            source order — no ranking, no allowance, nothing dropped.
          </div>
          <div className="two-col">
            <PathCard
              path="relevance"
              count={timeline.relevant_ids.length}
              overlap={overlap}
              exclusive={timeline.relevance_only_count}
              note={`cosine >= ${timeline.relevance_threshold}`}
            />
            <PathCard
              path="continuity"
              count={timeline.recent_ids.length}
              overlap={overlap}
              exclusive={timeline.recent_ids.length - overlap}
              note={`last ${int(timeline.recency_window_n)} exchanges`}
            />
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card__head">
          <span className="card__title">Union</span>
          <span className="card__note mono">{timeline.read_policy}</span>
        </div>
        <div className="card__body">
          <p className="section-note">
            An episode satisfying both conditions is delivered once. The
            overlap is the honest measure of what the threshold added:
            subtract it from the relevant count and what remains is the set
            continuity would never have supplied.
          </p>
          <div className="statgrid">
            <Stat
              label="delivered"
              value={int(report.episodes_delivered)}
              sub="the rendered union"
            />
            <Stat
              label="on relevance alone"
              value={int(timeline.relevance_only_count)}
              sub="what retrieval added"
              tone={timeline.relevance_only_count === 0 ? 'warn' : undefined}
            />
            <Stat
              label="on continuity alone"
              value={int(timeline.recent_ids.length - overlap)}
              sub="scored below the threshold"
            />
            <Stat
              label="both"
              value={int(overlap)}
              sub="counted once"
            />
            <Stat
              label="not delivered"
              value={int(timeline.eligible_count - report.episodes_delivered)}
              sub="missed both conditions"
            />
            {ceiling.engaged && (
              <Stat
                label="withheld by ceiling"
                value={int(ceiling.withheld_ids.length)}
                sub={`${chars(ceiling.withheld_chars)} not shown`}
                tone="warn"
              />
            )}
          </div>
          {timeline.through_turn !== null && (
            <p className="section-note">
              A horizon of turn {int(timeline.through_turn)} was applied, so{' '}
              {int(report.pool_size - timeline.eligible_count)} stored
              episode(s) were outside this read entirely.
            </p>
          )}
        </div>
      </section>

      {misses.length > 0 && (
        <section className="card">
          <div className="card__head">
            <span className="card__title">Nearest misses</span>
            <span className="card__note mono">
              threshold {timeline.relevance_threshold}
            </span>
          </div>
          <div className="card__body">
            <p className="section-note">
              With no capacity to run out of, an undelivered episode has
              exactly one reason: it scored below the threshold. These came
              closest.
            </p>
            <table className="rows">
              <thead>
                <tr>
                  <th>turn</th>
                  <th>cosine</th>
                  <th>margin</th>
                  <th>episode</th>
                </tr>
              </thead>
              <tbody>
                {misses.map((candidate) => (
                  <MissRow key={candidate.id} candidate={candidate} />
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="card">
        <div className="card__head">
          <span className="card__title">Context window</span>
          <span className="card__note mono">
            {chars(trace.context_block.chars)} total
          </span>
        </div>
        <div className="card__body">
          <div className="statgrid">
            <Stat
              label="episodes"
              value={int(trace.context_block.retrieved_episode_count)}
              sub="rendered chronologically"
            />
            <Stat
              label="of which recent"
              value={int(trace.context_block.recent_episode_count)}
              sub="continuity window"
            />
            <Stat
              label="characters"
              value={chars(report.chars_delivered)}
              sub="no ceiling on this path"
            />
            <Stat
              label="retrieval latency"
              value={ms(report.latency_ms)}
              sub="library, excluding embed"
            />
          </div>
          {relevanceOnly.length > 0 && (
            <details>
              <summary>
                {int(relevanceOnly.length)} episode
                {relevanceOnly.length === 1 ? '' : 's'} only long-term memory
                supplied
              </summary>
              <ul className="mono">
                {relevanceOnly.map((id) => (
                  <li key={id}>{id}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
      </section>

      {trace.generation &&
        ((trace.generation.task_context_chars ?? 0) > 0 ||
          trace.generation.model_queue_ms != null) && (
          <section className="card">
            <div className="card__head">
              <span className="card__title">Generation input</span>
            </div>
            <div className="card__body">
              <p className="section-note">
                Task context is supplied separately from verified memory. It
                does not create memory episodes.
              </p>
              <div className="statgrid">
                <Stat
                  label="memory input"
                  value={chars(trace.generation.context_block_chars)}
                  sub="verified retrieval payload"
                />
                <Stat
                  label="task context"
                  value={chars(trace.generation.task_context_chars ?? 0)}
                  sub="separate generation input"
                />
                <Stat
                  label="total prompt"
                  value={chars(trace.generation.total_prompt_chars)}
                  sub="includes task handoff input"
                />
                <Stat
                  label="model queue wait"
                  value={ms(trace.generation.model_queue_ms ?? null)}
                  sub="before this model request"
                />
              </div>
              {(trace.generation.task_ids?.length ?? 0) > 0 && (
                <details>
                  <summary>Referenced tasks</summary>
                  <ul className="mono">
                    {trace.generation.task_ids!.map((id) => (
                      <li key={id}>{id}</li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          </section>
        )}

      {trace.subagent && (
        <ResearchCard
          sub={trace.subagent}
          answered={Boolean(trace.generation?.response_text.trim())}
        />
      )}
    </div>
  )
}

function MissRow({ candidate }: { candidate: CandidateTrace }) {
  return (
    <tr>
      <td className="mono">{int(candidate.turn_number)}</td>
      <td className="mono">{candidate.cosine.toFixed(4)}</td>
      <td className="mono">{candidate.margin.toFixed(4)}</td>
      <td title={candidate.preview}>{truncate(candidate.preview, 90)}</td>
    </tr>
  )
}

function PathCard({
  path,
  count,
  overlap,
  exclusive,
  note,
}: {
  path: 'relevance' | 'continuity'
  count: number
  overlap: number
  exclusive: number
  note: string
}) {
  const inert = count > 0 && exclusive === 0
  return (
    <div
      className={'tier-card' + (inert ? ' tier-card--dashed' : '')}
      data-tier={path}
    >
      <div className="tier-card__head">
        <span className="tiermark" data-tier={path}>
          <span className="tiermark__code">{PATH_LABELS[path]}</span>
        </span>
        <span className={inert ? 'badge badge--warn' : 'badge'}>
          {count === 0
            ? 'admitted nothing'
            : inert
              ? 'all already covered'
              : 'contributed'}
        </span>
      </div>

      <div className="tier-card__nums">
        <div>
          <span className="tier-card__num">{int(count)}</span>
          <span className="strip__label">qualified</span>
        </div>
        <div>
          <span className="tier-card__num">{int(exclusive)}</span>
          <span className="strip__label">only here</span>
        </div>
        <div>
          <span className="tier-card__num">{int(overlap)}</span>
          <span className="strip__label">shared</span>
        </div>
      </div>

      <div className="tier-card__desc">{PATH_DESCRIPTIONS[path]}</div>
      <div className="card__note mono">{note}</div>
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
