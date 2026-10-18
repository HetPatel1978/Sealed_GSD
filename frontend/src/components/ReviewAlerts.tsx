// Every job.needs_human event: things Sealed stopped on or wants you to look at. Blocking ones first.

import type { ReviewAlert } from '../activity'
import { label, time } from './format'

export default function ReviewAlerts({ alerts, names }: { alerts: ReviewAlert[]; names: Map<string, string> }) {
  if (alerts.length === 0) return null
  const sorted = [...alerts].sort((a, b) => Number(b.blocking) - Number(a.blocking) || b.event_id - a.event_id)
  return <section className="panel alerts" aria-label="Needs your review">
    <div className="section-title"><h3>Needs your review</h3><span className="small">{alerts.filter(a => a.blocking).length} blocking · {alerts.length} total</span></div>
    {sorted.map(a => <div key={a.event_id} className={`alert ${a.blocking ? 'blocking' : ''}`}>
      <div className="tl-head">
        <span className={`chip ${a.blocking ? 'bad' : 'warn'}`}>{a.blocking ? 'Paused' : 'Review'}</span>
        <strong>{a.vendor_id ? names.get(a.vendor_id) ?? a.vendor_id : 'Job'} · {label(a.reason_code)}</strong>
        <span className="small">{time(a.ts)}</span>
      </div>
      <p>{a.message}</p>
      {a.flags.map((f, i) => <p key={i} className="small">{label(f.flag)}{f.speaker && ` (${f.speaker})`}{f.excerpt && `: “${f.excerpt}”`}</p>)}
    </div>)}
  </section>
}
