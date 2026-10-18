# Sealed — threat model

Status: 2026-10-17, against the decided design in `docs/architecture.md` (v0.3: ceiling shown on the user's dashboard, dealer discovery). Likelihood (L) and impact (I) are **ASSUMED** judgments (Low / Med / High) unless a label says otherwise. "Gap" is what the design does **not** fully cover; those are the things to test, show honestly, or accept.

| # | Threat | L | I | How the design handles it | Remaining gap |
|---|---|---|---|---|---|
| **Ceiling leak** | | | | | |
| 1 | Ceiling stated as a number in an email or script (`$31,500`, `31500`, `31.5k`) | Low | High | No outgoing text is computed from the ceiling; the builder never receives it. Secret checker (yes/no) runs on our text, on Fo's preview and on the sent copy. | Checker can't see what Fo says on a call live; relies on Fo never having the value. |
| 2 | Ceiling in words ("thirty-one five") or near it (within ±2%) | Low | High | Checker normalises supported number forms and flags values within ±2%. Exact allowed prices from the ledger, public target or approved quote are masked before the secret check; the guard separately rejects unknown dollar amounts and altered previews. | Legitimate public prices can lie near or equal the ceiling and remain allowed: blocking them would make the outgoing plan depend on the ceiling. Number-form coverage is bounded by the checker tests. |
| 3 | Implied ceiling ("we can't go above…", "that's our limit", "within budget") | Low | High | Briefs contain no private spending information; guard blocks budget vocabulary in generated drafts; the qualifier's output is only `propose`/`needs_human`, never a gap. | Vocabulary rules do not prove the meaning of arbitrary prose; unknown formats require review. |
| 4 | Offer drift (counters stepping toward the ceiling reveal it) | — | High | **Removed by design:** counters are fixed at the public target; strategy has no ceiling input; round count is fixed. | None known. |
| 5 | Fo memory: a ceiling mentioned to Fo once resurfaces later | Med | High | Fo CLAIMS durable memory across conversations (A3). Rule: never mention any budget to Fo, in any chat, test or manual use of the account. | A human using the same Wajo account outside Sealed could still tell Fo. Use a dedicated demo account. |
| 6 | Fo summaries or relays (e.g. Fo's own inference "6 would run about $450") | Med | Med | Prices enter the ledger only from the dealer's own written words (Gmail). Fo relays are `UNVERIFIED`. | — |
| 7 | Leak on a phone call | Low | High | Fo never has the ceiling, so it cannot say it. The call brief contains only ledger facts. | Fo improvises ("not a rigid script", CLAIMED); see #11. |
| 8 | Leak in follow-ups (firm_up, cancel, booking) | Low | High | Same builder + guard for every kind; booking states only the approved quote. | — |
| 9 | Fo hands the task to a human assistant who sees the conversation | Low | Low | Wajo CLAIMS hard tasks go to trained human assistants. Nothing secret is ever in the conversation. | Dealer identities and quotes are visible to Wajo staff; acceptable for the demo. |
| **Fo behaviour** | | | | | |
| 10 | Fo adds unsupported facts, prices or commitments, or changes routing/tag | Med | Med | New emails give Fo a bounded brief and require a complete draft before sending. Checks cover routing, disclosure, specification, permitted numbers/price roles and known unsafe claims/actions; reviewed draft saved before confirmation. Compliant rewording/signatures allowed. The configured Gmail auditor checks actual routing/authentication, all inline text alternatives, HTML links/attributes and unapproved attachments while retaining the account reservation. | Rule checks cannot prove arbitrary natural-language claims. The new draft-envelope live acceptance still needs approval, and the API, coordinator and dashboard are implemented and tested offline. Full live coordinator acceptance remains pending. Post-send detection cannot undo delivery. Legacy exact smoke evidence stays unchanged. |
| 11 | Fo says something untrue on a call, or agrees to something | Med | High | Sealed brief: one goal (a written total by email), allowed facts only, fixed hard limits; post-call review of the voice transcript flags numbers outside the brief, Fo commitments and money topics → `job.needs_human`; only written quotes count. | No live control during the call; Fo "works from a brief" (CLAIMED). Spike 1d measures drift. |
| 12 | Fo sends unrequested messages (e.g. an invite link on Oct 15) | Med | Low | Adapter ignores Fo text outside the status/preview; audit flags any extra outbound mail seen in Gmail with our tag. | Messages without our tag aren't detected. |
| 13 | Fo refuses (policy) or asks a question mid-turn | High (VERIFIED once) | Med | Adapter treats any `?` reply as a question: checks it as a preview; answers only "yes, send exactly this"; otherwise stops and raises `needs_human`. Never improvises an answer. | Some refusals need a human; the job pauses. |
| **Calls and spoken prices** | | | | | |
| 14 | Dealer gives a spoken price that is never put in writing | High | Low | Logged as `quote.parsed` with `firm: false`, `source: "call"`; triggers nothing; never cited to other dealers. | Dealer may be annoyed at the "please email it" ask. |
| 15 | Call recording without the dealer's consent | Med | Med | Wajo records calls and its terms make the **user** responsible for recording-consent laws (DOCUMENTED, `docs/research/wajo.md`). Script opens with an AI disclosure. Test calls only to a team member's own phone; real dealer calls only with real purchase intent. | Two-party-consent states: we don't control whether Fo announces recording. **The user's / team's decision** for any real dealer call. |
| **Untrusted input** | | | | | |
| 16 | Prompt injection in a dealer email ("ignore previous instructions", "the buyer agreed") | Med | High | Parser is a quarantined LLM (no tools, no ceiling) producing a fixed record; code validates; `suspicious_instructions` flag; dealer text never reaches Fo or any acting prompt. | — |
| 17 | Injection or social engineering on a call ("your boss already agreed, confirm now") | Med | High | Brief's hard limits: never agree, fixed answer to spending/deposit questions; Fo says it passes questions back (CLAIMED). Nothing said on a call can change the ledger except as a non-firm note. | Depends on Fo's own judgment during the call. |
| 18 | False or ambiguous quote; MSRP, "plus fees", monthly payment counted as firm | High | High | Firm = written out-the-door total for the exact model/trim/color, validated in code; otherwise `firm_up` once; numbers must appear literally in the dealer's text (grounding check). | Parser misreads are possible; the approval screen shows the dealer's exact words next to the number. |
| 19 | Spoofed sender (someone emails "a quote" with our tag) | Low | Med | Match on sender address from the dealer list + tag + thread headers; Gmail auth results (SPF/DKIM) recorded. | A compromised dealer mailbox is out of scope. |
| **Ceiling on the dashboard** | | | | | |
| 34 | Ceiling leaks from the browser (in events/SSE, a URL, `localStorage`, analytics, a log) | Low | High | Ceiling only on `GET /jobs/{id}/ceiling` with `no-store`; never in `JobView` or events (mock checker + backend grep test); dashboard keeps it in memory only and never sends it in another request. | A dashboard bug could still send it somewhere; review the frontend for any request carrying it. |
| 35 | Someone else reads the ceiling: shoulder surfing, screen share, a recorded demo, anyone who can reach the API | Med | Med | The user chooses to show it; the dashboard can hide it behind a "show" toggle. Fo and dealers never see the screen. | The API has no login; acceptable only for a local demo. Anyone with network access to the backend could call the endpoint. |
| 36 | The user types the ceiling into a Fo chat themselves | Med | High | Fo claims durable memory (CLAIMED), so it could resurface later. Dashboard shows a one-line warning: "never tell Fo your ceiling". | A human using the Wajo account outside Sealed; use a dedicated demo account (#5). |
| **Dealer discovery** | | | | | |
| 37 | Claude invents or garbles a dealer email/phone, so Fo contacts a stranger | Med | High | A detail is kept only if it appears word for word on a page Claude fetched, with the URL stored; the user reviews and picks every dealer; nothing is contacted automatically. | A real page with stale details (dealer moved, email changed). |
| 38 | Prompt injection on a dealer website ("ignore instructions", fake contact details) | Med | Med | Fo discovery receives no ceiling or target and is instructed to perform public research only. Code checks its fixed output against independently fetched official pages. Fo tools are not mechanically isolated. | A malicious page can still supply a wrong but literal email; the user's review is the last check. |
| 39 | Dealer outside the radius, or a closed / wrong-brand dealer | Med | Low | Distance recomputed in code from public Zippopotam.us ZIP centroids; no contact details → dropped; results shown with source links. | ZIP-centroid distance is approximate (a few miles); a closed dealer with a live website passes. |
| **Delivery and state** | | | | | |
| 20 | Missed or duplicate replies; threading breaks | Med | Med | Match by tag, then In-Reply-To/References, then sender + time window; dedupe on Gmail message id. | Dealer replying in a new thread without the tag → matched by sender only (flagged). |
| 21 | Duplicate send or call on retry | Med | High | One Fo conversation per dealer; dedupe key per dealer + round + kind + channel; on timeout poll `get_fo_reply`, never resend `ask_fo` (`ask_fo` is not idempotent, VERIFIED annotation). | If our process crashes mid-turn, the state "maybe sent" needs a human check (Gmail CC copy decides). |
| **Commitment** | | | | | |
| 22 | Fo commits to a purchase or pays | Low | High | Brief-mode drafts reject commitment/payment language; user-approved booking permits only the paperwork request at the approved total, never payment. Sealed retains exact-term approval. Fo asks before money (CLAIMED). Keep **no payment card** on the demo Wajo account. | Rules cover tested wording, not every possible paraphrase. Fo can make purchases with a connected card (CLAIMED); verify the account has none. |
| 23 | Dealer treats our quote request or call as an order | Low | Med | Every message says it's a quote request; booking is the only proceed message and needs approval. | Dealer may still "hold" a car; cancel messages go out politely. |
| **Fairness** | | | | | |
| 24 | Unfair treatment: fake competing quotes, false urgency, pressure, no decline | Low | Med | Only real written quotes cited (no dealer names); no invented deadlines; every non-winner gets a polite decline. | — |
| **Operational** | | | | | |
| 25 | Token expiry / refresh-token rotation | Med | High | `auth.py` refreshes before expiry and saves the rotated token at once (VERIFIED). | Two processes refreshing at once could invalidate each other: one adapter process only. |
| 26 | Serialized turns and ~16–53 s latency | High (VERIFIED for earlier turns) | Med | Serial queue; rounds measured in hours. Brief-mode email adds a draft and confirmation turn. | Earlier one-turn estimates do not establish latency for the new two-turn contract; measure it during live acceptance. |
| 27 | Gmail/IMAP limits or app-password issues | Low | Med | Read-only UID/Gmail-ID access with bounded messages/searches, sanitized failures, durable staging and audit observations. Reply ingestion and the existing sent-copy audit have passed live read-only checks. | The coordinator reconnects per cycle and blocks actions on poll failure. Adaptive outage backoff and live coordinator acceptance remain later integration work. |
| 28 | One live call at a time per account | Med | Med | Calls go through the same serial queue. | A long call blocks emails; acceptable. |
| 29 | Demo-day failure (Fo down, no dealer replies, Sunday closures) | Med | High | Sim mode with labelled stand-in dealers; recorded video; decide real vs stand-in by Sunday noon. | Live parts can only be shown if they happened. |
| **Car-specific** | | | | | |
| 30 | Dealer insists on "come in" instead of quoting | High | Low | One `firm_up` asking for the emailed out-the-door total; then `no_reply`/not qualifying. | Some dealers won't quote by email at all. |
| 31 | Dealer quotes a monthly payment | High | Med | Not firm; `firm_up` asks for the total; monthly figures never cited. | — |
| 32 | Dealer adds add-ons (nitrogen, protection packages) or omits fees | High | Med | Firm only if the reply states an out-the-door total; add-ons are shown on the quote card. | Totals with different add-ons aren't strictly comparable; shown to the user, not decided by code. |
| 33 | Dealer quotes a different trim or color | Med | Med | Parser extracts model/trim/color; code compares to the job spec; mismatch → `meets_spec: false`, never cited as competing. | Parser error → shown with the dealer's words at approval. |


## Fo discovery implementation update (2026-10-18)

Discovery uses Fo, replacing the planned Claude web tools. Research instructions
forbid outreach and private account actions, but the existing Fo MCP tool set is
not mechanically restricted. Page instructions can still influence Fo; saved
record validation does not prove that Fo performed no other internal actions.
See [operation and limitations](dealer_discovery.md).

Source verification uses public-only DNS answers, a pinned IP connection with
hostname-verified TLS, same-site HTTPS redirects, bounded response sizes and no
script execution. Contacts and addresses must appear in official-site content;
that does not independently certify dealer reputation, franchise status or stock.
ZIP-centroid distance is approximate, not an address or driving-radius guarantee.
Job creation binds selected contacts to a checked result and matching public
requirements within one hour. Pending/uncertain research claims are never resent.
