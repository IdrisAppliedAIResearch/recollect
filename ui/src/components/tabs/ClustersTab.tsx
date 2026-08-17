/**
 * The topic partition the coverage selector reasons over.
 *
 * The selector's whole novelty term is "is this cluster already covered",
 * so which clusters it entered — and which it entered but then lost to
 * packing — is the difference between a spread that worked and one that
 * only looked like it did.
 */
import { cosine, int, pct } from '../../lib/format.ts'
import type { ClusterTrace, TurnTrace } from '../../types/trace.ts'

export function ClustersTab({ trace }: { trace: TurnTrace }) {
  const clusters = trace.clusters
  if (clusters.length === 0) {
    return (
      <div className="empty">
        <div className="empty__title">No clustering this turn</div>
        <p>
          The candidate pool was empty, or the budget was too small to run the
          coverage selector at all.
        </p>
      </div>
    )
  }

  const entered = clusters.filter((cluster) => cluster.selected_ids.length > 0)
  const landed = clusters.filter((cluster) => cluster.delivered_ids.length > 0)

  return (
    <div className="stack">
      <div className="rowflex">
        <Stat label="clusters" value={int(clusters.length)} sub="over the pool" />
        <Stat
          label="entered"
          value={`${entered.length} / ${clusters.length}`}
          sub={pct(entered.length / clusters.length)}
        />
        <Stat
          label="reached the model"
          value={`${landed.length} / ${clusters.length}`}
          sub={pct(landed.length / clusters.length)}
          tone={landed.length < entered.length ? 'warn' : undefined}
        />
      </div>

      {landed.length < entered.length && (
        <div className="callout callout--warn">
          <span className="callout__mark">!</span>
          <div className="callout__body">
            <div className="callout__title">
              {entered.length - landed.length} cluster
              {entered.length - landed.length === 1 ? '' : 's'} were entered and then lost
            </div>
            The selector paid its novelty bonus to reach into these regions, and
            packing dropped the episode before it arrived. The spread it computed is
            not the spread the model got.
          </div>
        </div>
      )}

      <div className="legendrow mono">
        <span>
          <span className="memberdot" data-state="delivered" /> delivered
        </span>
        <span>
          <span className="memberdot" data-state="selected" /> selected, dropped
        </span>
        <span>
          <span className="memberdot" data-state="idle" /> not selected
        </span>
      </div>

      <div className="clustergrid">
        {clusters.map((cluster) => (
          <Cell key={cluster.id} cluster={cluster} />
        ))}
      </div>
    </div>
  )
}

function Cell({ cluster }: { cluster: ClusterTrace }) {
  const selected = new Set(cluster.selected_ids)
  const delivered = new Set(cluster.delivered_ids)
  const state =
    delivered.size > 0 ? 'delivered' : selected.size > 0 ? 'lost' : 'idle'

  return (
    <div className="clustercell" data-state={state}>
      <div className="clustercell__head">
        <span className="clustercell__id mono">#{cluster.id}</span>
        <span className="clustercell__size mono">{cluster.size}</span>
      </div>

      <div className="clustercell__dots">
        {cluster.member_ids.map((id) => (
          <span
            key={id}
            className="memberdot"
            data-state={
              delivered.has(id) ? 'delivered' : selected.has(id) ? 'selected' : 'idle'
            }
          />
        ))}
      </div>

      <div className="clustercell__meta mono">
        <span title="mean cosine of this cluster's members">
          x̄ {cosine(cluster.mean_relevance)}
        </span>
        <span title="best cosine in this cluster">
          max {cosine(cluster.max_relevance)}
        </span>
      </div>

      <div className="clustercell__state">
        {state === 'delivered' && <span className="badge badge--ok">reached</span>}
        {state === 'lost' && <span className="badge badge--warn">entered, dropped</span>}
        {state === 'idle' && <span className="badge">not entered</span>}
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
