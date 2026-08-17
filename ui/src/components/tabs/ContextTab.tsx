/**
 * The context block, exactly as the model received it.
 *
 * This is the ground truth of the whole system: whatever the other tabs
 * claim was chosen, these are the characters that were actually sent. The
 * raw view is one click away and is byte-for-byte the payload — no
 * reformatting, because a pretty-printer that quietly changed a character
 * would undermine the only exact artifact here.
 */
import { useMemo, useState } from 'react'

import { chars, pct } from '../../lib/format.ts'
import { orderedTiers } from '../../lib/derive.ts'
import type { TierName, TurnTrace } from '../../types/trace.ts'

interface ParsedEpisode {
  turn: number
  user: string
  assistant: string
}

interface ParsedBlock {
  name: string
  empty: boolean
  episodes: ParsedEpisode[]
}

export function ContextTab({ trace }: { trace: TurnTrace }) {
  const [raw, setRaw] = useState(false)
  const payload = trace.context_block.payload
  const blocks = useMemo(() => parseBlocks(payload), [payload])

  const tierOfTurn = useMemo(() => {
    const map = new Map<number, TierName | null>()
    for (const candidate of trace.candidates) {
      if (candidate.delivered) map.set(candidate.turn_number, candidate.delivered_via)
    }
    return map
  }, [trace.candidates])

  return (
    <div className="stack">
      <div className="ctx-toolbar">
        <div className="rowflex">
          <button
            type="button"
            className={raw ? 'ctl' : 'ctl ctl--on'}
            onClick={() => setRaw(false)}
          >
            Structured
          </button>
          <button
            type="button"
            className={raw ? 'ctl ctl--on' : 'ctl'}
            onClick={() => setRaw(true)}
          >
            Raw
          </button>
          <button
            type="button"
            className="ctl"
            onClick={() => void navigator.clipboard?.writeText(payload)}
          >
            Copy
          </button>
        </div>
        <span className="card__note mono">
          sha256 {trace.context_block.sha256.slice(0, 16)}…
        </span>
      </div>

      <Ruler trace={trace} />

      {raw ? (
        <pre className="payload">{payload || '(empty)'}</pre>
      ) : (
        <div className="payload payload--wrap">
          {blocks.length === 0 && <div className="empty__title">Nothing was delivered</div>}
          {blocks.map((block) => (
            <section key={block.name} className="card">
              <div className="card__head">
                <span className="card__title mono">
                  <span className="block-tag">&lt;{block.name}&gt;</span>
                </span>
                <span className="card__note mono">
                  {block.empty ? 'empty' : `${block.episodes.length} episodes`}
                </span>
              </div>
              <div className="card__body card__body--flush">
                {block.empty && (
                  <div className="block-empty mono">
                    &lt;{block.name}/&gt; — this path delivered nothing
                  </div>
                )}
                {block.episodes.map((episode) => {
                  const tier = tierOfTurn.get(episode.turn) ?? null
                  return (
                    <article
                      key={`${block.name}-${episode.turn}`}
                      className="ep"
                      data-tier={tier ?? 'none'}
                    >
                      <div className="ep__head mono">
                        <span className="xml-tag">&lt;episode</span>{' '}
                        <span className="xml-attr">turn="{episode.turn}"</span>
                        <span className="xml-tag">&gt;</span>
                        {tier && (
                          <span className="tiermark" data-tier={tier}>
                            <span className="tiermark__code">{tier}</span>
                          </span>
                        )}
                      </div>
                      <div className="ep__body">
                        <div>
                          <span className="xml-role mono">user</span> {episode.user}
                        </div>
                        <div>
                          <span className="xml-role mono">assistant</span>{' '}
                          {episode.assistant}
                        </div>
                      </div>
                    </article>
                  )
                })}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  )
}

function Ruler({ trace }: { trace: TurnTrace }) {
  const budget = Math.max(trace.report.budget_chars, 1)
  const tiers = orderedTiers(trace)

  return (
    <div className="ruler">
      <div className="ruler__fill">
        {tiers.map((tier) => (
          <div
            key={tier.name}
            className="ruler__seg"
            data-tier={tier.name}
            style={{ width: `${(tier.chars_delivered / budget) * 100}%` }}
            title={`${tier.label}: ${chars(tier.chars_delivered)} chars`}
          />
        ))}
        <div className="ruler__cap" />
      </div>
      <div className="ruler__legend mono">
        {tiers.map((tier) => (
          <span key={tier.name}>
            <span className="swatch" data-tier={tier.name} /> {tier.label}{' '}
            {chars(tier.chars_delivered)}
          </span>
        ))}
        <span className="ruler__tick">
          {chars(trace.report.chars_delivered)} / {chars(trace.report.budget_chars)} (
          {pct(trace.report.chars_delivered / budget)})
        </span>
      </div>
    </div>
  )
}

/**
 * Parse the renderer's output. Deliberately literal rather than using
 * DOMParser: the payload is not guaranteed well-formed XML (episode bodies
 * are escaped text, but nothing promises the escaping survives every future
 * change), and a lenient regex that fails visibly beats a strict parser that
 * throws on the one turn you needed to look at.
 */
function parseBlocks(payload: string): ParsedBlock[] {
  const blocks: ParsedBlock[] = []
  const names = ['recent_context', 'retrieved_stm']

  for (const name of names) {
    if (new RegExp(`<${name}/>`).test(payload)) {
      blocks.push({ name, empty: true, episodes: [] })
      continue
    }
    const body = new RegExp(`<${name}>([\\s\\S]*?)</${name}>`).exec(payload)
    if (!body) continue
    blocks.push({ name, empty: false, episodes: parseEpisodes(body[1] ?? '') })
  }
  return blocks
}

function parseEpisodes(body: string): ParsedEpisode[] {
  const episodes: ParsedEpisode[] = []
  const pattern = /<episode turn="([^"]*)">([\s\S]*?)<\/episode>/g
  let match = pattern.exec(body)
  while (match !== null) {
    const inner = match[2] ?? ''
    episodes.push({
      turn: Number(match[1] ?? 0),
      user: unescapeText(/<user>([\s\S]*?)<\/user>/.exec(inner)?.[1] ?? ''),
      assistant: unescapeText(
        /<assistant>([\s\S]*?)<\/assistant>/.exec(inner)?.[1] ?? '',
      ),
    })
    match = pattern.exec(body)
  }
  return episodes
}

function unescapeText(value: string): string {
  return value
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&amp;/g, '&')
}
