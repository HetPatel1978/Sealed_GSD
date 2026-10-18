import { API_BASE } from './config'

export type Health = { status: string; version: string; mode: 'sim' | 'live'; fo: string; gmail: string; worker?: string }
export type Proposal = {
  proposal_id: string; vendor_id: string; quote_id: string; price_total: number
  currency: string; delivery_date: string | null; flags: string[]
}
export type Vendor = {
  id: string; name: string; email: string | null; phone?: string | null; mode: string; state: string; flags: string[]
  latest_quote: { price_total: number | null; currency: string; firm: boolean; delivery_date: string | null; verification: string } | null
}
export type Job = {
  id: string; run_id: string; item: string; spec: { quantity: number; details: string }; deadline: string
  currency: string; target: number; ceiling_set: boolean; round: number; mode: 'sim' | 'live'; state: string
  vendors: Vendor[]; proposal: Proposal | null; first_quote: number | null; best_quote: number | null
  trust: Record<string, number>; last_event_id: number; worker_status?: string; start_requested?: boolean
}
export type SealedEvent = { id: number; job_id: string; ts: string; type: string; vendor_id: string | null; mode: string; data: Record<string, unknown> }
export type Receipt = {
  job_id: string; vendor_name: string; price_total: number; currency: string; delivery_date: string | null
  savings_abs: number; verification: string; mode: string; evidence: { kind: string; ref: string; label: string }[]
}

const messages: Record<string, string> = {
  validation_error: 'Check the fields. Use one item, two rounds, a future deadline and a ceiling above your target.',
  stale_proposal: 'The proposal changed. Review the latest terms before approving.',
  live_permissions_required: 'Authorize reply processing and outreach before starting a live job.',
  private_runtime_required: 'Restore your private ceiling to continue this job.',
  integration_review_required: 'This job needs integration review. Check its latest status before taking another action.',
  pending_integration_review: 'Mail or integration checks need to settle before approval.',
  discovery_unavailable: 'Dealer discovery is unavailable. You can add sellers manually.',
  discovery_permission_required: 'Authorize Fo research before starting a live dealer search.',
  discovery_selection_changed: 'The saved dealer selection changed or expired. Search again or add the seller manually.',
  job_already_started: 'This job has already started. Its current state has been refreshed.',
  job_not_found: 'This job could not be found.',
}

export async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', credentials: 'omit',
  })
  if (!response.ok) {
    const problem = await response.json().catch(() => ({ error: '' })) as { error?: string }
    throw new Error(messages[problem.error ?? ''] ?? `Request failed (HTTP ${response.status}). Refresh the job before retrying.`)
  }
  return response.json() as Promise<T>
}

export const eventTypes = [
  'job.created', 'job.started', 'vendor.contacted', 'guard.checked', 'message.queued', 'message.suppressed',
  'message.sent', 'audit.checked', 'reply.received', 'quote.parsed', 'quote.verified', 'strategy.decided',
  'proposal.created', 'approval.requested', 'job.approved', 'job.rejected', 'job.needs_human', 'job.completed',
  'job.cancelled', 'error',
]
