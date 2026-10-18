// One card per dealer: contact details, latest quote, and the full history of what we sent and what came back.

import { useEffect, useRef } from 'react'
import type { Activity, DealerActivity as Dealer } from '../activity'
import QuoteEvidence from './QuoteEvidence'
import { kindLabel, label, money, time } from './format'

export default function DealerActivity({ activity, currency, states, focus }: {
  activity: Activity; currency: string; states?: Map<string, string>
  focus?: { id: string; n: number } | null   // open + scroll to this dealer (n changes on every request)
}) {
  if (activity.dealers.length === 0) return <section className="panel"><p className="muted">No dealer activity yet.</p></section>
  return <section className="panel">
    <div className="section-title"><h3>Dealer activity</h3><span className="small">Everything sent and received, per dealer. Click a dealer to open its history.</span></div>
    <div className="dealer-cards">{activity.dealers.map(d => <DealerCard key={d.id} dealer={d} activity={activity} currency={currency} state={states?.get(d.id)} focus={focus?.id === d.id ? focus.n : null} />)}</div>
  </section>
}

function DealerCard({ dealer, activity, currency, state, focus }: { dealer: Dealer; activity: Activity; currency: string; state?: string; focus: number | null }) {
  const q = dealer.latest_quote
  const ref = useRef<HTMLDetailsElement>(null)
  useEffect(() => {
    if (focus == null || !ref.current) return
    ref.current.open = true
    ref.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [focus])
  return <details className="dealer-card" ref={ref}>
    <summary>
      <div>
        <strong>{dealer.name}</strong>
        <small>{[dealer.email ?? 'no email (phone only)', dealer.phone].filter(Boolean).join(' · ')}{dealer.source?.kind === 'discovery' ? ' · found by search' : ''}</small>
      </div>
      <div className="dealer-card-meta">
        {dealer.flagged && <span className="badge warn">flagged</span>}
        {state && <span className="badge">{label(state)}</span>}
        <strong>{money(q?.price_total, currency)}</strong>
        <small>{q ? (q.firm ? 'firm written' : 'not firm') : 'no quote yet'}</small>
      </div>
    </summary>
    <ol className="timeline">{dealer.items.map(item => {
      if (item.kind === 'sent') return <li key={item.event_id} className="sent">
        <div className="tl-head"><span className={`chip ${item.channel}`}>{item.channel === 'call' ? 'Call brief' : 'Email'}</span><strong>{kindLabel(item.message_kind)}</strong><span className="small">round {item.round} · {time(item.ts)}</span></div>
        <p className="small">{item.channel === 'call' ? 'Fo runs the call in its own words, inside this brief.' : `To ${item.to}`}{item.offer != null && ` · asks ${money(item.offer, currency)} (your public target)`}</p>
        <pre>{item.body}</pre>
        <p className="checks small">
          {item.guard_passed != null && <span className={item.guard_passed ? 'ok' : 'bad'}>{item.guard_passed ? '✓ passed pre-send checks' : '✗ blocked by checks'}</span>}
          {item.audit && <span className={item.audit.matched && item.audit.guard_passed ? 'ok' : 'bad'}>{item.audit.matched ? '✓ sent copy matches' : `✗ sent copy differs${item.audit.diff ? `: ${item.audit.diff}` : ''}`}</span>}
          {item.numbers.filter(n => n.source !== 'job').map(n => <span key={n.value} className={n.source === 'unknown' ? 'bad' : 'ok'}>{money(n.value, currency)} from {n.source === 'ledger' ? `quote ${n.quote_id ?? ''}` : n.source}</span>)}
        </p>
      </li>
      if (item.kind === 'reply') {
        const quote = [...activity.quotes.values()].find(x => x.reply_id === item.reply_id)
        return <li key={item.event_id} className="reply">
          <div className="tl-head"><span className="chip reply">{item.source === 'call' ? 'Call summary' : 'Reply'}</span><strong>{quote?.price_total != null ? money(quote.price_total, currency) : quote?.declined ? 'Declined' : 'No price'}</strong><span className="small">{time(item.ts)}</span></div>
          {quote ? <QuoteEvidence quote={quote} reply={item} currency={currency} /> : <blockquote><p>{item.text}</p></blockquote>}
        </li>
      }
      if (item.kind === 'decision') return <li key={item.event_id} className="decision">
        <div className="tl-head"><span className="chip">Decision</span><strong>{label(item.action)}</strong><span className="small">{time(item.ts)}</span></div>
        <p className="small">{item.reason}</p>
      </li>
      return null
    })}</ol>
  </details>
}
