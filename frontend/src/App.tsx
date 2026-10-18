import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { API_BASE, DATA_SOURCE } from './config'
import { eventTypes, request } from './api'
import type { Health, Job, Receipt, SealedEvent } from './api'
import mockEvents from '../../mock/events.json'
import DealerSearch from './DealerSearch'
import type { DealerSource, FoundDealer, Requirements } from './DealerSearch'
import { buildActivity } from './activity'
import CeilingMetric from './components/CeilingMetric'
import DealerActivity from './components/DealerActivity'
import EventFeed from './components/EventFeed'
import FeeComparison from './components/FeeComparison'
import QuoteEvidence from './components/QuoteEvidence'
import ReviewAlerts from './components/ReviewAlerts'
import TrustPanel from './components/TrustPanel'
import { label, money } from './components/format'
const deadlineDefault = () => new Date(Date.now() + 30 * 86400000).toISOString().slice(0, 10)

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [job, setJob] = useState<Job | null>(null)
  const [events, setEvents] = useState<SealedEvent[]>([])
  const [ceiling, setCeiling] = useState<number | null>(null)
  const [receipt, setReceipt] = useState<Receipt | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [connection, setConnection] = useState('connecting')
  const [showCreate, setShowCreate] = useState(true)
  const [liveConsent, setLiveConsent] = useState(false)
  const [callConsent, setCallConsent] = useState(false)
  const [restoreValue, setRestoreValue] = useState('')
  const selectedId = useRef<string | null>(null)
  const mock = DATA_SOURCE === 'mock'
  const activity = useMemo(() => buildActivity(mock ? mockEvents as SealedEvent[] : events), [mock, events])
  const names = useMemo(() => new Map([...activity.dealers.map(d => [d.id, d.name] as [string, string]), ...(job?.vendors ?? []).map(v => [v.id, v.name] as [string, string])]), [activity, job?.vendors])
  const proposalQuote = job?.proposal ? activity.quotes.get(job.proposal.quote_id) : undefined

  function selectJob(next: Job) {
    selectedId.current = next.id
    setEvents([]); setCeiling(null); setReceipt(null); setLiveConsent(false); setCallConsent(false); setRestoreValue('')
    setConnection('connecting'); setJob(next); setShowCreate(false)
  }

  useEffect(() => {
    if (mock) return
    let active = true
    Promise.all([request<Health>('/health'), request<Job[]>('/jobs')]).then(([nextHealth, nextJobs]) => {
      if (!active) return
      setHealth(nextHealth); setJobs(nextJobs)
      if (nextJobs[0]) selectJob(nextJobs[0])
    }).catch(() => { if (active) setError(`Backend unavailable. Start python -m app.run in backend, then refresh.`) })
    return () => { active = false }
  }, [mock])

  const jobId = job?.id
  useEffect(() => {
    if (!jobId || mock) return
    let active = true
    let refreshing = false
    let refreshAgain = false
    const refresh = async () => {
      if (refreshing) { refreshAgain = true; return }
      refreshing = true
      try {
        do {
          refreshAgain = false
          const next = await request<Job>(`/jobs/${jobId}`)
          if (active) { setJob(next); setJobs(old => old.map(item => item.id === jobId ? next : item)) }
        } while (refreshAgain && active)
      } catch { if (active) setConnection('reconnecting') }
      finally { refreshing = false }
    }
    request<{ ceiling: number | null }>(`/jobs/${jobId}/ceiling`).then(data => { if (active) setCeiling(data.ceiling) }).catch(() => { if (active) setError('Could not load your private ceiling.') })
    const stream = new EventSource(`${API_BASE}/jobs/${jobId}/events`)
    stream.onopen = () => { if (active) { setConnection('connected'); void refresh() } }
    stream.onerror = () => { if (active) setConnection('reconnecting') }
    for (const type of eventTypes) stream.addEventListener(type, message => {
      if (!active) return
      try {
        const event = JSON.parse((message as MessageEvent<string>).data) as SealedEvent
        if (event.job_id !== jobId) return
        setEvents(old => old.some(item => item.id === event.id) ? old : [...old, event].sort((a, b) => a.id - b.id).slice(-500))
        void refresh()
      } catch { setConnection('event review required') }
    })
    const poll = window.setInterval(() => { void refresh() }, 10000)
    return () => { active = false; window.clearInterval(poll); stream.close() }
  }, [jobId, mock])

  useEffect(() => {
    if (!jobId || job?.state !== 'COMPLETED' || mock) return
    let active = true
    request<Receipt>(`/jobs/${jobId}/receipt`).then(data => { if (active) setReceipt(data) }).catch(() => { if (active) setError('Could not load the final receipt.') })
    return () => { active = false }
  }, [jobId, job?.state, mock])

  async function act(operation: () => Promise<unknown>) {
    if (busy) return
    setBusy(true); setError('')
    try { await operation() }
    catch (problem) { setError(problem instanceof Error ? problem.message : 'The action could not be completed.') }
    finally {
      if (selectedId.current) {
        const id = selectedId.current
        try {
          const next = await request<Job>(`/jobs/${id}`)
          if (selectedId.current === id) { setJob(next); setJobs(old => old.map(item => item.id === id ? next : item)) }
        } catch { /* Keep the action's original message; SSE reconnects independently. */ }
      }
      setBusy(false)
    }
  }

  return (
    <main>
      <header className="topbar">
        <a className="wordmark" href="/">sealed<span>.</span></a>
        <div className="top-status"><span className={`dot ${mock || health ? 'on' : ''}`} />{mock ? 'Mock preview' : health ? `${health.mode === 'sim' ? 'Simulation' : 'Live'} backend · v${health.version}` : 'Connecting to backend'}</div>
      </header>
      <section className="intro">
        <div><p className="eyebrow">YOUR TERMS. YOUR APPROVAL.</p><h1>A better offer,<br />without the back-and-forth.</h1><p className="muted">Sealed compares written quotes and negotiates toward your target. You decide when to proceed.</p></div>
        <aside className="privacy"><span aria-hidden="true">◈</span><strong>Your ceiling stays private.</strong><p>Fo and sellers see your public target. Never tell Fo your ceiling; it remembers across chats.</p></aside>
      </section>
      {error && <div className="notice error" role="alert">{error}<button className="text-button" onClick={() => setError('')} aria-label="Dismiss error">×</button></div>}
      {mock ? <MockPreview activity={activity} names={names} events={mockEvents as SealedEvent[]} /> : <>
        <nav className="job-nav" aria-label="Jobs"><div>{jobs.length > 0 && <select aria-label="Select a job" value={job?.id ?? ''} disabled={busy} onChange={e => { const next = jobs.find(item => item.id === e.target.value); if (next) { selectJob(next); setError('') } }}>{jobs.map(item => <option key={item.id} value={item.id}>{item.item} · {label(item.state)}</option>)}</select>}</div><button className="secondary" disabled={busy || !health} onClick={() => setShowCreate(value => !value)}>{showCreate && job ? 'Back to job' : '+ New negotiation'}</button></nav>
        {showCreate ? <Intake mode={health?.mode ?? 'sim'} disabled={busy || !health} onCreate={data => act(async () => {
          const next = await request<Job>('/jobs', data)
          setJobs(old => [next, ...old]); selectJob(next)
        })} /> : job && <>
          <section className="panel job-overview"><div><p className="eyebrow">{job.mode === 'sim' ? 'SIMULATED NEGOTIATION · NO REAL OUTREACH' : 'LIVE NEGOTIATION'}</p><h2>{job.item}</h2><p className="muted">{job.spec.details}</p><p className="small">Delivery by {job.deadline} · Round {job.round} of 2 · <span className="connection">{connection}</span></p></div><span className={`badge ${job.state === 'COMPLETED' ? 'success' : ''}`}>{label(job.state)}</span></section>
          <div className="metrics"><Metric name="Public target" value={money(job.target, job.currency)} /><CeilingMetric ceiling={ceiling} currency={job.currency} /><Metric name="First firm quote" value={money(job.first_quote, job.currency)} /><Metric name="Proposed total" value={money(job.best_quote, job.currency)} /></div>
          {!job.ceiling_set && !['COMPLETED', 'CANCELLED'].includes(job.state) && <form className="notice restore" onSubmit={e => { e.preventDefault(); void act(async () => { await request(`/jobs/${job.id}/private-runtime`, { ceiling: Number(restoreValue) }); setCeiling(Number(restoreValue)); setRestoreValue('') }) }}><div><strong>Restore your private ceiling</strong><p>The backend restarted. Your ceiling was kept in memory and needs to be entered again.</p></div><input aria-label="Restore private ceiling" type="password" inputMode="decimal" autoComplete="off" value={restoreValue} onChange={e => setRestoreValue(e.target.value)} required /><button disabled={busy}>Restore</button></form>}
          {job.worker_status && ['mail_poll_failed', 'private_runtime_required', 'outreach_requires_reconciliation', 'needs_human', 'dispatch_authorization_required', 'fo_busy_research'].includes(job.worker_status) && <div className="notice" role="status">Worker: {label(job.worker_status)}. Review the latest events before proceeding.</div>}
          <ReviewAlerts alerts={activity.alerts} names={names} />
          {job.state === 'CREATED' && !job.start_requested && <section className="panel start-panel"><h3>Ready when you are</h3><p>Starting contacts the selected sellers in live mode. Simulation uses synthetic replies.</p>{job.mode === 'live' && <><label className="check"><input type="checkbox" checked={liveConsent} onChange={e => setLiveConsent(e.target.checked)} />I authorize sending these sellers’ replies to Anthropic for parsing and guarded Fo email outreach for this job.</label>{job.vendors.some(v => v.phone) && <label className="check"><input type="checkbox" checked={callConsent} onChange={e => setCallConsent(e.target.checked)} />I also authorize negotiation calls to the selected sellers.</label>}</>}<button disabled={busy || !job.ceiling_set || (job.mode === 'live' && (!liveConsent || (job.vendors.some(v => !v.email) && !callConsent)))} onClick={() => void act(() => request(`/jobs/${job.id}/start`, { allow_model_requests: liveConsent, allow_dispatch: liveConsent, allow_calls: callConsent }))}>{busy ? 'Starting…' : job.mode === 'sim' ? 'Start simulation' : 'Start live negotiation'}</button></section>}
          {job.mode === 'live' && job.worker_status === 'dispatch_authorization_required' && <section className="panel"><h3>A scheduled call needs your permission</h3><p className="muted">The worker is waiting before contacting sellers by phone.</p><button disabled={busy} onClick={() => { if (window.confirm('Authorize negotiation calls to the selected sellers for this job?')) void act(() => request(`/jobs/${job.id}/allow-calls`, { allow_calls: true })) }}>Authorize calls and continue</button></section>}
          {job.state === 'AWAITING_APPROVAL' && job.proposal && <section className="panel approval"><p className="eyebrow">YOUR DECISION</p><h2>Review the written offer</h2><p><strong>{job.vendors.find(v => v.id === job.proposal?.vendor_id)?.name}</strong> · {money(job.proposal.price_total, job.proposal.currency)} out-the-door · Delivery {job.proposal.delivery_date ?? 'not stated'}</p>{job.proposal.flags.length > 0 && <p className="warning">{job.proposal.flags.join(', ')}</p>}{proposalQuote && <QuoteEvidence quote={proposalQuote} reply={activity.replies.get(proposalQuote.reply_id)} currency={job.currency} />}<p className="muted">Approval authorizes a request for purchase paperwork at these exact terms. Completion requires the seller’s written confirmation.</p><div className="actions"><button disabled={busy || !job.ceiling_set} onClick={() => { const p = job.proposal!; void act(() => request(`/jobs/${job.id}/approve`, { proposal_id: p.proposal_id, vendor_id: p.vendor_id, price_total: p.price_total, currency: p.currency, delivery_date: p.delivery_date })) }}>Approve {money(job.proposal.price_total, job.currency)}</button><button className="secondary" disabled={busy} onClick={() => void act(() => request(`/jobs/${job.id}/reject`, { proposal_id: job.proposal!.proposal_id }))}>Reject offer</button></div></section>}
          <section className="panel"><div className="section-title"><h3>Seller comparison</h3><span className="small">Written terms only</span></div><div className="table-scroll"><table><thead><tr><th>Seller</th><th>Status</th><th>Latest total</th><th>Evidence</th></tr></thead><tbody>{job.vendors.map(vendor => <tr key={vendor.id}><td><strong>{vendor.name}</strong><small>{vendor.email ?? vendor.phone}</small></td><td>{label(vendor.state)}<small>{vendor.flags.join(', ')}</small></td><td>{money(vendor.latest_quote?.price_total, job.currency)}</td><td>{vendor.latest_quote ? vendor.latest_quote.firm ? 'Firm written quote' : 'Needs clarification' : 'Awaiting reply'}<small>{vendor.mode === 'sim' ? 'Synthetic evidence' : vendor.latest_quote?.verification === 'VERIFIED' ? 'Verified seller email' : ''}</small></td></tr>)}</tbody></table></div></section>
          <DealerActivity activity={activity} currency={job.currency} states={new Map(job.vendors.map(v => [v.id, v.state]))} />
          {receipt && <section className="panel receipt"><p className="eyebrow">{receipt.mode === 'sim' ? 'SIMULATION COMPLETE' : 'WRITTEN CONFIRMATION RECEIVED'}</p><h2>{money(receipt.price_total, receipt.currency)} with {receipt.vendor_name}</h2><p>Saved {money(receipt.savings_abs, receipt.currency)} from the first firm quote. Delivery {receipt.delivery_date}.</p><ul>{receipt.evidence.map(item => <li key={item.ref}>{item.label}: <code>{item.ref}</code></li>)}</ul></section>}
          {activity.fee_comparison && <FeeComparison rows={activity.fee_comparison} names={names} currency={job.currency} />}
          <section className="panel"><div className="section-title"><h3>Negotiation activity</h3><span className="small">{job.mode} · {job.last_event_id} events</span></div><TrustPanel trust={job.trust} /><EventFeed events={events} names={names} /></section>
          <div className="actions footer-actions">{job.mode === 'sim' && ['RUNNING', 'BOOKING'].includes(job.state) && <button disabled={busy || !job.ceiling_set} onClick={() => void act(() => request('/sim/advance', { job_id: job.id, steps: -1 }))}>{busy ? 'Advancing…' : job.state === 'BOOKING' ? 'Simulate written confirmation' : 'Advance to next decision'}</button>}{!['COMPLETED', 'CANCELLED'].includes(job.state) && <button className="danger" disabled={busy} onClick={() => { if (window.confirm('Cancel this negotiation and stop queued outreach?')) void act(() => request(`/jobs/${job.id}/cancel`, {})) }}>Cancel negotiation</button>}</div>
        </>}
      </>}
      <footer>Sealed · Your ceiling stays in backend memory. Booking always requires your approval.</footer>
    </main>
  )
}

function Metric({ name, value }: { name: string; value: string }) {
  return <div className="metric"><span>{name}</span><strong>{value}</strong></div>
}

function MockPreview({ activity, names, events }: { activity: ReturnType<typeof buildActivity>; names: Map<string, string>; events: SealedEvent[] }) {
  const j = activity.job
  const currency = j?.currency ?? 'USD'
  const completed = events.find(e => e.type === 'job.completed')?.data as { first_quote?: number; best_quote?: number } | undefined
  return <>
    <section className="panel job-overview"><div><p className="eyebrow">MOCK PREVIEW · RECORDED SIMULATION · NO OUTREACH</p><h2>{j?.item ?? 'Recorded negotiation'}</h2><p className="muted">{j?.details}</p><p className="small">Delivery by {j?.deadline} · ZIP {j?.zip} · Switch VITE_DATA_SOURCE to live to create jobs using your backend.</p></div><span className="badge">recorded</span></section>
    <div className="metrics"><Metric name="Public target" value={money(j?.target, currency)} /><Metric name="Your private ceiling" value="not in the event log" /><Metric name="First firm quote" value={money(completed?.first_quote, currency)} /><Metric name="Approved total" value={money(completed?.best_quote, currency)} /></div>
    <ReviewAlerts alerts={activity.alerts} names={names} />
    <DealerActivity activity={activity} currency={currency} />
    {activity.fee_comparison && <FeeComparison rows={activity.fee_comparison} names={names} currency={currency} />}
    <section className="panel"><div className="section-title"><h3>Negotiation activity</h3><span className="small">{events.length} events</span></div><TrustPanel trust={activity.trust} /><EventFeed events={events} names={names} /></section>
  </>
}

function Intake({ mode, disabled, onCreate }: { mode: 'sim' | 'live'; disabled: boolean; onCreate: (body: unknown) => Promise<void> }) {
  type IntakeVendor = { name: string; email: string; phone: string; source?: DealerSource }
  const [vendors, setVendors] = useState<IntakeVendor[]>([{ name: 'Sample seller', email: 'seller@example.org', phone: '' }])
  const [publicInput, setPublicInput] = useState({ item: 'Sample item', details: 'Exact sample specification', zip: '19104' })
  const requirements: Requirements = { item: publicInput.item, spec: { quantity: 1, details: publicInput.details }, zip: publicInput.zip }
  const placeholder = (vendor: IntakeVendor) => !vendor.source && ((!vendor.name && !vendor.email && !vendor.phone) || (vendor.name === 'Sample seller' && vendor.email === 'seller@example.org' && !vendor.phone))
  function changePublic(field: keyof typeof publicInput, value: string) {
    setPublicInput(old => ({ ...old, [field]: value }))
    setVendors(old => old.filter(vendor => !vendor.source))
  }
  function chooseDealer(dealer: FoundDealer, checked: boolean) {
    setVendors(old => {
      const current = old.filter(vendor => !placeholder(vendor))
      if (!checked) return current.filter(vendor => !(vendor.source?.discovery_id === dealer.source.discovery_id && vendor.source?.candidate_id === dealer.source.candidate_id))
      if (current.length >= 5) return old
      return [...current, { name: dealer.name, email: dealer.email ?? '', phone: dealer.phone ?? '', source: dealer.source }]
    })
  }
  const [today] = useState(() => new Date().toISOString().slice(0, 10))
  const [defaultDeadline] = useState(deadlineDefault)
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    await onCreate({ item: data.get('item'), spec: { quantity: 1, details: data.get('details') },
      zip: data.get('zip'), buyer: data.get('buyer'), deadline: data.get('deadline'), currency: data.get('currency'),
      target: Number(data.get('target')), ceiling: Number(data.get('ceiling')), max_rounds: 2,
      vendors: vendors.map(vendor => ({ name: vendor.name, email: vendor.email || null, phone: vendor.phone || null, mode, source: vendor.source ?? { kind: 'manual' } })),
    })
    // Discard the private input after submission. It is never persisted in the browser.
    const privateInput = form.elements.namedItem('ceiling') as HTMLInputElement | null
    if (privateInput) privateInput.value = ''
  }
  return <form className="panel intake" onSubmit={e => { void submit(e) }}><p className="eyebrow">NEW {mode === 'sim' ? 'SIMULATION' : 'LIVE JOB'}</p><h2>What are we negotiating?</h2><p className="muted">One item, up to five sellers, two negotiation rounds.</p><fieldset disabled={disabled}><div className="form-grid"><label>Item<input name="item" value={publicInput.item} onChange={e => changePublic('item', e.target.value)} maxLength={200} required /></label><label>Your first name<input name="buyer" defaultValue="Buyer" maxLength={80} required /></label><label className="full">Exact specification<textarea name="details" value={publicInput.details} onChange={e => changePublic('details', e.target.value)} maxLength={2000} required /></label><label>ZIP code<input name="zip" inputMode="numeric" pattern="[0-9]{5}" value={publicInput.zip} onChange={e => changePublic('zip', e.target.value)} /></label><label>Delivery deadline<input type="date" name="deadline" defaultValue={defaultDeadline} min={today} required /></label><label>Public target<input type="number" name="target" defaultValue={90} min="0.01" max="1000000000" step="0.01" required /></label><label>Private ceiling<input type="password" name="ceiling" inputMode="decimal" autoComplete="off" placeholder="Only you can see this" required /></label><label>Currency<select name="currency" defaultValue="USD"><option>USD</option><option>CAD</option><option>EUR</option><option>GBP</option></select></label></div><DealerSearch key={JSON.stringify(publicInput)} requirements={requirements} mode={mode} selected={vendors.filter(v => v.source).map(v => `${v.source!.discovery_id}:${v.source!.candidate_id}`)} count={vendors.filter(v => !placeholder(v)).length} onSelect={chooseDealer} /><div className="section-title"><h3>Sellers</h3><span className="small">{vendors.length}/5 · manual or selected</span></div>{vendors.map((vendor, index) => <div className="vendor-inputs" key={index}><label>Seller name<input aria-label={`Seller ${index + 1} name`} value={vendor.name} readOnly={!!vendor.source} maxLength={120} required onChange={e => setVendors(old => old.map((item, i) => i === index ? { ...item, name: e.target.value } : item))} /></label><label>Email<input type="email" aria-label={`Seller ${index + 1} email`} value={vendor.email} readOnly={!!vendor.source} required={mode === 'sim' || !vendor.phone} onChange={e => setVendors(old => old.map((item, i) => i === index ? { ...item, email: e.target.value } : item))} /></label><label>Phone (optional)<input type="tel" value={vendor.phone} readOnly={!!vendor.source} onChange={e => setVendors(old => old.map((item, i) => i === index ? { ...item, phone: e.target.value } : item))} /></label>{(vendors.length > 1 || vendor.source) && <button type="button" className="text-button" aria-label={`Remove seller ${index + 1}`} onClick={() => setVendors(old => old.filter((_, i) => i !== index))}>Remove</button>}</div>)}<div className="actions"><button className="secondary" type="button" disabled={vendors.length >= 5} onClick={() => setVendors(old => [...old, { name: '', email: '', phone: '' }])}>+ Add seller</button><button type="submit" disabled={vendors.length === 0}>{disabled ? 'Please wait…' : 'Create negotiation'}</button></div><p className="small">Creating a job contacts no one. Review the details before starting.</p></fieldset></form>
}
