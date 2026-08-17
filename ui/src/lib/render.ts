/**
 * A TypeScript mirror of `episodic/_render.py` and `_selection.additive_weight`.
 *
 * Used only by the mock generator, so that mock traces are internally
 * consistent: the payload it shows really is the packing walk it reports, and
 * `render_chars` really is the cost of admitting that episode. Nothing in the
 * live path renders anything — the payload always comes from the server.
 */

export interface RenderableEpisode {
  turn_number: number
  user_message: string
  assistant_message: string
}

function escapeText(value: string): string {
  // html.escape(quote=False): & < > only.
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function escapeAttribute(value: string): string {
  // html.escape(quote=True): & < > " '
  return escapeText(value).replace(/"/g, '&quot;').replace(/'/g, '&#x27;')
}

export function renderEpisodeElement(episode: RenderableEpisode): string {
  return [
    `<episode turn="${escapeAttribute(String(episode.turn_number))}">`,
    `<user>${escapeText(episode.user_message)}</user>`,
    `<assistant>${escapeText(episode.assistant_message)}</assistant>`,
    '</episode>',
  ].join('\n')
}

function renderEpisodeBlock(name: string, episodes: RenderableEpisode[]): string {
  if (episodes.length === 0) return `<${name}/>`
  return [
    `<${name}>`,
    ...episodes.map(renderEpisodeElement),
    `</${name}>`,
  ].join('\n')
}

export function renderStmPayload(
  recent: RenderableEpisode[],
  stm: RenderableEpisode[],
): string {
  return [
    renderEpisodeBlock('recent_context', recent),
    renderEpisodeBlock('retrieved_stm', stm),
  ].join('\n\n')
}

/** Serialized cost an episode adds inside a non-empty block. */
export function additiveWeight(episode: RenderableEpisode): number {
  return renderEpisodeElement(episode).length + 1
}

/** len(render_stm_payload([], [])) — 35 characters of empty block tags. */
export const EMPTY_PAYLOAD_CHARS = renderStmPayload([], []).length
