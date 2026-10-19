// Turns the event stream (docs/api.md "Events") into what the dashboard shows per dealer.
// Pure functions only: the same code runs on live SSE events and on mock/events.json.

import type { SealedEvent } from './api'

export type Channel = 'email' | 'call'
export type NumberSource = { value: number; source: 'ledger' | 'target' | 'job' | 'unknown'; quote_id?: string }
export type Breakdown = {
  vehicle_price: number | null
  dealer_fees: { label: string; amount: number }[]
  add_ons: { label: string; amount: number }[]
  taxes: number | null
  title_registration: number | null
  other: { label: string; amount: number }[]
  out_the_door_total: number | null
  valid_until: string | null
}

export type SentMessage = {
  kind: 'sent'; event_id: number; ts: string; message_id: string; message_kind: string; round: number
  channel: Channel; to: string; subject: string | null; body: string; offer: number | null
  numbers: NumberSource[]; guard_passed: boolean | null; audit: { matched: boolean; guard_passed: boolean; diff: string | null } | null
}
export type Reply = {
  kind: 'reply'; event_id: number; ts: string; reply_id: string; source: 'vendor_email' | 'fo_relay' | 'call'
  from: string; text: string; in_reply_to: string | null
}
export type Quote = {
  kind: 'quote'; event_id: number; ts: string; quote_id: string; reply_id: string; source: Channel
  price_total: number | null; firm: boolean; declined: boolean; suspicious: boolean; meets_spec: boolean
  delivery_date: string | null; hedge_words: string[]; breakdown: Breakdown | null; breakdown_flags: string[]
  verification: 'VERIFIED' | 'UNVERIFIED' | null; verification_reason: string | null
}
export type Decision = { kind: 'decision'; event_id: number; ts: string; action: string; reason: string; offer: number | null; channel: Channel }
export type ActivityItem = SentMessage | Reply | Quote | Decision

export type DealerActivity = {
  id: string; name: string; email: string | null; phone: string | null
  source: { kind: string; urls?: string[] } | null
  items: ActivityItem[]; latest_quote: Quote | null; flagged: boolean
}

export type ReviewAlert = {
  event_id: number; ts: string; vendor_id: string | null; reason_code: string; blocking: boolean; message: string
  flags: { flag: string; speaker?: string; excerpt?: string }[]
  resolution?: string   // set when the buyer settled this pause from the dashboard
}

export type FeeRow = {
  vendor_id: string; quote_id: string; out_the_door_total: number; vehicle_price: number | null
  dealer_fees_total: number; add_ons_total: number; taxes: number | null; title_registration: number | null
  other_total: number; dealer_fees: { label: string; amount: number }[]; add_ons: { label: string; amount: number }[]
  breakdown_flags: string[]; flags: string[]
}

export type Activity = {
  dealers: DealerActivity[]
  alerts: ReviewAlert[]
  quotes: Map<string, Quote>
  replies: Map<string, Reply>
  fee_comparison: FeeRow[] | null
  trust: Record<string, number> | null
  job: { item: string; details: string; zip: string | null; deadline: string; currency: string; target: number; mode: string } | null
}

type Data = Record<string, unknown>
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null)
const num = (v: unknown): number | null => (typeof v === 'number' ? v : null)

export function buildActivity(events: SealedEvent[]): Activity {
  const dealers = new Map<string, DealerActivity>()
  const sent = new Map<string, SentMessage>()
  const guardResults = new Map<string, boolean>()   // guard.checked arrives before message.sent
  const quotes = new Map<string, Quote>()
  const replies = new Map<string, Reply>()
  const alerts: ReviewAlert[] = []
  let fee_comparison: FeeRow[] | null = null
  let trust: Record<string, number> | null = null
  let job: Activity['job'] = null

  const dealer = (id: string): DealerActivity => {
    let d = dealers.get(id)
    if (!d) {
      d = { id, name: id, email: null, phone: null, source: null, items: [], latest_quote: null, flagged: false }
      dealers.set(id, d)
    }
    return d
  }

  for (const e of [...events].sort((a, b) => a.id - b.id)) {
    const d = e.data as Data
    const vid = e.vendor_id
    switch (e.type) {
      case 'job.created': {
        job = { item: String(d.item), details: String((d.spec as Data)?.details ?? ''), zip: str(d.zip),
          deadline: String(d.deadline), currency: String(d.currency ?? 'USD'), target: Number(d.target), mode: e.mode }
        for (const v of (d.vendors as Data[]) ?? []) {
          const x = dealer(String(v.id))
          x.name = String(v.name); x.email = str(v.email); x.phone = str(v.phone)
          x.source = (v.source as DealerActivity['source']) ?? null
        }
        break
      }
      case 'message.sent': {
        if (!vid) break
        const m: SentMessage = {
          kind: 'sent', event_id: e.id, ts: e.ts, message_id: String(d.message_id), message_kind: String(d.kind),
          round: Number(d.round ?? 1), channel: d.channel === 'call' ? 'call' : 'email', to: String(d.to ?? ''),
          subject: str(d.subject), body: String(d.body ?? ''), offer: num(d.offer),
          numbers: (d.numbers as NumberSource[]) ?? [], guard_passed: guardResults.get(String(d.message_id)) ?? null, audit: null,
        }
        sent.set(m.message_id, m)
        dealer(vid).items.push(m)
        break
      }
      case 'call.pending':
      case 'call.completed': {
        if (!vid) break
        dealer(vid).items.push({ kind: 'decision', event_id: e.id, ts: e.ts, channel: 'call', offer: null,
          action: e.type === 'call.pending' ? 'call pending' : 'call completed',
          reason: e.type === 'call.pending' ? 'Waiting for the existing call result. No repeat call will be placed.'
            : `Fo-reported outcome: ${String(d.outcome ?? 'unknown').replaceAll('_', ' ')}. ${d.identity_status === 'not_spoken' ? 'No introduction was spoken.' : d.identity_verified ? 'Buyer identity checked.' : 'Buyer identity needs review.'} A spoken price is not a written quote.` })
        break
      }
      case 'guard.checked': {
        const id = String(d.message_id)
        const passed = (guardResults.get(id) ?? true) && d.passed === true
        guardResults.set(id, passed)
        const m = sent.get(id)
        if (m) m.guard_passed = passed      // e.g. a fo_preview check after the send
        break
      }
      case 'audit.checked': {
        const m = sent.get(String(d.message_id))
        if (m) m.audit = { matched: d.matched === true, guard_passed: d.guard_passed === true, diff: str(d.diff) }
        break
      }
      case 'reply.received': {
        if (!vid) break
        const r: Reply = {
          kind: 'reply', event_id: e.id, ts: e.ts, reply_id: String(d.reply_id),
          source: (d.source as Reply['source']) ?? 'vendor_email', from: String(d.from ?? ''), text: String(d.text ?? ''),
          in_reply_to: str(d.in_reply_to),
        }
        replies.set(r.reply_id, r)
        dealer(vid).items.push(r)
        break
      }
      case 'quote.parsed': {
        if (!vid) break
        const q: Quote = {
          kind: 'quote', event_id: e.id, ts: e.ts, quote_id: String(d.quote_id), reply_id: String(d.reply_id),
          source: d.source === 'call' ? 'call' : 'email', price_total: num(d.price_total), firm: d.firm === true,
          declined: d.declined === true, suspicious: d.suspicious_instructions === true, meets_spec: d.meets_spec !== false,
          delivery_date: str(d.delivery_date), hedge_words: (d.hedge_words as string[]) ?? [],
          breakdown: (d.breakdown as Breakdown) ?? null, breakdown_flags: (d.breakdown_flags as string[]) ?? [],
          verification: null, verification_reason: null,
        }
        quotes.set(q.quote_id, q)
        const x = dealer(vid)
        x.items.push(q)
        if (q.price_total != null) x.latest_quote = q
        break
      }
      case 'quote.verified': {
        const q = quotes.get(String(d.subject_id))
        if (q) { q.verification = d.status === 'VERIFIED' ? 'VERIFIED' : 'UNVERIFIED'; q.verification_reason = str(d.reason) }
        break
      }
      case 'strategy.decided': {
        if (!vid) break
        dealer(vid).items.push({ kind: 'decision', event_id: e.id, ts: e.ts, action: String(d.action),
          reason: String(d.reason ?? ''), offer: num(d.offer), channel: d.channel === 'call' ? 'call' : 'email' })
        break
      }
      case 'job.needs_human': {
        alerts.push({ event_id: e.id, ts: e.ts, vendor_id: vid, reason_code: String(d.reason_code ?? 'review'),
          blocking: d.blocking === true, message: String(d.message ?? ''),
          flags: (d.flags as ReviewAlert['flags']) ?? [] })
        if (vid && d.reason_code === 'suspicious_instruction') dealer(vid).flagged = true
        break
      }
      case 'job.resumed': {
        const settled = alerts.find(a => a.event_id === d.resolves_event_id)
        if (settled) { settled.blocking = false; settled.resolution = String(d.message ?? '') }
        break
      }
      case 'job.completed': {
        fee_comparison = (d.fee_comparison as FeeRow[]) ?? null
        trust = (d.trust as Record<string, number>) ?? null
        break
      }
    }
  }
  return { dealers: [...dealers.values()], alerts, quotes, replies, fee_comparison, trust, job }
}

// One readable line per event for the activity feed.
export function describe(e: SealedEvent, names: Map<string, string>): string {
  const d = e.data as Data
  const who = e.vendor_id ? names.get(e.vendor_id) ?? e.vendor_id : 'Job'
  const money = (v: unknown) => (typeof v === 'number' ? `$${v.toLocaleString('en-US')}` : '—')
  const kind = String(d.kind ?? '').replaceAll('_', ' ')
  switch (e.type) {
    case 'job.created': return `Job created: ${String(d.item)}, target ${money(d.target)}`
    case 'job.started': return 'Negotiation started'
    case 'vendor.contacted': return `${who}: contacted`
    case 'guard.checked': return `${who}: ${kind} ${d.passed ? 'passed' : 'BLOCKED by'} the ${d.stage === 'fo_preview' ? "Fo-preview" : 'pre-send'} check`
    case 'message.sent': return `${who}: ${kind} sent by ${d.channel === 'call' ? 'call' : 'email'}${d.offer != null ? `, asking ${money(d.offer)}` : ''}`
    case 'message.queued': return `${who}: ${kind || 'message'} queued for Fo`
    case 'call.pending': return `${who}: waiting for the existing call result`
    case 'call.completed': return `${who}: call ended (${String(d.outcome ?? 'unknown').replaceAll('_', ' ')})`
    case 'message.suppressed': return `${who}: ${kind || 'message'} not sent (${String(d.reason ?? d.reason_code ?? 'suppressed').replaceAll('_', ' ')})`
    case 'audit.checked': return `${who}: sent copy ${d.matched ? 'matches' : 'DIFFERS from'} our text`
    case 'reply.received': return `${who}: ${d.source === 'call' ? 'call summary' : d.source === 'fo_relay' ? 'reply relayed by Fo' : 'written reply'} received`
    case 'quote.parsed': return `${who}: ${d.price_total == null ? 'no price' : money(d.price_total)}${d.firm ? ' (firm, written)' : ' (not firm)'}`
    case 'quote.verified': return `${who}: quote ${String(d.status).toLowerCase()}`
    case 'strategy.decided': return `${who}: next step ${String(d.action).replaceAll('_', ' ')}`
    case 'proposal.created': return `Proposal: ${who} at ${money(d.price_total)}`
    case 'approval.requested': return 'Waiting for your approval'
    case 'job.approved': return `You approved ${money(d.price_total)}`
    case 'job.rejected': return 'You rejected the proposal'
    case 'job.needs_human': return `Needs your review: ${String(d.message ?? d.reason_code)}`
    case 'job.resumed': return `Resumed: ${String(d.message ?? d.outcome)}`
    case 'job.completed': return `Completed with ${who}: ${money(d.best_quote)}`
    case 'job.cancelled': return 'Negotiation cancelled'
    case 'error': return `${who}: ${String(d.message ?? d.code)}`
    default: return `${who}: ${e.type}`
  }
}
