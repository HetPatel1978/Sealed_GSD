# Sealed — decisions (short ADRs)

Status: 2026-10-17. Each entry: decision, alternatives, why. Labels as in `docs/research/fo_mcp.md`.

## 1. Cars over moving
- **Decision:** launch scenario is buying one specific car from several dealers.
- **Alternatives:** moving (scored 27 vs 26), contractors, print/merch.
- **Why:** car quotes are directly comparable (same model, trim, color, out-the-door total), so "compete using only true facts" is easy to show and defend. Moving quotes are often non-binding estimates with no sound basis for comparison (`docs/research/problem.md`).

## 2. Hybrid: code decides facts, Fo does email + calls
- **Decision:** deterministic code decides every fact and price that goes out; Fo only delivers it, by email and by phone.
- **Alternatives:** let Fo negotiate end to end with a brief; an LLM negotiator that writes its own messages.
- **Why:** an agent that writes its own messages can leak or invent facts; plain code can be tested and audited. Fo's strength is reaching dealers on real channels, including the phone.

## 3. Ceiling only in the qualifier
- **Decision:** only the qualifier (and the yes/no secret checker) reads the ceiling. It runs once, after the fixed rounds, and outputs `propose` or `needs_human`.
- **Alternatives:** strategy engine uses the ceiling to decide accept/counter (v0.1 plan).
- **Why:** if no outgoing decision depends on the ceiling, nothing outgoing can leak it, directly or by behaviour.

## 4. Counters fixed at the target
- **Decision:** the only price we ever ask for is the public target, fixed at job creation.
- **Alternatives:** step counters between target and best quote (`T + (B − T) × k`).
- **Why:** stepping counters drift toward the ceiling and reveal it over rounds. A fixed target reveals nothing new.

## 5. Fixed number of rounds
- **Decision:** the round plan (round 1 + call nudge + firm-up, round 2 + optional call, then qualifier) is the same for every job.
- **Alternatives:** keep negotiating until a quote is under the ceiling.
- **Why:** stopping when "good enough" tells dealers where the ceiling is. Fixed rounds keep timing uninformative.

## 6. Templates in Fo's voice
- **Decision:** all emails and scripts say "I'm Fo, an AI assistant writing/calling on behalf of [buyer]".
- **Alternatives:** first-person text signed by the user.
- **Why:** Fo refused to send text written as the user (spike 1b, VERIFIED). Open AI disclosure is also the honest default.

## 7. Check Fo's preview before saying yes
- **Decision:** when Fo asks for confirmation, the pre-send guard checks the preview text (same rules + diff vs ours) before we answer "yes"; otherwise stop.
- **Alternatives:** always answer yes; never answer (manual confirmation).
- **Why:** after the confirmation, Fo sends without asking; the preview is the last point to catch a rewrite before it reaches a dealer. Post-send audit covers the rest.

## 8. Calls as nudges and fact carriers; written quotes only
- **Decision:** calls (a) nudge silent dealers to email a quote, (b) in round 2 state the true lowest written total and ask for a revised written quote. Spoken prices are never firm.
- **Alternatives:** no calls; let Fo negotiate on calls.
- **Why:** many dealers answer the phone faster than email, and calls are Fo's distinctive skill. But a spoken price can't be audited or cited, and Fo works "from a brief, not a rigid script" (CLAIMED), so nothing said on a call can commit or count.

## 9. One Fo conversation per dealer + serial queue
- **Decision:** `newConversation: true` on first contact; that dealer's emails and calls stay in it. One `ask_fo` turn at a time.
- **Alternatives:** one conversation for the whole job; parallel turns.
- **Why:** keeps each dealer's context separate (no cross-dealer mix-ups); the spec and spike 1b show turns are effectively serialized per account anyway.

## 10. Poll `get_fo_reply`, never resend
- **Decision:** on timeout or `working`, poll `get_fo_reply`; never repeat `ask_fo` for the same message id.
- **Alternatives:** retry `ask_fo`.
- **Why:** `ask_fo` is annotated not idempotent (VERIFIED); a retry is a second email or call.

## 11. Replies via Gmail IMAP, not Fo's email conversation
- **Decision:** read dealers' written replies from the user's Gmail (they reply-all; the user is CC'd).
- **Alternatives:** `get_fo_conversation` on the email-channel conversation; the `conversation.reply` webhook.
- **Why:** the email-channel conversation a reply creates has **0 turns** (VERIFIED twice). The webhook (DOCUMENTED on the separate agent MCP) needs a public URL and carries Fo's message, not the dealer's raw email. Fo CLAIMS replies come back into the original thread; if spike 1c confirms it, that becomes a cross-check, not the source of truth.

## 12. Real dealers only with real purchase intent; else stand-in dealers
- **Decision:** contact real dealers only if someone on the team is genuinely buying that exact car; otherwise run a clearly labelled round with consenting stand-in dealers.
- **Alternatives:** send real quote requests with no intent to buy.
- **Why:** asking for quotes with no intent breaks Sealed's own "true facts only" rule and wastes dealers' time.

## 13. The user sees their own ceiling and target on the dashboard
- **Decision:** the dashboard shows the target and the ceiling the user set. The ceiling comes from one private endpoint, `GET /jobs/{id}/ceiling` (`Cache-Control: no-store`), and is never in `JobView`, events, the SSE stream or logs. Fo, dealers and all LLMs still never see it.
- **Alternatives:** keep the ceiling write-only (v0.2); dashboard remembers what was typed (lost on refresh); put the ceiling in `JobView`/events.
- **Why:** the user should be able to see what they set. The dashboard never talks to Fo, so this opens no path to Fo. A separate endpoint keeps the event log ceiling-free, so the mock checker and the backend's ceiling-grep test still mean something. Any "best quote vs ceiling" comparison is computed in the browser only.

## 14. Dealer discovery: Claude only, plus a code distance check, 20-mile radius
- **Decision:** `POST /dealers/discover` asks Claude (web search + web fetch server tools, no other tools, no ceiling or target) for dealers of the car near the user's ZIP. Code keeps a contact detail only if it appears word for word on a fetched page, recomputes the distance from the address ZIP with an offline ZIP table, and drops dealers outside the radius. The user picks up to 5; discovery contacts no one. The radius is **20 miles** in v1; **10 and 50 miles** are added after the core flow and dashboard are built.
- **Alternatives:** a maps/places API (e.g. Google Places) for the list plus Claude for websites; Claude web search with no distance check; contact discovered dealers automatically.
- **Why:** no new account or key (the Anthropic key is already planned for the quote parser), less code before the freeze, and the distance check fixes web search's main weakness (no real radius). Contacting automatically would break the 5-dealer cap, the approve-every-send rule and decision 12 (real dealers only with real purchase intent). A places API can replace the search later if completeness matters.
