/**
 * A small markdown renderer for chat text.
 *
 * The model answers in markdown (`**bold**`, lists, code fences) but the
 * bubble used to print it raw. This covers the subset chat actually uses:
 * headings, lists (including task lists), blockquotes, horizontal rules,
 * GFM tables, fenced and inline code, bold, italic, strikethrough, links
 * and bare URLs. It builds React elements only, so model output can never
 * inject markup.
 */
import { type ReactNode, useMemo } from 'react'

type Key = () => string

const NEXT_CANDIDATE = /`|[*_~[]|https?:\/\//g
const ITEM_RE = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/
const HR_RE = /^\s{0,3}(?:-[ \t]*){3,}$|^\s{0,3}(?:\*[ \t]*){3,}$|^\s{0,3}(?:_[ \t]*){3,}$/
const HEADINGS: Record<number, 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'> = {
  1: 'h1',
  2: 'h2',
  3: 'h3',
  4: 'h4',
  5: 'h5',
  6: 'h6',
}

function isSpace(c: string | undefined): boolean {
  return c === undefined || c === ' ' || c === '\t' || c === '\n' || c === '\r'
}

function parseInline(src: string, key: Key): ReactNode[] {
  const out: ReactNode[] = []
  let text = ''
  let i = 0
  const flush = () => {
    if (text) {
      out.push(text)
      text = ''
    }
  }

  while (i < src.length) {
    const ch = src[i]

    // -- code span -------------------------------------------------------
    if (ch === '`') {
      let n = 0
      while (i + n < src.length && src[i + n] === '`') n++
      if (n <= 2) {
        let j = i + n
        let close = -1
        while (j < src.length) {
          if (src[j] === '`') {
            let m = 0
            while (j + m < src.length && src[j + m] === '`') m++
            if (m === n) {
              close = j
              break
            }
            j += m
            continue
          }
          j++
        }
        if (close > i + n) {
          flush()
          out.push(<code key={key()}>{src.slice(i + n, close)}</code>)
          i = close + n
          continue
        }
      }
      // unclosed (e.g. mid-stream): the backtick is literal
      text += ch
      i++
      continue
    }

    // -- emphasis: **strong** *em* __strong__ _em_ ***strong em*** -------
    if (ch === '*' || ch === '_') {
      let n = 0
      while (i + n < src.length && src[i + n] === ch) n++
      const marker =
        ch === '*' ? (n >= 3 ? '***' : n >= 2 ? '**' : '*') : n >= 2 ? '__' : '_'
      const mlen = marker.length
      let close = -1
      if (!isSpace(src[i + mlen])) {
        for (let j = i + mlen; j < src.length; j++) {
          if (j > i + mlen && src.startsWith(marker, j) && !isSpace(src[j - 1])) {
            close = j
            break
          }
        }
      }
      // Intraword underscores (`k_threshold`) are not emphasis in CommonMark;
      // they need a boundary on both sides.
      if (
        close !== -1 &&
        (marker[0] !== '_' ||
          ((i === 0 || /[\s(\[{"—–-]/.test(src[i - 1])) &&
            (isSpace(src[close + mlen]) || /[).,;:!?>"’–—-]/.test(src[close + mlen]))))
      ) {
        flush()
        const inner = parseInline(src.slice(i + mlen, close), key)
        if (marker === '***') {
          out.push(
            <strong key={key()}>
              <em>{inner}</em>
            </strong>,
          )
        } else if (marker === '**' || marker === '__') {
          out.push(<strong key={key()}>{inner}</strong>)
        } else {
          out.push(<em key={key()}>{inner}</em>)
        }
        i = close + mlen
        continue
      }
      text += ch
      i++
      continue
    }

    // -- strikethrough ----------------------------------------------------
    if (ch === '~' && src[i + 1] === '~') {
      let close = -1
      for (let j = i + 2; j < src.length; j++) {
        if (j > i + 2 && src.startsWith('~~', j) && !isSpace(src[j - 1])) {
          close = j
          break
        }
      }
      if (close !== -1) {
        flush()
        out.push(<del key={key()}>{parseInline(src.slice(i + 2, close), key)}</del>)
        i = close + 2
        continue
      }
      text += ch
      i++
      continue
    }

    // -- link --------------------------------------------------------------
    if (ch === '[') {
      const labelEnd = src.indexOf(']', i + 1)
      if (labelEnd !== -1 && src[labelEnd + 1] === '(') {
        const urlEnd = src.indexOf(')', labelEnd + 2)
        if (urlEnd !== -1) {
          const url = src.slice(labelEnd + 2, urlEnd).trim()
          if (/^(https?:\/\/|mailto:|#|\/|\.)/.test(url) && !url.includes(' ')) {
            flush()
            out.push(
              <a key={key()} href={url} target="_blank" rel="noreferrer">
                {parseInline(src.slice(i + 1, labelEnd), key)}
              </a>,
            )
            i = urlEnd + 1
            continue
          }
        }
      }
      text += ch
      i++
      continue
    }

    // -- bare URL -----------------------------------------------------------
    if (ch === 'h' && (src.startsWith('http://', i) || src.startsWith('https://', i))) {
      let j = i + 7
      while (j < src.length && !/[\s<>"'\]]/.test(src[j])) j++
      let end = j
      while (end > i + 7 && '.,;:!?'.includes(src[end - 1])) end--
      if (end > i + 7 && (src[end - 1] === ')' || src[end - 1] === ']')) end--
      if (end > i + 7) {
        const url = src.slice(i, end)
        flush()
        out.push(
          <a key={key()} href={url} target="_blank" rel="noreferrer">
            {url}
          </a>,
        )
        i = end
        continue
      }
    }

    // -- plain text: jump to the next candidate -----------------------------
    NEXT_CANDIDATE.lastIndex = i + 1
    const next = NEXT_CANDIDATE.exec(src)
    const j = next ? next.index : src.length
    text += src.slice(i, j)
    i = j
  }

  flush()
  return out
}

interface ListItem {
  indent: number
  ordered: boolean
  text: string
}

function renderList(items: ListItem[], key: Key): ReactNode[] {
  interface Node {
    item: ListItem
    children: Node[]
  }
  const roots: Node[] = []
  const stack: Node[] = []
  for (const item of items) {
    const node: Node = { item, children: [] }
    while (stack.length && stack[stack.length - 1].item.indent >= item.indent) stack.pop()
    if (stack.length) stack[stack.length - 1].children.push(node)
    else roots.push(node)
    stack.push(node)
  }

  const renderNodes = (nodes: Node[]): ReactNode[] =>
    nodes.map((node, index) => {
      // `- [ ]` / `- [x]` task items: the checkbox is display-only; the
      // model's list is data, not form state.
      const task = node.item.text.match(/^\[([ xX])\]\s+(.*)$/)
      return (
        <li key={index} className={task ? 'md-task' : undefined}>
          {task ? (
            <>
              <input
                className="md-task-box"
                type="checkbox"
                readOnly
                checked={task[1].toLowerCase() === 'x'}
              />
              {parseInline(task[2], key)}
            </>
          ) : (
            parseInline(node.item.text, key)
          )}
          {node.children.length ? renderNodes(node.children) : null}
        </li>
      )
    })

  const Tag = items[0].ordered ? 'ol' : 'ul'
  return [<Tag key={key()}>{renderNodes(roots)}</Tag>]
}

// GFM tables: a header row, a delimiter row on the very next line, then as
// many body rows as follow. A header seen mid-stream before its delimiter
// exists renders as paragraph text until the delimiter arrives, then the
// re-parse turns the whole run into a table.
type Align = 'left' | 'right' | 'center' | null

function splitRow(line: string): string[] {
  let t = line.trim()
  if (t.startsWith('|')) t = t.slice(1)
  if (t.endsWith('|') && t.length > 1) t = t.slice(0, -1)
  const cells: string[] = []
  let cur = ''
  for (let k = 0; k < t.length; k++) {
    if (t[k] === '\\' && t[k + 1] === '|') {
      cur += '|'
      k++
    } else if (t[k] === '|') {
      cells.push(cur.trim())
      cur = ''
    } else {
      cur += t[k]
    }
  }
  cells.push(cur.trim())
  return cells
}

function tableAligns(line: string): Align[] | null {
  const cells = splitRow(line)
  if (!cells.length) return null
  const out: Align[] = []
  for (const cell of cells) {
    const body = cell.replace(/:/g, '')
    if (!/^-+$/.test(body)) return null
    const left = cell.startsWith(':')
    const right = cell.endsWith(':')
    out.push(left && right ? 'center' : right ? 'right' : left ? 'left' : null)
  }
  return out
}

const isTableStart = (lines: string[], i: number): boolean =>
  lines[i].includes('|') && i + 1 < lines.length && tableAligns(lines[i + 1]) !== null

function renderTable(
  header: string[],
  aligns: Align[],
  rows: string[][],
  key: Key,
): ReactNode {
  const align = (c: number) =>
    aligns[c] ? { textAlign: aligns[c] } : undefined
  return (
    <div className="md-table-wrap" key={key()}>
      <table className="md-table">
        <thead>
          <tr>
            {header.map((cell, c) => (
              <th key={c} style={align(c)}>
                {parseInline(cell, key)}
              </th>
            ))}
          </tr>
        </thead>
        {rows.length ? (
          <tbody>
            {rows.map((cells, r) => (
              <tr key={r}>
                {header.map((_, c) => (
                  <td key={c} style={align(c)}>
                    {parseInline(cells[c] ?? '', key)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        ) : null}
      </table>
    </div>
  )
}

const isBlockStart = (line: string): boolean =>
  !line.trim() ||
  /^\s{0,3}(?:`{3,}|~{3,})/.test(line) ||
  /^\s{0,3}#{1,6}\s/.test(line) ||
  /^\s{0,3}>/.test(line) ||
  HR_RE.test(line) ||
  ITEM_RE.test(line)

function renderBlocks(src: string, key: Key): ReactNode[] {
  const lines = src.split('\n')
  const out: ReactNode[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) {
      i++
      continue
    }

    // fenced code — an unclosed fence (mid-stream) runs to the end
    const fence = line.match(/^\s{0,3}(`{3,}|~{3,})[ \t]*(\S*)/)
    if (fence) {
      const fenceChar = fence[1][0]
      const fenceLen = fence[1].length
      const lang = fence[2]
      const body: string[] = []
      let j = i + 1
      while (j < lines.length) {
        const t = lines[j].trim()
        if (t.length >= fenceLen && t.split('').every((c) => c === fenceChar)) break
        body.push(lines[j])
        j++
      }
      i = j < lines.length ? j + 1 : j
      out.push(
        <pre
          key={key()}
          className={lang ? `md-pre md-pre--${lang}` : 'md-pre'}
          title={lang || undefined}
        >
          <code>{body.join('\n')}</code>
        </pre>,
      )
      continue
    }

    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/)
    if (heading) {
      const Tag = HEADINGS[heading[1].length]
      out.push(<Tag key={key()}>{parseInline(heading[2], key)}</Tag>)
      i++
      continue
    }

    if (HR_RE.test(line)) {
      out.push(<hr key={key()} />)
      i++
      continue
    }

    if (/^\s{0,3}>/.test(line)) {
      const inner: string[] = []
      let j = i
      while (j < lines.length && /^\s{0,3}>/.test(lines[j])) {
        inner.push(lines[j].replace(/^\s{0,3}>[ \t]?/, ''))
        j++
      }
      i = j
      out.push(<blockquote key={key()}>{renderBlocks(inner.join('\n'), key)}</blockquote>)
      continue
    }

    if (isTableStart(lines, i)) {
      const header = splitRow(line)
      const aligns = tableAligns(lines[i + 1]) as Align[]
      const rows: string[][] = []
      let j = i + 2
      while (j < lines.length && lines[j].trim() && lines[j].includes('|')) {
        rows.push(splitRow(lines[j]))
        j++
      }
      i = j
      out.push(renderTable(header, aligns, rows, key))
      continue
    }

    if (ITEM_RE.test(line)) {
      const items: ListItem[] = []
      let j = i
      while (j < lines.length) {
        const m = lines[j].match(ITEM_RE)
        if (m) {
          items.push({
            indent: m[1].replace(/\t/g, '  ').length,
            ordered: /\d/.test(m[2][0]),
            text: m[3],
          })
          j++
        } else if (items.length && lines[j].trim() && /^\s{2,}\S/.test(lines[j])) {
          items[items.length - 1].text += ' ' + lines[j].trim()
          j++
        } else {
          break
        }
      }
      i = j
      out.push(...renderList(items, key))
      continue
    }

    // paragraph: single newlines are chat line breaks, so join with <br/>
    const para: string[] = []
    let j = i
    while (
      j < lines.length &&
      !isBlockStart(lines[j]) &&
      !isTableStart(lines, j)
    ) {
      para.push(lines[j])
      j++
    }
    i = j
    const nodes: ReactNode[] = []
    para.forEach((l, k) => {
      if (k > 0) nodes.push(<br key={`br${k}`} />)
      nodes.push(...parseInline(l, key))
    })
    out.push(<p key={key()}>{nodes}</p>)
  }

  return out
}

export function Markdown({ text }: { text: string }) {
  const blocks = useMemo(() => {
    let counter = 0
    const key = () => `m${counter++}`
    return renderBlocks(text, key)
  }, [text])
  return <div className="md">{blocks}</div>
}
