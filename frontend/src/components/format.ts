export const money = (value: number | null | undefined, currency = 'USD') =>
  value == null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(value)

export const label = (value: string) => value.toLowerCase().replaceAll('_', ' ')

export const time = (ts: string) => new Date(ts).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

const KINDS: Record<string, string> = {
  rfq: 'Quote request', firm_up: 'Ask for full total', price_match: 'Lowest-price notice', counter: 'Counter at target',
  booking: 'Proceed request', cancel: 'Polite decline',
}
export const kindLabel = (kind: string) => KINDS[kind] ?? label(kind)
