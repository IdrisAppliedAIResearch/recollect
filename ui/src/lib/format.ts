const INT = new Intl.NumberFormat('en-US')

export function int(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return INT.format(Math.round(value))
}

export function chars(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return INT.format(Math.round(value))
}

/** Cosines are the load-bearing number here; four places, never rounded away. */
export function cosine(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toFixed(4)
}

export function gain(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toFixed(4)
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  if (value >= 10_000) return `${(value / 1000).toFixed(1)} s`
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`
  if (value >= 10) return `${value.toFixed(0)} ms`
  return `${value.toFixed(1)} ms`
}

export function shortHash(value: string | null | undefined, size = 12): string {
  if (!value) return '—'
  return value.slice(0, size)
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function stamp(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString()
}

export function truncate(text: string, size: number): string {
  if (text.length <= size) return text
  return `${text.slice(0, size - 1)}…`
}
