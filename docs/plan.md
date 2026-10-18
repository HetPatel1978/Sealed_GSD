# Sealed — build plan (delta vs repo)

Status: 2026-10-17 (Saturday). Design: `docs/architecture.md` (decided). Code freeze: **Sunday evening**. Post on X by **Mon Oct 19, 10:15am ET**. Backend and dashboard are built together by the team. v0.3 additions: ceiling + target shown on the dashboard (decision 13), dealer discovery with a 20-mile radius (decision 14).

## 1. Keep / change / delete, file by file

| File | Action | Notes |
|---|---|---|
| `backend/app/main.py` | **Change** | Add the routes from `docs/api.md` + SSE, including `GET /jobs/{id}/ceiling` (`no-store`, not logged) and `POST /dealers/discover`. `/health` stays. |
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
| `backend/sealed/discovery/` | **Done (step 6, decision 16)** | Fo first (`fo_finder.py`, one research-only turn), Claude backup (`claude_finder.py`, `web_search` + `web_fetch`, structured output); neither gets the ceiling or target. Code check for both (`verify.py` + our GET-only `fetch.py`): details found literally on fetched pages, distance with `pgeocode`, radius 20 only in v1. Order in `dealers.py`, `SEALED_DISCOVERY_ORDER`. |
| `backend/sealed/core/events.py`, `store.py` | **New** | Append-only event log (SQLite), SSE replay by `Last-Event-ID`. |
| `backend/tests/` | **Extend** | Unit tests per module + a full sim run test that greps every outgoing string and event for the ceiling; import test: only `qualifier`/`secret_checker` and the `GET /jobs/{id}/ceiling` route read the ceiling store; `JobView` never contains it; discovery prompts never contain ceiling or target; discovery drops details not found on a fetched page and dealers outside 20 miles; each Fo failure (error, timeout, unreadable, acted, too few) falls back to Claude; the fetcher only GETs and refuses local hosts (`tests/test_discovery.py`). |
| `docs/api.md` | **Change** | v0.2 optional fields + v0.3 ceiling endpoint and discovery (section 2). Done. |
| `mock/build_mock.py`, `mock/events.json` | **Rewrite** | Car scenario; offers only target or null; includes a call nudge, a spoken price (`firm: false`), a `fo_preview` guard check. |
| `mock/check_mock.py` | **Change** | Also assert `strategy.decided.offer ∈ {target, null}`; allow the new optional fields. |
| `mock/ceiling.json`, `mock/discovery.json` | **New (done)** | Sample responses for `GET /jobs/{id}/ceiling` and `POST /dealers/discover`. `check_mock.py --secret 32000` confirms no event contains the mock ceiling (same value as the sim run). |
| `docs/sealed_module_io_flow.png` | Keep, marked superseded | README points to `architecture.md`. |
| `docs/fo_tools.md` | Keep, stale | Superseded by `docs/research/fo_mcp.md` (5 tools, output schemas). |
| `spikes/*` | Keep | Throwaway; nothing imports them. |
| `frontend/` | **New** | Dashboard: intake (car, ZIP, target, ceiling), dealer picker (discovered or typed), target + ceiling display, live dealer view, approval gate, receipt. |

## 2. API / event changes for the dashboard (nothing removed or renamed)

| Event / field | Change |
|---|---|
| `strategy.decided.offer` | Always equals the job's `target`, or `null`. |
| `guard.checked.stage` | New optional: `"pre_send"` \| `"fo_preview"`. A message can have two guard checks. |
| `message.sent.channel` | New optional: `"email"` \| `"call"`. `kind` unchanged. For calls, `body` holds the sealed brief and `subject` is null. |
| `quote.parsed.source` | New optional: `"email"` \| `"call"`. A spoken price arrives with `firm: false`, `source: "call"`. |
| `reply.received.source` | Adds `"call"` (Fo's call summary) next to `vendor_email` / `fo_relay`. |
| Mock scenario | One car: `spec.quantity: 1`, `spec.details` = model/trim/color; all prices are out-the-door totals. |
| `GET /jobs/{id}/ceiling` (v0.3) | New. The only place the ceiling is returned; the dashboard shows it next to the target, keeps it in memory only. Never in `JobView` or events. |
| `POST /dealers/discover` (v0.3) | New. Car + ZIP + `radius_miles` (20 only in v1) → suggested dealers with distance and source links. Contacts no one. |
| `POST /jobs` (v0.3) | Optional `zip`; per vendor optional `phone` and `source`. Each vendor needs an email or a phone. |

**Compatibility check (done):** the envelope and every existing field are unchanged, so a dashboard built on the current `docs/api.md` and `mock/events.json` keeps working. One semantic difference: the current mock's round-2 counter has `offer: 357`, which the new rule forbids (`offer` must be the target, 330, or null). The dashboard should not assume a counter offer differs from the target. The regenerated car mock fixes this.

## 3. Build order (estimates; Saturday ~14:00 ET → Sunday ~19:00 ET freeze)

| # | Step | Estimate | Target window | Done when |
|---|---|---|---|---|
| 0 ✅ | Car mock + `check_mock.py` rule update (unblocks the dashboard) | 1 h | Sat 14:00–15:00 | `check_mock.py` green on the car run |
| 1 ✅ | Ledger + strategy (fixed rounds) + qualifier + templates + call scripts + guard, with unit tests, runnable end-to-end in **sim mode** | 5 h | Sat 15:00–20:00 | A sim job produces the full event stream; ceiling-grep test green |
| 2 | Fo adapter: serial queue, one conversation per dealer, preview check, `get_fo_reply` polling, dedupe | 3 h | Sat 20:00–23:00 | One live email to rfb5381@psu.edu through the adapter (after a team member approves) |
| 3 | Reply ingestion (Gmail IMAP) + quote parser + validation + post-send audit | 4 h | Sun 08:00–12:00 | A test reply-all from rfb5381@psu.edu becomes a ledger quote; audit diff recorded |
| — | **Decision: real dealers vs stand-ins** | — | **Sun 12:00** | |
| 4 | Calls: round-1 nudge to non-responders, round-2 call, through the same adapter and queue; call summary → `reply.received` (source `call`) | 2 h | Sun 12:00–14:00 | One call to a team member's own phone through the adapter |
| 5 | SSE + routes matching `docs/api.md` (incl. `GET /jobs/{id}/ceiling`); dashboard switches from mock to live | 3 h | Sun 14:00–17:00 | Dashboard shows a live sim run with target + ceiling displayed |
| 6 | Dealer discovery: `POST /dealers/discover` (Fo first, Claude backup, see below; code check + distance check, 20 mi) + dealer picker in the dashboard | 3 h | Sun 17:00–18:00 | Search for the demo car near the demo ZIP returns verified dealers within 20 miles |
| — | End-to-end run, README, tests green, freeze | 1 h | Sun 18:00–19:00 | Freeze |
| later | Radius options 10 / 50 miles (API + dashboard selector), only after the core flow and dashboard are built | 0.5 h | after freeze | `radius_miles` accepts 10, 20, 50 |
| — | Demo video + X post draft | — | Sun night | Posted by Mon 10:15 ET |

The dashboard can be built against the mock (`events.json`, `ceiling.json`, `discovery.json`) from step 0 onward; SSE wiring lands in step 5.

### Step 6: who finds the dealers (decided: B first, A as backup — decision 16)

Both searches end in the same code check (our own fetch of the pages), and neither ever gets the ceiling or the target. Claude runs only if Fo fails, times out, is unreadable, appears to have acted, or leaves fewer than 3 verified dealers.

| | Option A: Claude (backup) | Option B: Fo (primary) |
|---|---|---|
| How | Claude Messages API with `web_search` + `web_fetch` server tools, structured output: dealers of the exact car near the ZIP, with name, website, sales email, phone, address and the pages each came from. | One `ask_fo` turn in a **new** conversation: "Find franchise dealers for [exact car] within 20 miles of ZIP [zip]. For each, give the name, website URL, sales email and phone. Don't contact anyone." Fo returns free text; code extracts the fields. |
| Code check | Same as B: code fetches the pages itself (one check path for both); distance from the address ZIP with an offline ZIP table (`pgeocode`); drop outside 20 mi. | **Code fetches each URL itself** (plain HTTP GET, read-only) and keeps an email / phone / address only if it appears word for word on that page; same distance check; drop outside 20 mi. |
| Pros | Structured output; the page text comes back in the same response; one API key we already plan for the parser. | Uses Fo (the hackathon's tool); no extra prompt engineering for tools; Fo may know local dealers well. |
| Cons / risks | Web search has no real radius (the distance check fixes that). | Free text (needs parsing); slower (~20–50 s per turn, serial queue shared with sends); Fo might act beyond "find" (the prompt says contact no one, and the turn is checked like any other: a reply that suggests it contacted someone raises `needs_human`); Fo remembers across conversations, so the request must contain only the car and ZIP. Our own fetcher must handle robots/blocked pages and never submit forms. |
| Status | Built as the backup (`claude_finder.py`). | Built as the primary (`fo_finder.py`). |

## 4. Cut list (in cut order)

1. **Dealer discovery** → the user types dealers in by hand (the existing `POST /jobs` path).
2. **Live dealer round** → fall back to stand-in dealers (labelled on screen).
3. **Round-2 calls** → keep the round-1 call nudge.

The **Gmail post-send audit is not on the cut list**: spike 1c showed Fo sends without a preview and its conversation doesn't contain the sent email, so the CC copy in Gmail is the only check of what Fo actually sent.

Never cut: ceiling isolation (qualifier only inside the backend; never in events, Fo or LLMs), local-only API (loopback bind, one localhost CORS origin), Gmail post-send audit, pre-send guard, written-quotes-only rule, approval gate, live/sim labels.

## 5. Demo checklist

- [ ] **Sunday noon:** decide real dealers (only with real purchase intent for that exact car) vs stand-in dealers.
- [ ] If stand-ins: 3 consenting people with real inboxes (one is rfb5381@psu.edu), scripted replies (clean total; MSRP + "fees extra"; late reply that revises down). **At least one stand-in reachable by phone** so the demo shows a call.
- [ ] Demo Wajo account: no payment card connected; no budget ever mentioned to Fo in any chat.
- [ ] Every send and call approved by the user or a team member; test calls only to a team member's own phone.
- [ ] Polite decline sent to every non-winner.
- [ ] If discovery is shown with stand-in dealers: run the search live, then label clearly that the run uses stand-ins.
- [ ] The ceiling is visible on screen in the video (the user's own view); confirm 0 ceiling mentions in every message and event.
- [ ] Trust counter on screen: messages checked before/after, 0 ceiling mentions, 0 commitments without approval.
- [ ] **Record the demo video Sunday night**; post on X by Mon 10:15 ET.

## 6. Status

- Step 0 done (`e38de1f`).
- Step 1 done: `sealed/core` (models, ledger, strategy, guard, events, engine), `sealed/private` (vault, secret checker, qualifier), `sealed/sim` (stand-in dealers, transport, runner), templates + call scripts in `sealed/fo/messages.py`; 46 tests. `mock/events.json` is now generated by the sim engine (`mock/build_mock.py`). Qualifier rule: a dealer flagged for a suspicious instruction is proposed only if no unflagged firm quote qualifies; the flag is shown either way.

- v0.3 (additive): itemised price breakdowns on `quote.parsed` (grounded in the dealer's text, sum-checked within $1, `breakdown_mismatch` flag), `message.sent.numbers` (source of every number), `job.completed.fee_comparison`. Mock dealers itemise; Harbor's first quote has a junk add-on (nitrogen-filled tires), Summit's parts don't add up.
