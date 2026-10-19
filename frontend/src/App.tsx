import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { API_BASE, DATA_SOURCE } from './config'
import { eventTypes, request } from './api'
import type { Health, Job, Receipt, SealedEvent } from './api'
import mockEvents from '../../mock/events.json'
import DealerSearch from './DealerSearch'
import type { DealerSource, FoundDealer, Requirements } from './DealerSearch'
import { buildActivity } from './activity'
import type { Activity } from './activity'
import CeilingInput from './components/CeilingInput'
import CeilingMetric from './components/CeilingMetric'
import DealerActivity from './components/DealerActivity'
import EventFeed from './components/EventFeed'
import FeeComparison from './components/FeeComparison'
import QuoteEvidence from './components/QuoteEvidence'
import ReviewAlerts from './components/ReviewAlerts'
import Tabs from './components/Tabs'
import type { TabId } from './components/Tabs'
import TrustPanel from './components/TrustPanel'
import { label, money } from './components/format'
const deadlineDefault = () => new Date(Date.now() + 30 * 86400000).toISOString().slice(0, 10)
const TERMINAL = ['COMPLETED', 'CANCELLED', 'FAILED']
const WORKER_NOTES = ['mail_poll_failed', 'private_runtime_required', 'outreach_requires_reconciliation', 'needs_human', 'dispatch_authorization_required', 'fo_busy_research']

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
  const [reviewChecked, setReviewChecked] = useState(false)
  // Layout: which detail tab is open, which dealer card a link asked to open, and what the user has already seen.
  const [tab, setTab] = useState<TabId>('overview')
  const [focus, setFocus] = useState<{ id: string; n: number } | null>(null)
  const [seen, setSeen] = useState({ dealers: 0, activity: 0 })
  const selectedId = useRef<string | null>(null)
  const mock = DATA_SOURCE === 'mock'
  const activity = useMemo(() => buildActivity(mock ? mockEvents as SealedEvent[] : events), [mock, events])
  const names = useMemo(() => new Map([...activity.dealers.map(d => [d.id, d.name] as [string, string]), ...(job?.vendors ?? []).map(v => [v.id, v.name] as [string, string])]), [activity, job?.vendors])
  const proposalQuote = job?.proposal ? activity.quotes.get(job.proposal.quote_id) : undefined
  const lastEventId = events.at(-1)?.id ?? 0

  function selectJob(next: Job) {
    selectedId.current = next.id
    setEvents([]); setCeiling(null); setReceipt(null); setLiveConsent(false); setCallConsent(false); setRestoreValue('')
    setConnection('connecting'); setJob(next); setShowCreate(false)
    setTab('overview'); setFocus(null); setSeen({ dealers: next.last_event_id, activity: next.last_event_id })
  }

  function openTab(next: TabId, dealerId?: string) {
    // The tab being left and the tab being opened both count as seen up to now, so their "new" badges clear.
    setSeen(old => {
      const updated = { ...old }
      for (const t of [tab, next]) if (t === 'dealers' || t === 'activity') updated[t] = lastEventId
      return updated
    })
    setTab(next)
    if (dealerId) setFocus(old => ({ id: dealerId, n: (old?.n ?? 0) + 1 }))
    if (!dealerId) window.requestAnimationFrame(() => document.getElementById('job-tabs')?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
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

  // ---- The one thing to do now. Changes in place as the job moves, so nothing needs scrolling to. ----------
  function nextStep(j: Job): ReactNode {
    const dealerName = (id?: string) => (id && names.get(id)) || 'the dealer'
    // The background worker may advance the simulation first; a refusal is only an error if the job didn't move.
    const advance = (text: string) => j.mode === 'sim' && <button disabled={busy || !j.ceiling_set} onClick={() => void act(async () => {
      try { await request('/sim/advance', { job_id: j.id, steps: -1 }) }
      catch (problem) { if ((await request<Job>(`/jobs/${j.id}`)).state === j.state) throw problem }
    })}>{busy ? 'Advancing…' : text}</button>
    const replied = j.vendors.filter(v => v.latest_quote?.firm).length
    const blocking = [...activity.alerts].reverse().find(a => a.blocking)

    if (j.worker_status === 'call_in_progress' || j.worker_status === 'fo_busy_call') return <Step eyebrow="CALL RESULT PENDING" title={j.worker_status === 'fo_busy_call' ? 'Fo is finishing another call' : 'Waiting for Fo’s call result'}>
      <p className="muted">Sealed is checking the existing call. It will not dial again. A completed call still needs a verified written quote before an offer can be approved.</p>
      <div className="actions"><button className="secondary" onClick={() => openTab('dealers')}>View dealer history</button></div>
    </Step>

    if (!j.ceiling_set && !TERMINAL.includes(j.state)) return <Step eyebrow="ACTION NEEDED" title="Restore your private ceiling">
      <p>The backend restarted. Your ceiling was kept only in memory, so enter it again to continue.</p>
      <form className="restore" onSubmit={e => { e.preventDefault(); void act(async () => { await request(`/jobs/${j.id}/private-runtime`, { ceiling: Number(restoreValue) }); setCeiling(Number(restoreValue)); setRestoreValue('') }) }}>
        <CeilingInput value={restoreValue} onChange={setRestoreValue} target={j.target} currency={j.currency} label="Your private ceiling" />
        <button disabled={busy || !(Number(restoreValue) > j.target)}>Restore</button>
      </form>
    </Step>

    if (j.mode === 'live' && j.worker_status === 'dispatch_authorization_required') return <Step eyebrow="ACTION NEEDED" title="A scheduled call needs your permission">
      <p className="muted">The worker is waiting before contacting sellers by phone.</p>
      <div className="actions"><button disabled={busy} onClick={() => { if (window.confirm('Authorize negotiation calls to the selected sellers for this job?')) void act(() => request(`/jobs/${j.id}/allow-calls`, { allow_calls: true })) }}>Authorize calls and continue</button></div>
    </Step>

    switch (j.state) {
      case 'CREATED':
        if (j.start_requested) return <Step eyebrow="STARTING" title="Starting the negotiation…"><p className="muted">This page updates by itself.</p></Step>
        return <Step eyebrow="STEP 1 OF 3 · START" title="Ready when you are">
          <p>Sealed will ask {j.vendors.length} seller{j.vendors.length === 1 ? '' : 's'} for a written out-the-door total. {j.mode === 'sim' ? 'This is a simulation: replies are synthetic and nothing is sent.' : 'This contacts the sellers for real.'}</p>
          {j.mode === 'live' && <><label className="check"><input type="checkbox" checked={liveConsent} onChange={e => setLiveConsent(e.target.checked)} />I authorize sending these sellers’ replies to Anthropic for parsing and guarded Fo email outreach for this job.</label>{j.vendors.some(v => v.phone) && <label className="check"><input type="checkbox" checked={callConsent} onChange={e => setCallConsent(e.target.checked)} />I also authorize negotiation calls to the selected sellers.</label>}</>}
          <div className="actions"><button disabled={busy || !j.ceiling_set || (j.mode === 'live' && (!liveConsent || (j.vendors.some(v => !v.email) && !callConsent)))} onClick={() => void act(() => request(`/jobs/${j.id}/start`, { allow_model_requests: liveConsent, allow_dispatch: liveConsent, allow_calls: callConsent }))}>{busy ? 'Starting…' : j.mode === 'sim' ? 'Start simulation' : 'Start live negotiation'}</button></div>
        </Step>
      case 'RUNNING':
        return <Step eyebrow={`STEP 2 OF 3 · NEGOTIATING · ROUND ${j.round} OF 2`} title={replied === 0 ? 'Waiting for written quotes' : `${replied} of ${j.vendors.length} sellers have a firm written quote`}>
          <p className="muted">{j.mode === 'sim' ? 'Advance runs the simulated dealers until there is something for you to decide.' : 'Sealed is waiting for dealer replies. This page updates by itself.'}</p>
          <div className="actions">{advance('Advance to next decision')}<button className="secondary" onClick={() => openTab('dealers')}>See what was sent and received →</button></div>
        </Step>
      case 'AWAITING_APPROVAL': {
        const p = j.proposal
        if (!p) return null
        return <Step eyebrow="STEP 3 OF 3 · YOUR DECISION" title="Review the written offer" tone="approval">
          <p><strong>{dealerName(p.vendor_id)}</strong> · {money(p.price_total, p.currency)} out-the-door · Delivery {p.delivery_date ?? 'not stated'}</p>
          {p.flags.length > 0 && <p className="warning">Flagged: {p.flags.map(label).join(', ')}. See “Needs your review” in Overview.</p>}
          {proposalQuote && <QuoteEvidence quote={proposalQuote} reply={activity.replies.get(proposalQuote.reply_id)} currency={j.currency} />}
          <p className="muted">Approval authorizes a request for purchase paperwork at these exact terms. Completion requires the seller’s written confirmation.</p>
          <div className="actions">
            <button disabled={busy || !j.ceiling_set} onClick={() => void act(() => request(`/jobs/${j.id}/approve`, { proposal_id: p.proposal_id, vendor_id: p.vendor_id, price_total: p.price_total, currency: p.currency, delivery_date: p.delivery_date }))}>Approve {money(p.price_total, j.currency)}</button>
            <button className="secondary" disabled={busy} onClick={() => void act(() => request(`/jobs/${j.id}/reject`, { proposal_id: p.proposal_id }))}>Reject offer</button>
            <button type="button" className="text-button" onClick={() => openTab('dealers', p.vendor_id)}>Full history with {dealerName(p.vendor_id)} →</button>
          </div>
        </Step>
      }
      case 'BOOKING':
        return <Step eyebrow="APPROVED · FINISHING" title={`Waiting for written confirmation from ${dealerName(j.proposal?.vendor_id)}`}>
          <p className="muted">Sealed sent the proceed request at the approved terms. The job completes when the seller confirms in writing.</p>
          <div className="actions">{advance('Simulate written confirmation')}</div>
        </Step>
      case 'WAITING_AFTER_REJECT':
        return <Step eyebrow="PAUSED AFTER YOUR REJECTION" title="Waiting for a new written quote">
          <p className="muted">Sealed won’t show you the same offer again. It proposes again only if a seller sends a new firm written quote.{j.mode === 'sim' ? ' In this simulation no further quotes will arrive, so cancel and start a new negotiation to try approving.' : ''}</p>
          <div className="actions"><button className="secondary" onClick={() => openTab('dealers')}>Review dealer history →</button></div>
        </Step>
      case 'NEEDS_HUMAN': {
        // Only an unclear Fo send/call can be settled here; other pauses stay review-only.
        const review = j.review
        const call = review?.channel === 'call'
        const resolve = (outcome: 'sent' | 'retry' | 'skip') => review && void act(async () => {
          await request(`/jobs/${j.id}/resolve`, { message_id: review.message_id, outcome, checked: true })
          setReviewChecked(false)
        })
        return <Step eyebrow="PAUSED · NEEDS YOUR DECISION" title={blocking ? `${blocking.vendor_id ? dealerName(blocking.vendor_id) + ': ' : ''}${label(blocking.reason_code)}` : 'Sealed paused this negotiation'} tone="alert">
          <p>{blocking?.message ?? 'Review the latest events before proceeding.'}</p>
          {review && <>
            <p className="muted">{call
              ? `Open the Fo conversation for ${dealerName(review.vendor_id)} and check whether the call was actually placed.`
              : `Check Gmail (you are CC’d) and the Fo conversation for the email to ${dealerName(review.vendor_id)}.`} Then tell Sealed what happened.</p>
            <label className="check"><input type="checkbox" checked={reviewChecked} onChange={e => setReviewChecked(e.target.checked)} />I checked Fo{call ? '' : ' and Gmail'}, and Fo is not still working on this {call ? 'call' : 'email'} or scheduling a follow-up.</label>
            <div className="actions">
              <button disabled={busy || !reviewChecked} onClick={() => resolve('sent')}>{call ? 'The call happened, continue' : 'It was sent, continue'}</button>
              <button className="secondary" disabled={busy || !reviewChecked} onClick={() => resolve('retry')}>{call ? 'Not called, try again' : 'Not sent, send again'}</button>
              {review.can_skip && <button className="secondary" disabled={busy || !reviewChecked} onClick={() => resolve('skip')}>{call ? 'Not called, skip it' : 'Not sent, skip it'}</button>}
            </div>
          </>}
          <div className="actions"><button className="secondary" onClick={() => openTab('overview')}>See details →</button></div>
        </Step>
      }
      case 'COMPLETED':
        return <Step eyebrow={j.mode === 'sim' ? 'DONE · SIMULATION COMPLETE' : 'DONE · WRITTEN CONFIRMATION RECEIVED'} title={`${money(j.best_quote, j.currency)} with ${dealerName(j.proposal?.vendor_id ?? receipt?.vendor_name)}`} tone="done" status>
          {receipt && <p>Saved {money(receipt.savings_abs, receipt.currency)} from the first firm quote. Delivery {receipt.delivery_date}.</p>}
          <div className="actions"><button className="secondary" onClick={() => openTab('receipt')}>View receipt and fee comparison →</button></div>
        </Step>
      case 'CANCELLED':
        return <Step eyebrow="CANCELLED" title="This negotiation was cancelled" status><p className="muted">Queued outreach was stopped. Start a new negotiation to try again.</p></Step>
      default:
        return <Step eyebrow={label(j.state).toUpperCase()} title="Check the latest activity"><div className="actions"><button className="secondary" onClick={() => openTab('activity')}>Open activity →</button></div></Step>
    }
  }

  const freshReplies = activity.dealers.flatMap(d => d.items).filter(i => i.kind === 'reply' && i.event_id > seen.dealers).length
  const freshEvents = events.filter(e => e.id > seen.activity).length

  return (
    <main>
      <header className="topbar">
        <a className="wordmark" href="/">sealed<span>.</span></a>
        <div className="top-status"><span className={`dot ${mock || health ? 'on' : ''}`} />{mock ? 'Mock preview' : health ? `${health.mode === 'sim' ? 'Simulation' : 'Live'} backend · v${health.version}` : 'Connecting to backend'}</div>
      </header>
      {(mock || showCreate || !job) && <section className="intro">
        <div><p className="eyebrow">YOUR TERMS. YOUR APPROVAL.</p><h1>A better offer,<br />without the back-and-forth.</h1><p className="muted">Sealed compares written quotes and negotiates toward your target. You decide when to proceed.</p></div>
        <aside className="privacy"><span aria-hidden="true">◈</span><strong>Your ceiling stays private.</strong><p>Fo and sellers see your public target. Never tell Fo your ceiling; it remembers across chats.</p></aside>
      </section>}
      {error && <div className="notice error" role="alert">{error}<button className="text-button" onClick={() => setError('')} aria-label="Dismiss error">×</button></div>}
      {mock ? <MockPreview activity={activity} names={names} events={mockEvents as SealedEvent[]} /> : <>
        <nav className="job-nav" aria-label="Jobs"><div>{jobs.length > 0 && <select aria-label="Select a job" value={job?.id ?? ''} disabled={busy} onChange={e => { const next = jobs.find(item => item.id === e.target.value); if (next) { selectJob(next); setError('') } }}>{jobs.map(item => <option key={item.id} value={item.id}>{item.item} · {label(item.state)}</option>)}</select>}</div><button className="secondary" disabled={busy || !health} onClick={() => setShowCreate(value => !value)}>{showCreate && job ? 'Back to job' : '+ New negotiation'}</button></nav>
        {showCreate ? <Intake mode={health?.mode ?? 'sim'} disabled={busy || !health} onCreate={data => act(async () => {
          const next = await request<Job>('/jobs', data)
          setJobs(old => [next, ...old]); selectJob(next)
        })} /> : job && <>
          {/* Pinned: summary, the next step, and a one-line alert banner. */}
          <section className="panel job-overview compact"><div><p className="eyebrow">{job.mode === 'sim' ? 'SIMULATED NEGOTIATION · NO REAL OUTREACH' : 'LIVE NEGOTIATION'}</p><h2>{job.item}</h2><p className="muted">{job.spec.details}</p><p className="small">Delivery by {job.deadline} · Round {job.round} of 2 · <span className="connection">{connection}</span></p></div><span className={`badge ${job.state === 'COMPLETED' ? 'success' : ''}`}>{label(job.state)}</span></section>
          <div className="metrics"><Metric name="Public target" value={money(job.target, job.currency)} /><CeilingMetric ceiling={ceiling} currency={job.currency} /><Metric name="First firm quote" value={money(job.first_quote, job.currency)} /><Metric name={job.state === 'COMPLETED' ? 'Final total' : 'Proposed total'} value={money(job.best_quote, job.currency)} /></div>
          {nextStep(job)}
          {job.worker_status && WORKER_NOTES.includes(job.worker_status) && job.worker_status !== 'dispatch_authorization_required' && <div className="notice" role="status">Worker: {label(job.worker_status)}. Review the latest events before proceeding.</div>}
          {activity.alerts.length > 0 && <button type="button" className="alert-banner" onClick={() => openTab('overview')}>
            <span className="chip warn">Review</span>{activity.alerts.length} item{activity.alerts.length === 1 ? '' : 's'} need{activity.alerts.length === 1 ? 's' : ''} your review{activity.alerts.some(a => a.blocking) ? ' · negotiation paused' : ''}<span className="arrow">→</span>
          </button>}

          {/* Details, one tab at a time. Tabs only change when the user clicks. */}
          <div id="job-tabs"><Tabs active={tab} onSelect={id => openTab(id)} tabs={[
            { id: 'overview', label: 'Overview', alert: activity.alerts.length > 0 },
            { id: 'dealers', label: 'Dealers', count: job.vendors.length, fresh: freshReplies },
            { id: 'activity', label: 'Activity', count: job.last_event_id, fresh: freshEvents },
            { id: 'receipt', label: 'Receipt', hidden: !receipt && !activity.fee_comparison },
          ]} /></div>
          <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
            {tab === 'overview' && <>
              <ReviewAlerts alerts={activity.alerts} names={names} />
              <section className="panel"><div className="section-title"><h3>Seller comparison</h3><span className="small">Written terms only</span></div><div className="table-scroll"><table><thead><tr><th>Seller</th><th>Status</th><th>Latest total</th><th>Evidence</th><th></th></tr></thead><tbody>{job.vendors.map(vendor => <tr key={vendor.id}><td><strong>{vendor.name}</strong><small>{vendor.email ?? vendor.phone}</small></td><td>{label(vendor.state)}<small>{vendor.flags.join(', ')}</small></td><td>{money(vendor.latest_quote?.price_total, job.currency)}</td><td>{vendor.latest_quote ? vendor.latest_quote.firm ? 'Firm written quote' : 'Needs clarification' : 'Awaiting reply'}<small>{vendor.mode === 'sim' ? 'Synthetic evidence' : vendor.latest_quote?.verification === 'VERIFIED' ? 'Verified seller email' : ''}</small></td><td><button type="button" className="text-button" onClick={() => openTab('dealers', vendor.id)}>History →</button></td></tr>)}</tbody></table></div></section>
              <section className="panel"><div className="section-title"><h3>Trust checks</h3><span className="small">Counted by code on every message</span></div><TrustPanel trust={job.trust} /></section>
            </>}
            {tab === 'dealers' && <DealerActivity activity={activity} currency={job.currency} states={new Map(job.vendors.map(v => [v.id, v.state]))} focus={focus} />}
            {tab === 'activity' && <section className="panel"><div className="section-title"><h3>Negotiation activity</h3><span className="small">{job.mode} · {job.last_event_id} events · newest first</span></div><EventFeed events={events} names={names} /></section>}
            {tab === 'receipt' && <>
              {receipt && <section className="panel receipt"><p className="eyebrow">{receipt.mode === 'sim' ? 'SIMULATION COMPLETE' : 'WRITTEN CONFIRMATION RECEIVED'}</p><h2>{money(receipt.price_total, receipt.currency)} with {receipt.vendor_name}</h2><p>Saved {money(receipt.savings_abs, receipt.currency)} from the first firm quote. Delivery {receipt.delivery_date}.</p><ul>{receipt.evidence.map(item => <li key={item.ref}>{item.label}: <code>{item.ref}</code></li>)}</ul></section>}
              {activity.fee_comparison && <FeeComparison rows={activity.fee_comparison} names={names} currency={job.currency} />}
            </>}
          </div>
          {!TERMINAL.includes(job.state) && <div className="actions footer-actions"><button className="danger" disabled={busy} onClick={() => { if (window.confirm('Cancel this negotiation and stop queued outreach?')) void act(() => request(`/jobs/${job.id}/cancel`, {})) }}>Cancel negotiation</button></div>}
        </>}
      </>}
      <footer>Sealed · Your ceiling stays in backend memory. Booking always requires your approval.</footer>
    </main>
  )
}

function Step({ eyebrow, title, tone, children, status }: { eyebrow: string; title: string; tone?: 'approval' | 'alert' | 'done'; children: ReactNode; status?: boolean }) {
  return <section className={`panel next-step ${tone ?? ''}`} aria-live="polite"><p className="eyebrow">{status ? 'STATUS' : 'NEXT STEP'} · {eyebrow}</p><h2>{title}</h2>{children}</section>
}

function Metric({ name, value }: { name: string; value: string }) {
  return <div className="metric"><span>{name}</span><strong>{value}</strong></div>
}

function MockPreview({ activity, names, events }: { activity: Activity; names: Map<string, string>; events: SealedEvent[] }) {
  const [tab, setTab] = useState<TabId>('overview')
  const j = activity.job
  const currency = j?.currency ?? 'USD'
  const completed = events.find(e => e.type === 'job.completed')?.data as { first_quote?: number; best_quote?: number; vendor_id?: string } | undefined
  return <>
    <section className="panel job-overview compact"><div><p className="eyebrow">MOCK PREVIEW · RECORDED SIMULATION · NO OUTREACH</p><h2>{j?.item ?? 'Recorded negotiation'}</h2><p className="muted">{j?.details}</p><p className="small">Delivery by {j?.deadline} · ZIP {j?.zip} · Switch VITE_DATA_SOURCE to live to create jobs using your backend.</p></div><span className="badge">recorded</span></section>
    <div className="metrics"><Metric name="Public target" value={money(j?.target, currency)} /><Metric name="Your private ceiling" value="not in the event log" /><Metric name="First firm quote" value={money(completed?.first_quote, currency)} /><Metric name="Approved total" value={money(completed?.best_quote, currency)} /></div>
    <section className="panel next-step done"><p className="eyebrow">RECORDED RUN · COMPLETED</p><h2>{money(completed?.best_quote, currency)} with {names.get(completed?.vendor_id ?? '') ?? '—'}</h2><p className="muted">Open the tabs below to see how it got there.</p></section>
    <Tabs active={tab} onSelect={setTab} tabs={[
      { id: 'overview', label: 'Overview', alert: activity.alerts.length > 0 },
      { id: 'dealers', label: 'Dealers', count: activity.dealers.length },
      { id: 'activity', label: 'Activity', count: events.length },
      { id: 'receipt', label: 'Fee comparison', hidden: !activity.fee_comparison },
    ]} />
    {tab === 'overview' && <><ReviewAlerts alerts={activity.alerts} names={names} /><section className="panel"><div className="section-title"><h3>Trust checks</h3></div><TrustPanel trust={activity.trust} /></section></>}
    {tab === 'dealers' && <DealerActivity activity={activity} currency={currency} />}
    {tab === 'activity' && <section className="panel"><div className="section-title"><h3>Negotiation activity</h3><span className="small">{events.length} events · newest first</span></div><EventFeed events={events} names={names} /></section>}
    {tab === 'receipt' && activity.fee_comparison && <FeeComparison rows={activity.fee_comparison} names={names} currency={currency} />}
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
  const [target, setTarget] = useState('90')
  const [currency, setCurrency] = useState('USD')
  const [ceiling, setCeiling] = useState('')   // in memory only; cleared after the job is created
  const ceilingOk = Number(ceiling) > Number(target)
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
    setCeiling('')
  }
  return <form className="panel intake" onSubmit={e => { void submit(e) }}><p className="eyebrow">NEW {mode === 'sim' ? 'SIMULATION' : 'LIVE JOB'}</p><h2>What are we negotiating?</h2><p className="muted">One item, up to five sellers, two negotiation rounds.</p><fieldset disabled={disabled}><div className="form-grid"><label>Item<input name="item" value={publicInput.item} onChange={e => changePublic('item', e.target.value)} maxLength={200} required /></label><label>Your first name<input name="buyer" defaultValue="Buyer" maxLength={80} required /></label><label className="full">Exact specification<textarea name="details" value={publicInput.details} onChange={e => changePublic('details', e.target.value)} maxLength={2000} required /></label><label>ZIP code<input name="zip" inputMode="numeric" pattern="[0-9]{5}" value={publicInput.zip} onChange={e => changePublic('zip', e.target.value)} /></label><label>Delivery deadline<input type="date" name="deadline" defaultValue={defaultDeadline} min={today} required /></label><label>Public target<input type="number" name="target" value={target} onChange={e => setTarget(e.target.value)} min="0.01" max="1000000000" step="0.01" required /><small>Sealed openly asks sellers for this price.</small></label><CeilingInput name="ceiling" value={ceiling} onChange={setCeiling} target={Number(target)} currency={currency} /><label>Currency<select name="currency" value={currency} onChange={e => setCurrency(e.target.value)}><option>USD</option><option>CAD</option><option>EUR</option><option>GBP</option></select></label></div><DealerSearch key={JSON.stringify(publicInput)} requirements={requirements} mode={mode} selected={vendors.filter(v => v.source).map(v => `${v.source!.discovery_id}:${v.source!.candidate_id}`)} count={vendors.filter(v => !placeholder(v)).length} onSelect={chooseDealer} /><div className="section-title"><h3>Sellers</h3><span className="small">{vendors.length}/5 · manual or selected</span></div>{vendors.map((vendor, index) => <div className="vendor-inputs" key={index}><label>Seller name<input aria-label={`Seller ${index + 1} name`} value={vendor.name} readOnly={!!vendor.source} maxLength={120} required onChange={e => setVendors(old => old.map((item, i) => i === index ? { ...item, name: e.target.value } : item))} /></label><label>Email<input type="email" aria-label={`Seller ${index + 1} email`} value={vendor.email} readOnly={!!vendor.source} required={mode === 'sim' || !vendor.phone} onChange={e => setVendors(old => old.map((item, i) => i === index ? { ...item, email: e.target.value } : item))} /></label><label>Phone (optional)<input type="tel" value={vendor.phone} readOnly={!!vendor.source} onChange={e => setVendors(old => old.map((item, i) => i === index ? { ...item, phone: e.target.value } : item))} /></label>{(vendors.length > 1 || vendor.source) && <button type="button" className="text-button" aria-label={`Remove seller ${index + 1}`} onClick={() => setVendors(old => old.filter((_, i) => i !== index))}>Remove</button>}</div>)}<div className="actions"><button className="secondary" type="button" disabled={vendors.length >= 5} onClick={() => setVendors(old => [...old, { name: '', email: '', phone: '' }])}>+ Add seller</button><button type="submit" disabled={vendors.length === 0 || !ceilingOk}>{disabled ? 'Please wait…' : 'Create negotiation'}</button></div><p className="small">Creating a job contacts no one. Review the details before starting.</p></fieldset></form>
}
