# Wajo and Fo — public research (Phase A2)

Researched 2026-10-17. Labels: **DOCUMENTED** (independent source), **CLAIMED** (Wajo says it about itself), **ASSUMED** (our judgment). Nothing here is VERIFIED by us; for that, see `fo_mcp.md` (A1, A3, A4 still to run).

---

## 1. Hackathon facts that change our plan

| Fact | Source | Label |
|---|---|---|
| **Submission: post the project on X by 10:15am on Mon Oct 19.** Our brief says "Mon night". | [Luma](https://luma.com/vwbfvso8) | DOCUMENTED |
| Judged on technical, creative and **social reach**. | Luma | DOCUMENTED |
| Prizes $1,000 / $500 / $300; top hackers get a shot at an internship or full-time role. | Luma | DOCUMENTED |
| Page says "Plug into the Fo MCP server **and API**". The official spec shows the only non-MCP endpoint is the live-voice join (`/v1/fo-calls/<ticket>:join`). The spec lists no webhooks, though the tool schema mentions a `conversation.reply` webhook. See `fo_mcp.md`. | Luma, hackathon spec | DOCUMENTED |
| Judges announced: Rida Mehdi (Applied AI, Anthropic) and Lukas Ingemarsson. Ingemarsson is a co-author of Wajo's trust paper. | [LinkedIn post](https://www.linkedin.com/in/rida-mehdi/), [Progressive Robot](https://www.progressiverobot.com/2026/09/29/wajo-launches-fo-ai-agent-calls-payments/) | DOCUMENTED |

**Action:** confirm the deadline in Discord today. If it's Monday 10:15am, code freeze Sunday evening still works, but the X post and video must be ready Sunday night.

---

## 2. Company

| Fact | Source | Label |
|---|---|---|
| San Francisco startup; founder Shivani Poddar (previously Meta, Google, DeepMind). | [Dealroom](https://dealroom.co/news/160588-vinod-khosla-bets-on-ex-deepmind-engineers-agent-startup-wajo-citing-tru/) | DOCUMENTED |
| Backed by Vinod Khosla; Jeff Dean also invested. | Dealroom | DOCUMENTED |
| Alpha launched Aug 25, 2026; public Sept 28, 2026. | [wajo.ai](https://wajo.ai/blog/unveiling-wajo/), Progressive Robot | DOCUMENTED |
| About 9 people. | Progressive Robot | DOCUMENTED |
| Used in 107 countries, grew 10x since launch (no user numbers). | Dealroom | CLAIMED |
| Positioning: trust and privacy first, against ad-driven incumbents (Meta's Muse, Instinct). | Dealroom, Khosla quote | DOCUMENTED |

---

## 3. What Fo is and can do

| Capability | Source | Label |
|---|---|---|
| Own email address, own phone number, own voice, single-use virtual cards. | [wajo.ai launch](https://wajo.ai/blog/unveiling-wajo/), [Crypto Briefing](https://cryptobriefing.com/vinod-khosla-backs-wajo-fo-ai-agent/) | CLAIMED / DOCUMENTED |
| Users reach Fo by text, iMessage, WhatsApp, email, group chat, and via MCP from Claude/ChatGPT. | Progressive Robot, Dealroom, [trendingaitools](https://www.trendingaitools.com/?p=45165) | DOCUMENTED |
| Calls businesses, including in the other party's language (since Sept 14). | Progressive Robot | CLAIMED |
| Calls are **recorded**; terms make the user responsible for recording-consent laws. | Terms §5.9 via Progressive Robot | DOCUMENTED |
| Hands hard tasks to trained human assistants, at Wajo's cost. | Progressive Robot | CLAIMED |
| Keeps "do not contact me" requests as a lasting rule. | [ecosistemastartup](https://ecosistemastartup.com/?p=115297) | DOCUMENTED (secondhand) |
| Learns about the user over time once Google Workspace is connected. | [Product Hunt](https://www.producthunt.com/products/wajo) (founder reply) | CLAIMED |
| Founder's own example of the vision: a leaking sink → Fo calls five plumbers, compares them, negotiates, books the best. | wajo.ai launch | CLAIMED |
| Payments: four routes depending on merchant; without spending controls Fo may act within the user's "general authorization". | Terms via Progressive Robot | DOCUMENTED |
| Plans: free tier with daily limit, invite-only unlimited tier, Pro on request. | Progressive Robot | DOCUMENTED |

---

## 4. Wajo's trust paper — the part that matters most for Sealed

Source: [wajo.ai/blog/trust-and-task-completion](https://wajo.ai/blog/trust-and-task-completion/) (CLAIMED; independent breakdown at Progressive Robot).

- Simulated world of invented businesses with inboxes and phone lines; every trap has a matched control.
- Fo completed 71% of errands alone; 94% trust on trap runs (10 violations in 172).
- Without guardrails the same model still **decides** to send unrequested emails; the guardrail holds the message and asks the user. It also strips private details (a phone number, a relative's diagnosis) before sending.
- The harness takes a vendor's offer to the user instead of accepting it just because it fits the budget.
- Admitted limits: invented world, model grader not checked against humans.

**Why this matters:**
1. It matches what we VERIFIED: Fo has added content and sent unrequested messages. Wajo's own data says the *model* does this and the *guardrail* catches it. We should not assume the guardrail will catch "the ceiling is $X" — a budget isn't on their list of private details. Our own pre-send guard stays.
2. Sealed speaks Wajo's language: trust measured on the outcome, not on the agent's story. A dashboard counter of "messages sent / ceiling mentions / unverified claims", checked by deterministic code on the actual outbound text, is the same idea as their "grade the final state of the world".

---

## 5. Unknowns (to check in A1 / Discord)

Answered by the official spec (see `fo_mcp.md`): the "API" is the live-voice join endpoint; no webhooks are documented (one is mentioned in the tool schema); turns are one-at-a-time per conversation by design.

Still open:
- Whether seller replies to Fo's mailbox show up via `list_fo_conversations` (channel `email`) and `get_fo_conversation`.
- Whether Fo's outbound calls to businesses, started by a text request, return a transcript.
- Rate limits.

---

## 6. What Wajo's engineers would likely value (ASSUMED)

1. **Deep use of Fo's own identity and channels** — its inbox and phone, not just "Fo, send this email."
2. **Trust you can measure**, in their style: outcomes checked by code, with traps and controls (e.g., a seller reply containing an injection, a hedged price).
3. **The founder's own use case**, done properly — "call five sellers and negotiate" with honesty guarantees they can point to.
4. **Honest handling of failure**: what happens when a seller doesn't reply, Fo refuses, or Fo asks a question mid-turn.
5. **Feedback on the MCP surface**: a short list of what we needed and couldn't do (e.g., read replies, get a reply event). That is useful to a 9-person team shipping MCP.
6. **Social reach** is a judging criterion — the 60-second "Priya saved $X without revealing her max" story needs to work on X.
