// A quote with its evidence: the dealer's own words and the itemised breakdown exactly as they wrote it.

import type { Quote, Reply } from '../activity'
import { money } from './format'

const FLAG_TEXT: Record<string, string> = {
  breakdown_mismatch: "The parts don't add up to the stated total; the dealer's total is used.",
  breakdown_ungrounded: "Some amounts weren't in the dealer's text and were dropped.",
}

export default function QuoteEvidence({ quote, reply, currency }: { quote: Quote; reply?: Reply; currency: string }) {
  const b = quote.breakdown
  const rows: [string, number | null][] = b ? [
    ['Vehicle price', b.vehicle_price],
    ...b.dealer_fees.map(f => [f.label, f.amount] as [string, number]),
    ...b.add_ons.map(f => [`Add-on: ${f.label}`, f.amount] as [string, number]),
    ['Taxes', b.taxes],
    ['Title & registration', b.title_registration],
    ...b.other.map(f => [f.label, f.amount] as [string, number]),
  ].filter(([, v]) => v != null) as [string, number][] : []
  return <div className="evidence">
    {reply && <blockquote>
      <span className="small">{reply.source === 'call' ? "Fo's call summary (not the dealer's own words)" : reply.source === 'fo_relay' ? 'Relayed by Fo' : `Dealer's own words · ${reply.from}`}</span>
      <p>{reply.text}</p>
    </blockquote>}
    <p className="small">
      {quote.firm ? 'Firm written out-the-door total' : quote.source === 'call' ? 'Spoken on a call: not firm until it arrives in writing' : 'Not a firm out-the-door total'}
      {quote.verification && ` · ${quote.verification === 'VERIFIED' ? 'verified' : 'unverified'}`}
      {quote.delivery_date && ` · available ${quote.delivery_date}`}
      {!quote.meets_spec && ' · does not match the exact spec'}
      {quote.hedge_words.length > 0 && ` · hedged ("${quote.hedge_words.join('", "')}")`}
    </p>
    {rows.length > 0 && <table className="breakdown"><tbody>
      {rows.map(([name, value], i) => <tr key={i}><td>{name}</td><td>{money(value, currency)}</td></tr>)}
      <tr className="total"><td>Out-the-door total</td><td>{money(quote.price_total, currency)}</td></tr>
    </tbody></table>}
    {quote.breakdown_flags.map(f => <p key={f} className="warning">{FLAG_TEXT[f] ?? f}</p>)}
  </div>
}
