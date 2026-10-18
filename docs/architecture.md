# Sealed — architecture ("sealed envelope")

Status: decided, 2026-10-17; updated the same day for v0.3 (ceiling shown on the user's dashboard, dealer discovery). Supersedes `docs/sealed_module_io_flow.png`. API and event contract: `docs/api.md` (v0.3); changes are listed in section 8.

Launch scenario: buying one specific car from several dealers (see `docs/research/problem.md`).

## 1. The idea

Fo does the outreach on its own channels: **email and phone calls**. Plain, deterministic code decides every fact that goes out, in an email or on a call. **Nothing that leaves Sealed is ever computed from the ceiling.** Outgoing prices are only:

- a dealer's own quote,
- the lowest written competing quote (true, from the ledger), or
- the user's **target**, which is public, fixed when the job is created, and never moves.

So the ceiling cannot leak through what we send or say, even indirectly ("offer drift"). Inside the backend it is used in exactly one place: after a fixed number of rounds, a small qualifier decides whether the best firm quote is good enough to *propose to the user*, or whether to raise `job.needs_human`. That decision is output only as "proposal" or "needs human", never as a number or a gap.

The user can **see their own ceiling and target on the dashboard**. The ceiling reaches the dashboard through one private endpoint (`GET /jobs/{id}/ceiling`), never through events, so the event log and everything sent to Fo stay ceiling-free. Fo, dealers and every LLM never see it.

Dealers can be **typed in by the user or discovered**: Claude (web search + web fetch only) suggests dealers for the car near the user's ZIP, code keeps only those within the radius (20 miles in v1) whose contact details appear word for word on a fetched page, and the user picks up to 5. Discovery contacts no one.

Calls are a core channel with two jobs: nudge silent dealers to send a written quote, and carry the true lowest written total to a dealer in round 2. **A spoken price is never a firm quote**; only written replies enter the ledger as firm.

The secret checker and pre-/post-send checks stay as defence in depth.

## 2. System diagram

```mermaid
flowchart TB
    subgraph UI["Dashboard — the user's own screen"]
        INTAKE["Job intake<br/>car spec, ZIP, target, ceiling, dealers"]
        PICK["Dealer picker<br/>discovered suggestions or typed in"]
        SHOW["Target + ceiling display<br/>ceiling via private endpoint only"]
        APPROVE["Approval gate<br/>user taps exact terms"]
    end

    subgraph DISC["Dealer discovery — never sees ceiling or target"]
        FIND["Claude: web search + web fetch<br/>no other tools"]
        DCHECK["Code checks<br/>details on fetched page; ZIP distance ≤ radius (20 mi)"]
    end

    subgraph PRIVATE["Private zone (backend only)"]
        VAULT[("Ceiling<br/>shown only on the user's dashboard")]
        QUAL["Qualifier<br/>propose or needs_human"]
        SECRET["Secret checker<br/>text in, yes/no out"]
    end

    subgraph CORE["Sealed core — never sees the ceiling"]
        STRAT["Strategy engine<br/>fixed rounds; target + ledger only"]
        BUILD["Message builder<br/>email templates + call briefs<br/>in Fo's voice, ledger facts only"]
        GUARD["Pre-send guard<br/>rules incl. secret checker"]
        FOAD["Fo adapter<br/>one conversation per dealer, serial queue"]
        INGEST["Reply ingestion<br/>Gmail IMAP (dealer's own email)<br/>+ Fo call summaries"]
        PARSE["Quote parser<br/>LLM → fixed record, code validates"]
        LEDGER[("Quote ledger<br/>firm = written only")]
        AUDIT["Post-send audit<br/>sent copy vs ours"]
    end

    subgraph EXT["Outside"]
        FO["Fo (Wajo MCP)"]
        DEALERS["Dealers"]
    end

    INTAKE -->|"car, ZIP, radius"| FIND
    FIND -->|"suggested dealers + source pages"| DCHECK
    DCHECK -->|"verified suggestions"| PICK
    PICK -->|"chosen dealers (max 5)"| STRAT
    INTAKE -->|ceiling| VAULT
    VAULT -.->|"GET /jobs/{id}/ceiling (display only)"| SHOW
    INTAKE -->|"car spec, target"| STRAT
    STRAT -->|"decision: rfq / firm_up / price_match / counter@target / booking, channel email or call"| BUILD
    BUILD --> GUARD
    GUARD <-->|"yes/no"| SECRET
    VAULT -.-> SECRET
    GUARD -->|passed text only| FOAD
    FOAD <-->|"ask_fo, Fo's confirmation preview, get_fo_reply"| FO
    FOAD -->|"preview text"| GUARD
    FO -->|"email from Fo's address, CC user"| DEALERS
    FO -->|"phone call, Fo's own words within a sealed brief"| DEALERS
    DEALERS -->|"written reply (reply-all)"| INGEST
    FO -->|"voice transcript, post-call review (spoken price = not firm)"| INGEST
    INGEST --> AUDIT
    AUDIT <-->|"yes/no"| SECRET
    INGEST -->|"dealer's own words"| PARSE
    PARSE --> LEDGER
    LEDGER --> STRAT
    STRAT -->|"after fixed rounds: best firm quote"| QUAL
    VAULT -.-> QUAL
    QUAL -->|"proposal or needs_human"| APPROVE
    APPROVE -->|"approved terms"| STRAT
```

## 3. Who knows what

| Component | Ceiling | Target | Dealer quotes | Notes |
|---|---|---|---|---|
| Dashboard (the user's own screen) | **Yes, display only** (`GET /jobs/{id}/ceiling`) | Yes | Yes | Never in events/SSE; never stored in `localStorage` or URLs; never sent on in any request. |
| Dealer discovery (Claude) | **No** | **No** | No | Gets only car, ZIP, radius. Web search + web fetch, no send tools. Output is a fixed record checked by code. |
| Ceiling vault | **Yes** | — | — | Backend memory/env only. Not logged. |
| Secret checker | **Yes** | — | — | Text in, yes/no out. Defence in depth. |
| Qualifier | **Yes** | Yes | Best firm quote only | Outputs `propose` or `needs_human`. Runs once, after the fixed rounds. |
| Strategy engine | **No** | Yes | Yes | Fixed round plan; offers are only target or a real ledger quote. |
| Message builder | No | Yes | Ledger values | No LLM. Fo's voice. |
| Call brief | **No** | No | Only the lowest written total (round 2) | Goal + allowed facts + fixed hard limits; Fo talks in its own words inside it. Guarded before the call; transcript reviewed after. |
| Pre-send guard | No (asks checker) | Yes | Allowed values | Checks our email text or call brief **and** Fo's preview. |
| Fo adapter / Fo | No | Only if a counter is sent | Only what a message or brief states | One Fo conversation per dealer. Fo has durable memory (CLAIMED), so nothing secret is ever given to it. |
| Quote parser | No | No | Yes | Untrusted input → fixed record. |
| Each dealer | No | Only if countered | Its own + best competing total (no dealer name) | All true. |

## 4. Round plan (fixed, never depends on the ceiling)

| Round | Who | Channel | Message kind | Content |
|---|---|---|---|---|
| 1 | every dealer with an email | email | `rfq` | Exact car (model, trim, color), zip, timeline, ask for a written **out-the-door** total. |
| 1 | phone-only dealers (no email on file) | **call** | `rfq` | "I'm Fo, calling for a buyer about [exact car]. Could you email the out-the-door total to [buyer's address]?" Counts as their call nudge. Later emails go to the address their first written reply came from. |
| 1 | dealers with no email reply after a fixed wait (e.g. 3 h, set at job creation) | **call** | `rfq` (call nudge) | Sealed brief. Goal: get a written out-the-door total emailed. Facts: car, ZIP, timeline, buyer's first name, "we emailed and have no reply yet". Once per dealer. |
| 1b | dealers whose written reply is not firm | email | `firm_up` | "Please send the out-the-door total including all fees and taxes." Once. |
| 2 | every dealer with a firm quote that is not the lowest | email | `price_match` | "Your total is $X. The lowest written out-the-door total so far is $Y. Would you like to revise?" |
| 2 | same dealers (optional, fixed rule: dealers that didn't answer the round-2 email after the wait) | **call** | `price_match` (call) | Sealed brief. Goal: tell them the lowest written total ($Y) and ask for a revised written total by email. No new numbers, no agreement. |
| 2 | the dealer holding the lowest quote | email | `counter` (optional) | "Could you do $target out-the-door?" Target is fixed; sent only if target < lowest quote. |
| end | — | — | qualifier | Best firm quote → `propose` or `needs_human`. |
| after approval | winner | email | `booking` | "The buyer would like to proceed at $Z. Please send the purchase paperwork." No payment through Fo. |
| after approval | others | email | `cancel` | Polite decline. |

Whether a call or round 2 happens never depends on the ceiling, so the timing of messages and calls reveals nothing. A price heard on a call is logged (`quote.parsed`, `firm: false`, source `call`) and triggers nothing until the dealer puts it in writing.

## 5. One send, step by step

```mermaid
sequenceDiagram
    autonumber
    participant S as Strategy engine
    participant B as Message builder
    participant G as Pre-send guard
    participant C as Secret checker
    participant A as Fo adapter
    participant F as Fo
    participant D as Dealer
    participant I as Reply ingestion
    participant P as Quote parser

    S->>B: decision (e.g. price_match, cite q_04, channel email or call)
    B->>G: email body or call brief, in Fo's voice
    G->>C: text
    C-->>G: yes/no
    G->>A: passed text only
    A->>F: ask_fo(newConversation) "send exactly this to D" or "call D within this brief"
    alt Fo replies with a preview or confirmation question
        F-->>A: preview text
        A->>G: check preview (same rules, diff vs ours)
        G-->>A: pass, answer "yes" / fail, stop and flag
    end
    Note over A,F: status "working" means poll get_fo_reply, never resend ask_fo
    alt email
        F->>D: email from Fo's address, user CC'd
        I->>I: CC copy found, post-send audit (sent copy vs ours, checker again)
        D-->>I: written reply-all
    else call
        F->>D: phone call in Fo's own words, inside the brief
        F-->>I: voice transcript via get_fo_conversation
        I->>I: post-call review, numbers outside brief, commitments, money topics
    end
    I->>P: dealer's own words only
    P->>P: fixed record, code checks sender, trim, out-the-door total
    P-->>S: written quote (firm or not) to ledger, spoken price logged as not firm
```

## 6. Trust boundaries

1. **Ceiling → anything outgoing.** No outgoing number is computed from it (by construction). The secret checker is a second line, before and after sending.
2. **Sealed → Fo.** Only template text and call briefs cross. Fo's preview is checked at the confirmation step; the sent email copy is audited after. Fo claims durable memory across conversations, so the rule "never mention a budget to Fo, in any form" covers every chat, test and manual use of the account.
3. **Dealers → Sealed.** Written replies and call summaries are untrusted. They become a fixed record validated by code and never reach Fo or any prompt that can act. `suspicious_instructions` raises a flag.
4. **Dashboard.** The ceiling crosses to the browser only on `GET /jobs/{id}/ceiling` (`no-store`). The dashboard never talks to Fo, so showing the ceiling there gives Fo no path to it. Shown on screen means visible to anyone looking at it (e.g. a recorded demo); that is the user's choice.
5. **Dealer websites → discovery.** Web pages are untrusted (could contain instructions or wrong details). Discovery has no tools that act, no ceiling, no target; code keeps an email/phone/address only if it appears literally on a fetched page, recomputes the distance from the address ZIP, and the user picks who is contacted.
6. **Calls.** A call **cannot leak the ceiling**: Fo never has it, and the brief contains only ledger facts. Fo runs the conversation in its own words inside a **sealed brief**: a goal (get a written total emailed), the only facts it may use, and fixed hard limits (say it's an AI assistant; no agreeing, reserving, ordering or paying; no payment details; no numbers outside the brief; no follow-ups; questions about spending or deposits get one fixed answer). The guard checks the brief before the call. After the call, the **post-call review** reads the voice transcript and flags any number Fo said that isn't in the brief, any commitment wording from Fo, and any mention of budget, deposit or payment by either side; flags raise `job.needs_human` (`call_review`). Only written quotes count; spoken prices stay `firm: false`. Call recording/consent is the user's responsibility under Wajo's terms; see `docs/threat_model.md`.

## 7. Fo-specific decisions (from spikes and the official spec)

- **Fo's voice.** Fo refuses to send text written as the user (spike 1b, VERIFIED). Templates and briefs say "I'm Fo, an AI assistant writing/calling on behalf of [buyer]".
- **One Fo conversation per dealer** (`newConversation: true` on the first send; store the id). Calls to that dealer go in the same conversation.
- **Serial send queue.** One `ask_fo` turn at a time per account (spike + spec). Calls and emails share the queue.
- **No duplicate sends or calls.** On timeout or `working`, poll `get_fo_reply`. Never resend `ask_fo` for the same `message_id`.
- **Replies.** Gmail IMAP is the source of truth for written replies: `get_fo_conversation` on the email-channel conversation created by a vendor reply returned **0 turns** (VERIFIED twice). Fo CLAIMS replies "arrive back in this thread"; spike 1c watches the per-dealer conversation to test that. If it holds, it becomes a second read path, not a replacement.
- **Webhook.** A `conversation.reply` webhook is DOCUMENTED in the separate Wajo agent MCP (`https://mcp.api.wajo.ai/mcp`, `register_webhook`), needs a public HTTPS URL, and carries Fo's final message (max 2,000 chars), not the dealer's raw email. Not used; polling stays.
- **Token.** `auth.py` refreshes before expiry; refresh tokens rotate and are saved immediately.

## 8. Changes vs the earlier design and API

| Area | Earlier | New | Breaking for the dashboard? |
|---|---|---|---|
| Ceiling use | Strategy engine sees it; counters move toward it | Only the qualifier sees it; counters fixed at target | No |
| `strategy.decided.offer` | Any number | Always equals `target` (or null) | No (same field) |
| `guard.checked` | One per message | Can appear twice per message; add optional `stage: "pre_send" \| "fo_preview"` | No (new optional field) |
| `message.sent` | Email only | Add optional `channel: "email" \| "call"`; `kind` unchanged; for calls `body` holds the sealed brief | No (new optional field) |
| `job.needs_human` | — | New reason `call_review` with `flags: [{flag, speaker, excerpt}]` after a call | No (new value) |
| `quote.parsed` | Written replies | A spoken price arrives with `firm: false` and `source: "call"` | No (new optional field) |
| Message voice | "Hi …, Thank you, Het" | Written as Fo on the buyer's behalf | No |
| Scenario | Hoodies | One car, `spec.quantity: 1`, `spec.details` = model/trim/color; prices are out-the-door totals | Mock data only |
| `booking` | Order the item | "Proceed, please send paperwork" (no payment) | No |
| Dashboard | Never sees ceiling | Shows the user's own ceiling and target; ceiling fetched from `GET /jobs/{id}/ceiling` only, never in events | No (new endpoint) |
| Dealers | Typed in by the user | Typed in, or discovered (`POST /dealers/discover`, 20-mile radius in v1; 10/50 later) and picked by the user | No (new endpoint; optional `zip`, vendor `phone`, `source`) |

Everything else in `docs/api.md` stays as is.

### Dealer discovery, step by step

1. User enters car, spec and ZIP (radius fixed at 20 miles in v1).
2. Backend asks Claude (Messages API, `web_search` + `web_fetch` server tools, structured output) for franchise dealers of that make near the ZIP: name, website, sales email, phone, street address, and whether the exact trim/color is listed in stock, each with the page it came from. The prompt contains no ceiling and no target.
3. Code checks Claude's record against the page text returned in the `web_fetch` results of the same response: each email/phone/address must appear literally on the page Claude cited, or that field is dropped.
4. Code computes distance from the user's ZIP to the address ZIP with an offline ZIP-to-location table (e.g. `pgeocode`) and drops dealers outside the radius. Dealers with neither email nor phone are dropped.
5. Dashboard shows the list with distance and source links, labelled "found by AI, check before contacting". The user ticks up to 5 (and can still add dealers by hand). Then the normal flow starts.

Radius options **10 and 50 miles** are added after the core flow and dashboard are built; until then the API accepts only 20.

## 9. Open checks before building

- Discovery: how many local dealers publish a sales email (vs contact forms only)? Dealers without one can only be called.

- ~~A1: does `get_fo_conversation` on an `email` conversation include the dealer's reply?~~ No (0 turns, VERIFIED). Gmail IMAP is the read path.
- Spike 1c: does Fo send a Fo-voice body verbatim, does the preview contain the full draft, and does the reply show up in the per-dealer Fo conversation (Fo's claim)?
- Spike 1d: does a text request make Fo place a call, does it stay on the script (done: yes, mostly; see spike 1d), and does a transcript or summary come back through MCP?
- `working` + `get_fo_reply` path: not yet observed (all turns so far returned `complete` in one call).
