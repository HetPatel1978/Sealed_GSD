# Sealed — decisions (short ADRs)

Status: updated 2026-10-18. Decision 16 supersedes the exact-email wording and delivery-only portions of decisions 2, 6 and 7. Each entry: decision, alternatives, why. Labels as in `docs/research/fo_mcp.md`.

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
- **Decision:** all emails and call briefs say "I'm Fo, an AI assistant writing/calling on behalf of [buyer]".
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

## 14. Dealer discovery: Fo first, verified public sources, 20-mile ZIP radius
- **Decision (updated 2026-10-18 at the user's request):** use the existing Fo MCP connection for public research before adding another search provider. Background discovery receives only item/specification, ZIP and radius. Code independently checks official-site contact evidence and approximate ZIP-centroid distance. Users select up to five sellers; discovery never starts the negotiation. See [implementation and limitations](dealer_discovery.md).
- **Changes from the earlier plan:** Fo replaces the proposed Claude web tools; public key-free Zippopotam.us ZIP lookup replaces an offline ZIP table. Fo is instructed to avoid outreach and all private/account actions, but its tools are not mechanically sandboxed by this integration. Stock remains unconfirmed. Distance is approximate, not an exact address/driving-radius guarantee.
- **Why:** the live Fo research probe worked, the existing account avoids another AI/search key, and independent source checks prevent treating generated contact details or distance estimates as facts. Completed results cache for one hour; uncertain attempts are never automatically resent.

## 15. Calls use a sealed brief, not a script (supersedes the "script" part of #8)
- **Decision:** a call gets a brief: a goal (get a WRITTEN out-the-door total, or a revised one, emailed), the only facts Fo may use (car, ZIP, timeline, buyer's first name; in round 2 the lowest written total from the ledger), and fixed hard limits (say it's an AI assistant; no agreeing, reserving, ordering or paying; no payment details; no numbers outside the brief; no follow-ups; spending/deposit questions get "The buyer is comparing written out-the-door totals"). Fo runs the conversation in its own words. The guard checks the brief before the call; a post-call review reads the voice transcript and flags numbers outside the brief, Fo commitments, and money topics → `job.needs_human`.
- **Alternatives:** a fixed word-for-word script (spike 1d); free-form instructions.
- **Why:** a phone call is two-way. A script breaks the moment a dealer asks a question, and Fo already improvises (spike 1d: it opened with "Hey Het! What's up?"; it says it works "from a brief, not a rigid script", CLAIMED). A brief matches how Fo actually works, while keeping what matters fixed: the facts it can state and the things it must never do. Because nothing on a call can commit the buyer or enter the ledger as firm, the remaining risk is wording, and that is caught after the call by the transcript review rather than prevented by a script Fo wouldn't follow anyway.

## 16. Fo composes email from briefs (2026-10-18; supersedes exact email wording in #6/#7)
- **Decision:** Sealed supplies the objective, routing, verified facts and explicit price roles for one outreach action. Fo composes the email and returns a complete draft before permission to send. Code checks recipient/CC/subject, disclosure, specification, permitted numbers and price roles, commitments and known unsupported claims/actions. Ordinary signatures and compliant rewording are allowed. The checked draft is persisted before confirmation; sending without a checked draft raises review and never triggers a resend.
- **Scope:** fixed rounds, permitted offers, private-ceiling isolation, verified written quotes, exact-term approval and no-payment rules stay in Sealed. This is conversational freedom within an authorized action, not blanket authorization for autonomous follow-ups. Existing exact-send records are not migrated, resent or automatically released from review.
- **Why:** Fo should handle natural wording while Sealed controls authority and financial facts. Semantic checks are conservative rules, not proof of arbitrary prose; unknown or ambiguous formats require review. Actual Gmail sent-copy evidence is still required. See `docs/fo_briefs.md`.
