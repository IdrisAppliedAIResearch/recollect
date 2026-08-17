/**
 * Whether anything on the other six tabs can be believed.
 *
 * Every number this UI shows is produced by an instrumented reconstruction
 * of the retrieval pipeline, not by the library that actually built the
 * context. So the harness runs both and compares them — the payload byte for
 * byte, the report field for field. If they disagree, the trace describes a
 * computation that did not happen, and this tab says so before you draw a
 * conclusion from it.
 */
import { chars, ms, pct, shortHash } from '../../lib/format.ts'
import { cacheHitRatio, isTrustworthy } from '../../lib/derive.ts'
import type { TurnTrace } from '../../types/trace.ts'

export function VerifyTab({ trace }: { trace: TurnTrace }) {
  const verification = trace.verification
  const trusted = isTrustworthy(verification)
  const generation = trace.generation
  const cache = generation ? cacheHitRatio(generation.prompt_cache) : null

  return (
    <div className="stack">
      <div className={trusted ? 'verdict' : 'verdict verdict--bad'}>
        <span className="verdict__glyph">{trusted ? '✓' : '✕'}</span>
        <div>
          <div className="verdict__title">
            {trusted
              ? 'The trace reproduces the library exactly'
              : 'The trace does NOT match the library'}
          </div>
          <div className="verdict__sub">
            {trusted
              ? 'The instrumented reconstruction produced the same characters and the ' +
                'same counts as the untouched mechanism. Everything on the other tabs ' +
                'describes what actually happened.'
              : 'The reconstruction and the mechanism disagree. Treat every score, ' +
                'attribution and drop reason in this turn as unreliable.'}
          </div>
        </div>
      </div>

      <section className="card">
        <div className="card__head">
          <span className="card__title">Payload comparison</span>
          <span className="card__note mono">episodic {verification.library_version}</span>
        </div>
        <div className="card__body">
          <div className="hashrow mono">
            <span className="strip__label">authority</span>
            <span className={verification.payload_identical ? 'hash--match' : 'hash--mismatch'}>
              {shortHash(verification.authority_payload_sha256, 32)}
            </span>
          </div>
          <div className="hashrow mono">
            <span className="strip__label">shadow</span>
            <span className={verification.payload_identical ? 'hash--match' : 'hash--mismatch'}>
              {shortHash(verification.shadow_payload_sha256, 32)}
            </span>
          </div>

          {verification.mismatched_fields.length > 0 && (
            <ul className="mismatch-list mono">
              {verification.mismatched_fields.map((entry) => (
                <li key={entry}>{entry}</li>
              ))}
            </ul>
          )}

          <div className="rowflex">
            <Stat
              label="payload"
              value={verification.payload_identical ? 'identical' : 'DIFFERENT'}
              tone={verification.payload_identical ? 'ok' : 'bad'}
            />
            <Stat
              label="report fields"
              value={verification.report_fields_identical ? 'identical' : 'DIFFERENT'}
              tone={verification.report_fields_identical ? 'ok' : 'bad'}
            />
            <Stat
              label="shadow cost"
              value={ms(verification.shadow_latency_ms)}
              sub="to compute twice"
            />
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card__head">
          <span className="card__title">Embedding identity</span>
        </div>
        <div className="card__body">
          <div className="kv mono">
            <span className="strip__label">query vector</span>
            <span>{shortHash(trace.query.embedding_sha256, 24)}</span>
            <span className="strip__label">norm</span>
            <span>{trace.query.embedding_norm.toFixed(5)}</span>
            <span className="strip__label">store sentinel</span>
            <span>{shortHash(trace.store.sentinel_sha256, 24)}</span>
            <span className="strip__label">model artifact</span>
            <span>{shortHash(trace.store.embedder_model_sha256, 24)}</span>
            <span className="strip__label">embed latency</span>
            <span>
              {ms(trace.query.embed_latency_ms)}
              {trace.query.embed_cache_hit ? ' (cached)' : ''}
            </span>
          </div>
          <p className="section-note">
            The store re-embeds a fixed sentinel on every open and refuses to serve if
            the digest moved. A match means stored vectors and this query vector live
            in the same space, so the cosines on the Scores tab are comparable.
          </p>
        </div>
      </section>

      {generation && (
        <section className="card">
          <div className="card__head">
            <span className="card__title">Generation</span>
            <span className="card__note mono">{generation.model}</span>
          </div>
          <div className="card__body">
            <div className="rowflex">
              <Stat label="time to first token" value={ms(generation.ttft_ms)} />
              <Stat label="total" value={ms(generation.total_ms)} />
              <Stat
                label="throughput"
                value={
                  generation.tokens_per_sec
                    ? `${generation.tokens_per_sec.toFixed(1)} tok/s`
                    : '—'
                }
                sub={generation.tokens_out ? `${generation.tokens_out} tokens` : undefined}
              />
              <Stat
                label="prompt cache"
                value={cache === null ? '—' : pct(cache)}
                sub={
                  generation.prompt_cache.prompt_tokens
                    ? `${generation.prompt_cache.cached_tokens ?? 0} reused, ` +
                      `${generation.prompt_cache.processed_tokens ?? 0} prefilled`
                    : 'not reported'
                }
              />
            </div>

            <div className="kv mono">
              <span className="strip__label">system prompt</span>
              <span>{chars(generation.system_prompt_chars)} chars</span>
              <span className="strip__label">memory block</span>
              <span>{chars(generation.context_block_chars)} chars</span>
              <span className="strip__label">total prompt</span>
              <span>{chars(generation.total_prompt_chars)} chars</span>
              <span className="strip__label">thinking</span>
              <span>{generation.thinking_enabled ? 'enabled' : 'disabled'}</span>
              <span className="strip__label">finish reason</span>
              <span>{generation.finish_reason ?? '—'}</span>
            </div>

            <p className="section-note">
              This architecture rewrites its memory block every turn by design, so most
              of the server's prefix cache is forfeited on purpose. A low hit ratio here
              is the mechanism working, not a fault — but it is what the rebuild costs.
            </p>

            {generation.error && (
              <div className="callout callout--bad">
                <span className="callout__mark">✕</span>
                <div className="callout__body">{generation.error}</div>
              </div>
            )}
          </div>
        </section>
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
