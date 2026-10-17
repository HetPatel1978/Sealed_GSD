# Sealed — build plan (delta vs repo)

Status: 2026-10-17 (Saturday). Design: `docs/architecture.md` (decided). Code freeze: **Sunday evening**. Post on X by **Mon Oct 19, 10:15am ET**. Het: backend. Renu: dashboard only.

## 1. Keep / change / delete, file by file

| File | Action | Notes |
|---|---|---|
| `backend/app/main.py` | **Change** | Add the routes from `docs/api.md` + SSE. `/health` stays. |
| `backend/sealed/env.py` | Keep | |
| `backend/sealed/fo/auth.py` | Keep | Token storage with absolute expiry, refresh before connect (VERIFIED). One adapter process only (refresh tokens rotate). |
| `backend/sealed/fo/messages.py` | **Done (step 1)** | Fo's voice ("I'm Fo, an AI assistant writing on behalf of …"); kinds `rfq`, `firm_up`, `price_match`, `counter` (target only), `booking`, `cancel`; **add call scripts** (`rfq` nudge, round-2 `price_match`). Keep `wrap_for_fo` + tag. Inputs: job spec, target, ledger facts. No ceiling parameter anywhere. |
| `backend/sealed/fo/adapter.py` | **New** | Serial queue; one Fo conversation per dealer; dedupe key (dealer, round, kind, channel); preview check before "yes"; poll `get_fo_reply`, never resend. Live + sim implementations. |
| `backend/sealed/core/ledger.py` | **Done (step 1)** | Quotes as records; firm = written out-the-door total for the exact spec; spoken prices stored `firm: false, source: call`. |
| `backend/sealed/core/strategy.py` + `engine.py` | **Done (step 1)** | Fixed round plan. **No ceiling input** (signature enforces it). Offers only target or ledger values. |
| `backend/sealed/private/qualifier.py` (+ `vault.py`) | **Done (step 1)** | Only module that reads the ceiling (plus the checker). Runs once after the fixed rounds → `propose` / `needs_human`. |
| `backend/sealed/core/guard.py` + `private/secret_checker.py` | **Done (step 1)** | Rules: secret checker (number, words, ±2%), budget vocabulary, grounded leverage, commitment words, safety line. Stages `pre_send` and `fo_preview`. |
| `backend/sealed/ingest/gmail.py` | **New** (from `spikes/gmail_read.py`) | Read-only IMAP; match by tag → In-Reply-To/References → sender + time; dedupe on message id; post-send audit of the CC copy. |
| `backend/sealed/parser/quote.py` | **New** | Quarantined LLM → fixed record; code validates spec match, out-the-door total, grounding. |
| `backend/sealed/core/events.py`, `store.py` | **New** | Append-only event log (SQLite), SSE replay by `Last-Event-ID`. |
| `backend/tests/` | **Extend** | Unit tests per module + a full sim run test that greps every outgoing string and event for the ceiling; import test: only `qualifier`/`secret_checker` import the ceiling store. |
| `docs/api.md` | **Change (additive)** | Optional fields below (section 2). Done in this branch. |
| `mock/build_mock.py`, `mock/events.json` | **Rewrite** | Car scenario; offers only target or null; includes a call nudge, a spoken price (`firm: false`), a `fo_preview` guard check. |
| `mock/check_mock.py` | **Change** | Also assert `strategy.decided.offer ∈ {target, null}`; allow the new optional fields. |
| `docs/sealed_module_io_flow.png` | Keep, marked superseded | README points to `architecture.md`. |
| `docs/fo_tools.md` | Keep, stale | Superseded by `docs/research/fo_mcp.md` (5 tools, output schemas). |
| `spikes/*` | Keep | Throwaway; nothing imports them. |
| `frontend/` | Renu's | No backend changes there. |

## 2. API / event changes for Renu (all optional fields; nothing removed or renamed)

| Event / field | Change |
|---|---|
| `strategy.decided.offer` | Always equals the job's `target`, or `null`. |
| `guard.checked.stage` | New optional: `"pre_send"` \| `"fo_preview"`. A message can have two guard checks. |
| `message.sent.channel` | New optional: `"email"` \| `"call"`. `kind` unchanged. For calls, `body` holds the sealed brief and `subject` is null. |
| `quote.parsed.source` | New optional: `"email"` \| `"call"`. A spoken price arrives with `firm: false`, `source: "call"`. |
| `reply.received.source` | Adds `"call"` (Fo's call summary) next to `vendor_email` / `fo_relay`. |
| Mock scenario | One car: `spec.quantity: 1`, `spec.details` = model/trim/color; all prices are out-the-door totals. |

**Compatibility check (done):** the envelope and every existing field are unchanged, so a dashboard built on the current `docs/api.md` and `mock/events.json` keeps working. One semantic difference: the current mock's round-2 counter has `offer: 357`, which the new rule forbids (`offer` must be the target, 330, or null). Renu should not build UI that assumes a counter offer differs from the target. The regenerated car mock fixes this.

## 3. Build order (estimates; Saturday ~14:00 ET → Sunday ~19:00 ET freeze)

| # | Step | Estimate | Target window | Done when |
|---|---|---|---|---|
| 0 ✅ | Car mock + `check_mock.py` rule update (unblocks Renu) | 1 h | Sat 14:00–15:00 | `check_mock.py` green on the car run |
| 1 ✅ | Ledger + strategy (fixed rounds) + qualifier + templates + call scripts + guard, with unit tests, runnable end-to-end in **sim mode** | 5 h | Sat 15:00–20:00 | A sim job produces the full event stream; ceiling-grep test green |
| 2 | Fo adapter: serial queue, one conversation per dealer, preview check, `get_fo_reply` polling, dedupe | 3 h | Sat 20:00–23:00 | One live email to rfb5381@psu.edu through the adapter (after Het's yes) |
| 3 | Reply ingestion (Gmail IMAP) + quote parser + validation + post-send audit | 4 h | Sun 08:00–12:00 | Renu's reply-all becomes a ledger quote; audit diff recorded |
| — | **Decision: real dealers vs stand-ins** | — | **Sun 12:00** | |
| 4 | Calls: round-1 nudge to non-responders, round-2 call, through the same adapter and queue; call summary → `reply.received` (source `call`) | 2 h | Sun 12:00–14:00 | One call to Het's phone through the adapter |
| 5 | SSE + routes matching `docs/api.md`; Renu switches from mock to live | 3 h | Sun 14:00–17:00 | Dashboard shows a live sim run |
| — | End-to-end run, README, tests green, freeze | 2 h | Sun 17:00–19:00 | Freeze |
| — | Demo video + X post draft | — | Sun night | Posted by Mon 10:15 ET |

Renu can work against the car mock from step 0 onward; SSE wiring lands in step 5.

## 4. Cut list (in cut order)

1. **Live dealer round** → fall back to stand-in dealers (labelled on screen).
2. **Gmail post-send audit** → keep the audit via `get_fo_conversation` (our turn + Fo's confirmation text).
3. **Round-2 calls** → keep the round-1 call nudge.

Never cut: ceiling isolation (qualifier only), pre-send guard, written-quotes-only rule, approval gate, live/sim labels.

## 5. Demo checklist

- [ ] **Sunday noon:** decide real dealers (only with real purchase intent for that exact car) vs stand-in dealers.
- [ ] If stand-ins: 3 consenting people with real inboxes (one is rfb5381@psu.edu), scripted replies (clean total; MSRP + "fees extra"; late reply that revises down). **At least one stand-in reachable by phone** so the demo shows a call.
- [ ] Demo Wajo account: no payment card connected; no budget ever mentioned to Fo in any chat.
- [ ] Every send and call approved by Het; test calls only to Het's phone.
- [ ] Polite decline sent to every non-winner.
- [ ] Trust counter on screen: messages checked before/after, 0 ceiling mentions, 0 commitments without approval.
- [ ] **Record the demo video Sunday night**; post on X by Mon 10:15 ET.

## 6. Status

- Step 0 done (`e38de1f`).
- Step 1 done: `sealed/core` (models, ledger, strategy, guard, events, engine), `sealed/private` (vault, secret checker, qualifier), `sealed/sim` (stand-in dealers, transport, runner), templates + call scripts in `sealed/fo/messages.py`; 46 tests. `mock/events.json` is now generated by the sim engine (`mock/build_mock.py`). Qualifier rule: a dealer flagged for a suspicious instruction is proposed only if no unflagged firm quote qualifies; the flag is shown either way.

- v0.3 (additive): itemised price breakdowns on `quote.parsed` (grounded in the dealer's text, sum-checked within $1, `breakdown_mismatch` flag), `message.sent.numbers` (source of every number), `job.completed.fee_comparison`. Mock dealers itemise; Harbor's first quote has a junk add-on (nitrogen-filled tires), Summit's parts don't add up.
