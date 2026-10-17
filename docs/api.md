# Sealed API contract (v0.3)

v0.3 (2026-10-17): the ceiling is shown back to the user through one private endpoint (`GET /jobs/{id}/ceiling`); dealer discovery (`POST /dealers/discover`, 20-mile radius); optional `zip` on jobs and `phone` / `source` on vendors. Also additive: optional `quote.parsed.breakdown` + `breakdown_flags`, `message.sent.numbers`, `job.completed.fee_comparison` (shapes under "Amounts shown to the user" below). The ceiling rule changed: see "The one rule".

v0.2 (2026-10-17): additive only. Optional `guard.checked.stage`, `message.sent.channel`, `quote.parsed.source`, `reply.received.source: call`; `strategy.decided.offer` is always the target or null. See `docs/plan.md` section 2.

Base URL (local): `http://localhost:8000`. All bodies are JSON, all timestamps ISO 8601 UTC, all money is a number in the job's `currency` (e.g. `372.0`).

## The one rule

**The ceiling never leaves the backend except to the user's own dashboard, through one endpoint.** It is accepted on `POST /jobs` and returned only by `GET /jobs/{id}/ceiling`, so the user can see what they set. It is never in `JobView`, any event, the SSE stream, a log line, or an error message, and it is never sent to Fo, any dealer, or any LLM. The backend returns nothing derived from it (no "gap to budget", no "within budget" flag, no percentage of it); if the dashboard wants to show "best quote vs your ceiling", it computes that in the browser and never sends it anywhere.

The target price (`target`) is not secret: the dashboard shows it, and it is the price Sealed openly asks dealers for.

Why the endpoint is separate: the event log stays ceiling-free, so `mock/check_mock.py` and the backend's "grep every event and outgoing message for the ceiling" test stay meaningful.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Service and integration status |
| POST | `/dealers/discover` | Find dealers near a ZIP for the job's car (suggestions only; contacts no one) |
| POST | `/jobs` | Create a job |
| GET | `/jobs/{id}` | Current job state (never includes the ceiling) |
| GET | `/jobs/{id}/ceiling` | The ceiling the user set, for display on their dashboard only |
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

### POST /dealers/discover
Finds dealers for the car within a radius of the ZIP. Claude (web search + web fetch, no other tools, never given the ceiling or target) proposes dealers; code then keeps only those whose contact details appear word for word on a fetched page and whose address ZIP is within the radius (offline ZIP-to-location distance check). **Nothing is contacted.** The user picks up to 5 results and sends them in `POST /jobs`.

Request:
```json
{ "item": "2026 Honda Civic Sport", "spec": { "quantity": 1, "details": "2026 Honda Civic Sport sedan, Crystal Black Pearl, automatic" },
  "zip": "19104", "radius_miles": 20 }
```
- `radius_miles`: optional, default `20`. **v1 accepts only `20`** (`422` otherwise). `10` and `50` are enabled later, once the core flow and dashboard are built (`docs/plan.md`).

Response `200`:
```json
{
  "zip": "19104", "radius_miles": 20, "mode": "live" | "sim",
  "dealers": [
    { "name": "Valley Honda", "email": "sales@valleyhonda.example" | null, "phone": "+1-215-555-0101" | null,
      "website": "https://valleyhonda.example", "address": "3900 Chestnut St, Philadelphia, PA 19104",
      "distance_miles": 4.2, "in_stock": true | false | null,
      "source": { "kind": "discovery", "urls": ["https://valleyhonda.example/contact"] } }
  ],
  "dropped": 3
}
```
- Every non-null `email`, `phone` and `address` appeared literally on one of `source.urls`. `in_stock: null` = not known.
- `dropped`: how many Claude suggestions code removed (outside the radius, or details not found on a fetched page).
- A dealer with neither email nor phone is never returned. A dealer with no email can only be reached by call.
- Errors: `422` validation; `503` `{ "error": "discovery_unavailable" }` if the search fails (the user can still type dealers in by hand).

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
  "zip": "19104",
  "vendors": [
    { "name": "Northstar Apparel", "email": "orders@northstar.example", "phone": null, "mode": "sim",
      "source": { "kind": "manual" } }
  ]
}
```
- `ceiling`: required, must be `> target`. Stored in the private zone only; shown back only by `GET /jobs/{id}/ceiling`.
- `max_rounds`: default 2. `vendors`: 1-5 entries; `mode` is `"live"` or `"sim"`.
- Optional (v0.3): `zip`; per vendor `phone` (needed for calls) and `source` (`{ "kind": "manual" }` if typed in, or the `source` object from `POST /dealers/discover`). Each vendor needs an `email` or a `phone`.

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

### GET /jobs/{id}/ceiling
Response `200`: `{ "job_id": "job_c41v", "ceiling": 32000.0, "currency": "USD" }`, with header `Cache-Control: no-store`.
- The only place the ceiling is ever returned. The dashboard fetches it when it shows the job, keeps it in memory, and never puts it in a URL, `localStorage`, analytics, or any other request.
- `404` if the job doesn't exist. Not logged (the request log records the path, never the body).

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
| `job.created` | null | `item`, `spec`, `zip` (v0.3, optional), `deadline`, `currency`, `target`, `max_rounds`, `vendors: [{id, name, email, phone, mode}]` (`phone` v0.3, optional). Never the ceiling. |
| `job.started` | null | `seed` (sim only, else null), `round: 1` |
| `vendor.contacted` | set | `message_id`, `round` (vendor state becomes `CONTACTED`) |
| `guard.checked` | set | `message_id`, `kind`, `round`, `passed: bool`, `rules: [{rule, passed, detail?}]`. Rules: `secret_checker` (yes/no only, never the value), `limit_language` (no "budget"/"max price"-style wording), `leverage_grounded`, `commitment_words`, `safety_line`; at the `fo_preview` stage also `preview_matches_ours`. No budget values ever. Optional `stage: "pre_send" | "fo_preview"` (a message can be checked twice: our text, then Fo's confirmation preview). |
| `message.sent` | set | `message_id`, `kind`, `round`, `to`, `subject`, `body` (the exact email body we asked Fo to send), `offer` (number or null), `cited_quote_id` (price_match only). Optional `channel: "email" | "call"` (default email); for a call, `body` is the sealed call brief (goal, allowed facts, hard limits) and `subject` is null. Optional `numbers`: every number in `body` and where it came from (see below). |
| `audit.checked` | set | `message_id`, `cc_copy_found: bool`, `matched: bool` (Fo's sent copy equals our body), `diff` (null or short text), `guard_passed: bool` (guard re-run on the actual sent copy) |
| `reply.received` | set | `reply_id`, `in_reply_to` (message_id), `source: vendor_email | fo_relay | call` (`call` = Fo's summary of a phone call), `from`, `subject`, `text` (seller's own words, quotes/signature stripped), `evidence: {kind, ref}` |
| `quote.parsed` | set | `reply_id`, `quote_id`, `price_total` (null if none), `price_unit`, `derived_total: bool` (total computed by code from unit x qty), `currency`, `firm: bool`, `hedge_words: []`, `delivery_date`, `meets_deadline: bool`, `meets_spec: bool`, `declined: bool`, `suspicious_instructions: bool`, `numbers_grounded: bool`. Optional `source: "email" | "call"`; a price heard on a call always has `firm: false` and is never cited to other sellers. Optional `breakdown` (itemised parts as the dealer wrote them) and `breakdown_flags: [breakdown_mismatch | breakdown_ungrounded]`. |
| `quote.verified` | set | `subject_id` (quote_id or message_id for a booking), `status: VERIFIED | UNVERIFIED`, `reason`, `evidence: {kind, ref}`. VERIFIED only when the seller's own email is in the user's inbox; a Fo relay alone is UNVERIFIED. |
| `strategy.decided` | set or null | `round`, `action: counter | price_match | firm_up | propose | needs_human`, `offer` (always the job's `target`, or null; never any other number), `cited_quote_id` + `cited_price` (price_match only), `reason` (plain text, never mentions the ceiling) |
| `proposal.created` | set | `proposal_id`, `quote_id`, `price_total`, `currency`, `delivery_date`, `first_quote`, `savings_abs`, `savings_pct`, `flags: []` |
| `approval.requested` | set | `proposal_id` |
| `job.approved` | set | `proposal_id`, `price_total`, `currency`, `delivery_date` (exact echoed terms) |
| `job.rejected` | set | `proposal_id`, `reason` |
| `job.needs_human` | set or null | `reason_code: suspicious_instruction | no_qualifying_quote | all_declined | audit_mismatch | guard_blocked | call_review | fo_refused | fo_failed | send_unclear | payment_mention | integration_down`, optional `fo_said` (Fo's own words), optional `flags: [{flag: number_outside_brief | commitment | money_topic, speaker: fo | dealer, turn, excerpt}]` (for `call_review`), `blocking: bool`, `message`, `closest` (optional `{vendor_id, price_total, delivery_date}`; never a gap to the ceiling) |
| `job.completed` | set | `vendor_id`, `first_quote`, `best_quote`, `savings_abs`, `savings_pct`, `rounds`, `duration_s`, `trust: TrustSummary`, optional `fee_comparison` (see below) |
| `error` | set or null | `code`, `message` (user-safe), `retryable: bool`, `resent: bool` (always false for bookings) |

### Amounts shown to the user (v0.3)

Every field is nullable. Amounts come only from what the dealer actually wrote; each one must appear in the dealer's own text (the parser never invents numbers; code drops ungrounded ones and flags `breakdown_ungrounded`).

`quote.parsed.breakdown`:
```json
{
  "vehicle_price": 27890.0,
  "dealer_fees": [{"label": "Doc fee", "amount": 389.0}, {"label": "Dealer prep", "amount": 495.0}],
  "add_ons": [{"label": "Nitrogen-filled tires", "amount": 199.0}],
  "taxes": 1637.0,
  "title_registration": 243.0,
  "other": [],
  "out_the_door_total": 30690.0,
  "valid_until": "2026-10-31"
}
```
- Code checks that `vehicle_price + dealer_fees + add_ons + taxes + title_registration + other` equals the total within $1. If not, `breakdown_flags` includes `breakdown_mismatch`, and `price_total` stays the dealer's stated total.

`message.sent.numbers`: every distinct number in `body`, with its source.
```json
[{"value": 30690.0, "source": "ledger", "quote_id": "q_05"},
 {"value": 29500.0, "source": "target"},
 {"value": 19104.0, "source": "job"}]
```
- `ledger`: a dealer's written quote (`quote_id` given; value equals that quote's `price_total`). `target`: the job's public target. `job`: a fact from the job spec (model year, ZIP, quantity, timeline). Nothing else can appear; the guard blocks unknown prices.

`job.completed.fee_comparison`: each dealer's latest firm written quote, lowest total first.
```json
[{"vendor_id": "v_harbor", "quote_id": "q_09", "out_the_door_total": 30450.0, "vehicle_price": 27950.0,
  "dealer_fees_total": 449.0, "add_ons_total": 0, "taxes": 1677.0, "title_registration": 374.0, "other_total": 0,
  "dealer_fees": [{"label": "Doc fee", "amount": 449.0}], "add_ons": [], "breakdown_flags": [], "flags": []}]
```
- `flags` may include `suspicious_instruction` for a dealer flagged during the job. Category totals are `0` or null when the dealer didn't itemise.

### Definitions
- `first_quote`: the first **firm** total price received in the job.
- `best_quote`: the approved price.
- `savings_abs = first_quote - best_quote`, `savings_pct = round(100 * savings_abs / first_quote, 2)`.
- `rounds`: negotiation rounds used (round 1 = request for quote + any firm-up; round 2+ = counters).
- `duration_s`: seconds from `job.started` to `job.completed`.

### Mock
`mock/events.json` is one full simulated run in this exact format. `python mock/check_mock.py` validates it.
`mock/ceiling.json` is the matching `GET /jobs/{id}/ceiling` response (a made-up value; it is not in `events.json`, and `python mock/check_mock.py --secret 32000` confirms no event contains it or anything within 2%).
`mock/discovery.json` is a sample `POST /dealers/discover` response (fictional `.example` dealers).
