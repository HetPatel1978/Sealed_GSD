# Sealed API contract (v0.2)

v0.2 (2026-10-17): additive only. Optional `guard.checked.stage`, `message.sent.channel`, `quote.parsed.source`, `reply.received.source: call`; `strategy.decided.offer` is always the target or null. See `docs/plan.md` section 2.

Base URL (local): `http://localhost:8000`. All bodies are JSON, all timestamps ISO 8601 UTC, all money is a number in the job's `currency` (e.g. `372.0`).

## The one rule

**The ceiling is write-only.** It is accepted once, on `POST /jobs`, and is never returned by any endpoint, event, log line, or error message. Nothing derived from it is returned either: no "gap to budget", no "within budget" flag, no percentage of it. The frontend never needs it and never gets it.

The target price (`target`) is *not* secret from the frontend; it is the opening ask Sealed sends sellers.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Service and integration status |
| POST | `/jobs` | Create a job |
| GET | `/jobs/{id}` | Current job state (never includes the ceiling) |
| POST | `/jobs/{id}/start` | Start negotiating |
| GET | `/jobs/{id}/events` | SSE event stream, supports `Last-Event-ID` |
| POST | `/jobs/{id}/approve` | Approve the current proposal (echoes exact terms) |
| POST | `/jobs/{id}/reject` | Reject the current proposal |
| POST | `/jobs/{id}/cancel` | Cancel the job |
| GET | `/jobs/{id}/receipt` | Final receipt with evidence |
| POST | `/sim/advance` | Step simulated sellers forward (sim mode only) |

### GET /health
```json
{ "status": "ok", "version": "0.1.0", "mode": "sim",
  "fo": "live" | "not_configured" | "down",
  "gmail": "live" | "not_configured" | "down" }
```

### POST /jobs
Request:
```json
{
  "item": "Custom team hoodies",
  "spec": { "quantity": 6, "details": "Navy, 2-colour front print, sizes S-XL" },
  "deadline": "2026-10-30",
  "currency": "USD",
  "target": 330.0,
  "ceiling": "<number, secret>",
  "max_rounds": 2,
  "vendors": [
    { "name": "Northstar Apparel", "email": "orders@northstar.example", "mode": "sim" }
  ]
}
```
- `ceiling`: required, must be `> target`. Stored in the private zone only.
- `max_rounds`: default 2. `vendors`: 1-5 entries; `mode` is `"live"` or `"sim"`.

Response `201`: a `JobView` (below). Errors: `422` validation (the message never echoes the ceiling value).

### GET /jobs/{id}
Response `200`: `JobView`
```json
{
  "id": "job_7f3a",
  "run_id": "run_01",
  "item": "Custom team hoodies",
  "spec": { "quantity": 6, "details": "Navy, 2-colour front print, sizes S-XL" },
  "deadline": "2026-10-30",
  "currency": "USD",
  "target": 330.0,
  "ceiling_set": true,
  "max_rounds": 2,
  "round": 2,
  "mode": "sim",
  "state": "CREATED | RUNNING | AWAITING_APPROVAL | BOOKING | COMPLETED | NEEDS_HUMAN | CANCELLED | FAILED",
  "started_at": "2026-10-17T14:00:05Z",
  "vendors": [ VendorView ],
  "proposal": ProposalView | null,
  "first_quote": 432.0 | null,
  "best_quote": 372.0 | null,
  "trust": TrustSummary,
  "last_event_id": 54
}
```

`VendorView`:
```json
{
  "id": "v_northstar",
  "name": "Northstar Apparel",
  "email": "orders@northstar.example",
  "mode": "sim",
  "state": "CREATED | CONTACTED | QUOTE_RECEIVED | COUNTERED | FINAL_OFFER | SELECTED | APPROVED | BOOKING_SENT | ACCEPTED | VERIFIED | NO_REPLY | DECLINED | DISQUALIFIED | FAILED | NEEDS_HUMAN | CANCELLED",
  "latest_quote": {
    "quote_id": "q_06", "price_total": 372.0, "price_unit": 62.0, "derived_total": false,
    "currency": "USD", "firm": true, "delivery_date": "2026-10-28",
    "meets_deadline": true, "meets_spec": true, "verification": "VERIFIED | UNVERIFIED"
  } | null,
  "flags": ["hedged", "declined", "late", "suspicious_instruction"]
}
```

`ProposalView`:
```json
{ "proposal_id": "p_02", "vendor_id": "v_northstar", "quote_id": "q_06",
  "price_total": 372.0, "currency": "USD", "delivery_date": "2026-10-28",
  "flags": [] }
```

`TrustSummary` (live counters; final values also appear on `job.completed`):
```json
{ "budget_revealed_count": 0, "messages_checked_before": 7, "messages_checked_after": 7,
  "false_claims": 0, "commitments_without_approval": 0, "suspicious_instructions_flagged": 1 }
```

### POST /jobs/{id}/start
Request: `{ "seed": 42 }` (optional; only used for sim sellers). Response `200`: `JobView`. `409` if already started.

### GET /jobs/{id}/events (SSE)
- `Content-Type: text/event-stream`. Each message:
  ```
  id: 17
  event: quote.parsed
  data: {...full event envelope as JSON...}
  ```
- On connect the server replays every event with `id > Last-Event-ID` (header, or `?last_event_id=` query param for `EventSource` reconnects you control), then streams live.
- No `Last-Event-ID` = replay from the start.
- Heartbeat comment `: ping` every 15 s.

### POST /jobs/{id}/approve
The body must echo the exact terms the user saw. If anything differs from the current proposal (or the proposal is gone/superseded), the server returns `409` and does nothing.
```json
{ "proposal_id": "p_02", "vendor_id": "v_northstar",
  "price_total": 372.0, "currency": "USD", "delivery_date": "2026-10-28" }
```
Response `200`: `JobView` (state `BOOKING`). `409`: `{ "error": "stale_proposal", "current_proposal": ProposalView | null }`.

### POST /jobs/{id}/reject
`{ "proposal_id": "p_01", "reason": "optional free text" }` → `200` `JobView`. `409` if stale.

### POST /jobs/{id}/cancel
`{ "reason": "optional" }` → `200` `JobView` (state `CANCELLED`). Contacted sellers get a short, polite "no longer needed" note (live mode only after the UI confirms).

### GET /jobs/{id}/receipt
`200` once `COMPLETED`, `409` before.
```json
{
  "job_id": "job_7f3a", "vendor_id": "v_northstar", "vendor_name": "Northstar Apparel",
  "price_total": 372.0, "currency": "USD", "delivery_date": "2026-10-28",
  "first_quote": 432.0, "best_quote": 372.0, "savings_abs": 60.0, "savings_pct": 13.89,
  "rounds": 2, "duration_s": 3300, "verification": "VERIFIED",
  "evidence": [ { "kind": "gmail_message", "ref": "sim-msg-0016", "label": "Northstar order confirmation" } ],
  "trust": TrustSummary,
  "mode": "sim"
}
```

### POST /sim/advance
Sim mode only (`404` in live mode).
```json
{ "job_id": "job_7f3a", "steps": 1 }
```
`steps`: number of pending simulated seller actions to release (default 1; `-1` = run to completion). Response: `{ "released": 1, "pending": 3 }`.

---

## Events

### Envelope
```json
{ "id": 17, "run_id": "run_01", "job_id": "job_7f3a", "ts": "2026-10-17T14:12:30Z",
  "type": "quote.parsed", "vendor_id": "v_northstar" | null, "mode": "live" | "sim",
  "data": { } }
```
- `id`: integer, strictly increasing per job; this is the SSE `id`.
- `mode`: whether this action really happened via Fo/Gmail (`live`) or was simulated (`sim`). The UI must show it.
- Events are append-only. Nothing in any event contains the ceiling or anything derived from it.

### Shared shapes inside `data`
- `message_id`: our outbound message id, e.g. `"m_03"`.
- `kind` (outbound): `rfq | firm_up | counter | price_match | booking | cancel`.
- `reply_id`: inbound message id, e.g. `"r_02"`. `quote_id`: e.g. `"q_02"`. `proposal_id`: e.g. `"p_01"`.

### Event types

| Type | vendor_id | `data` fields |
|---|---|---|
| `job.created` | null | `item`, `spec`, `deadline`, `currency`, `target`, `max_rounds`, `vendors: [{id, name, email, mode}]` |
| `job.started` | null | `seed` (sim only, else null), `round: 1` |
| `vendor.contacted` | set | `message_id`, `round` (vendor state becomes `CONTACTED`) |
| `guard.checked` | set | `message_id`, `kind`, `round`, `passed: bool`, `rules: [{rule, passed, detail?}]`. Rules: `secret_checker` (yes/no only, never the value), `limit_language` (no "budget"/"max price"-style wording), `leverage_grounded`, `commitment_words`, `safety_line`; at the `fo_preview` stage also `preview_matches_ours`. No budget values ever. Optional `stage: "pre_send" | "fo_preview"` (a message can be checked twice: our text, then Fo's confirmation preview). |
| `message.sent` | set | `message_id`, `kind`, `round`, `to`, `subject`, `body` (the exact email body we asked Fo to send), `offer` (number or null), `cited_quote_id` (price_match only). Optional `channel: "email" | "call"` (default email); for a call, `body` is the sealed call brief (goal, allowed facts, hard limits) and `subject` is null. |
| `audit.checked` | set | `message_id`, `cc_copy_found: bool`, `matched: bool` (Fo's sent copy equals our body), `diff` (null or short text), `guard_passed: bool` (guard re-run on the actual sent copy) |
| `reply.received` | set | `reply_id`, `in_reply_to` (message_id), `source: vendor_email | fo_relay | call` (`call` = Fo's summary of a phone call), `from`, `subject`, `text` (seller's own words, quotes/signature stripped), `evidence: {kind, ref}` |
| `quote.parsed` | set | `reply_id`, `quote_id`, `price_total` (null if none), `price_unit`, `derived_total: bool` (total computed by code from unit x qty), `currency`, `firm: bool`, `hedge_words: []`, `delivery_date`, `meets_deadline: bool`, `meets_spec: bool`, `declined: bool`, `suspicious_instructions: bool`, `numbers_grounded: bool`. Optional `source: "email" | "call"`; a price heard on a call always has `firm: false` and is never cited to other sellers. |
| `quote.verified` | set | `subject_id` (quote_id or message_id for a booking), `status: VERIFIED | UNVERIFIED`, `reason`, `evidence: {kind, ref}`. VERIFIED only when the seller's own email is in the user's inbox; a Fo relay alone is UNVERIFIED. |
| `strategy.decided` | set or null | `round`, `action: counter | price_match | firm_up | propose | needs_human`, `offer` (always the job's `target`, or null; never any other number), `cited_quote_id` + `cited_price` (price_match only), `reason` (plain text, never mentions the ceiling) |
| `proposal.created` | set | `proposal_id`, `quote_id`, `price_total`, `currency`, `delivery_date`, `first_quote`, `savings_abs`, `savings_pct`, `flags: []` |
| `approval.requested` | set | `proposal_id` |
| `job.approved` | set | `proposal_id`, `price_total`, `currency`, `delivery_date` (exact echoed terms) |
| `job.rejected` | set | `proposal_id`, `reason` |
| `job.needs_human` | set or null | `reason_code: suspicious_instruction | no_qualifying_quote | all_declined | audit_mismatch | guard_blocked | call_review | fo_refused | fo_failed | send_unclear | payment_mention | integration_down`, optional `fo_said` (Fo's own words), optional `flags: [{flag: number_outside_brief | commitment | money_topic, speaker: fo | dealer, turn, excerpt}]` (for `call_review`), `blocking: bool`, `message`, `closest` (optional `{vendor_id, price_total, delivery_date}`; never a gap to the ceiling) |
| `job.completed` | set | `vendor_id`, `first_quote`, `best_quote`, `savings_abs`, `savings_pct`, `rounds`, `duration_s`, `trust: TrustSummary` |
| `error` | set or null | `code`, `message` (user-safe), `retryable: bool`, `resent: bool` (always false for bookings) |

### Definitions
- `first_quote`: the first **firm** total price received in the job.
- `best_quote`: the approved price.
- `savings_abs = first_quote - best_quote`, `savings_pct = round(100 * savings_abs / first_quote, 2)`.
- `rounds`: negotiation rounds used (round 1 = request for quote + any firm-up; round 2+ = counters).
- `duration_s`: seconds from `job.started` to `job.completed`.

### Mock
`mock/events.json` is one full simulated run in this exact format. `python mock/check_mock.py` validates it.
