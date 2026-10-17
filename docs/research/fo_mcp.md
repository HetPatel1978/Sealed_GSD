# Fo MCP — what we know (Phase A)

Labels: **DOCUMENTED** = official Wajo hackathon spec (`fo-mcp-hackathon.md`, shared in Discord), unless another link is given. **VERIFIED** = we observed it. **CLAIMED** = Fo said it about itself. **ASSUMED** = our guess. **UNTESTED** = open question for A1.

A1 (live introspection), A3 (asking Fo) and A4 (spike findings) are in section 7.

---

## 1. Connection (DOCUMENTED)

- Endpoint `https://mcp.api.wajo.ai/fo`, Streamable HTTP.
- OAuth via browser sign-in with the Wajo account. Needs the **OG Fo** plan; Fo mini is refused.
- Everything is scoped to the signed-in account. Gmail, Calendar etc. are connected at wajo.ai, not through MCP.

## 2. Tools (DOCUMENTED)

| Tool | Input | Output | Notes for Sealed |
|---|---|---|---|
| `ask_fo` | `message`, `conversationId`?, `newConversation`? | `status`, `conversationId`, `response`?, `progress`?, `elapsedSeconds`? | Waits up to 45 s. Not idempotent: a retry is a second message. |
| `get_fo_reply` | `conversationId`?, `waitSeconds` (0–40) | same, `response` always set | **Use this for retries/timeouts instead of re-sending `ask_fo`.** |
| `list_fo_conversations` | — | up to 50 rows: `conversationId`, `title`, `channel`, `startedAt` | Channels include `chat`, `email`, `sms`, `whatsapp`, `telegram`, `slack`, `voice`. |
| `get_fo_conversation` | `conversationId` | conversation + last **40** text turns: `role`, `text`, `at`, `runId` | 40-turn cap matters for long rounds. |
| `start_fo_call` | — | `joinUrl`, `expiresAt` | Live voice between **the user and Fo**, not Fo calling a seller. |

Rules:
- `status` is `complete`, `working` or `failed`.
- `conversationId` and `newConversation` can't both be passed. Made-up ids error. Don't show ids to end users.
- Errors come back as tool results with `isError: true`.
- **Don't send another `ask_fo` while a turn is `working`**, except to answer a question Fo is waiting on. This is the documented form of the "serialized turns" we saw.
- Long tasks can take 10+ minutes and continue if the client disconnects.

## 3. Confirmation behavior (DOCUMENTED)

- Before emailing anyone other than the user, or calling anyone, Fo checks the user asked for it explicitly.
- If Fo is waiting on the user (a question, or confirmation before emailing/calling), `response` says so and the answer goes back through `ask_fo`.

**Implication:** every seller send can turn into a confirmation round-trip. Sealed must detect "Fo is asking for confirmation", show it, and answer — and the pre-send guard must check the text Fo is asking to send, not only the text we gave it.

## 4. Live audio (DOCUMENTED)

`start_fo_call` → `POST https://mcp.api.wajo.ai/v1/fo-calls/<ticket>:join` (no auth; ticket is the credential) → LiveKit WebRTC. Single-use ticket (10 min), ~2 min token, relay-only ICE, one call per user. A browser can just open `joinUrl`.

This HTTP endpoint is probably the "API" mentioned on the Luma page. **There are no webhooks or reply events in the spec.**

## 5. What this changes

| Earlier question | Answer | Label |
|---|---|---|
| Is there a webhook/event API? | Not in the official spec; only polling via `get_fo_reply` / `get_fo_conversation`. But the tool schema mentions a `conversation.reply` webhook (see §7). Design for polling; ask Wajo. | DOCUMENTED / UNTESTED |
| How do we avoid duplicate sends on retry? | On timeout, poll `get_fo_reply`; never resend `ask_fo` for the same step. | DOCUMENTED |
| Can we trigger Fo's outbound call to a seller through MCP? | Not as a tool. Only by asking Fo in text ("call X and ask…"). Whether a transcript comes back is unknown. | DOCUMENTED / UNTESTED |
| Can we read seller email replies through MCP? | **No** for the email-channel conversation a reply creates: 0 turns (A1, VERIFIED twice). Fo claims replies come back into the original thread; spike 1c tests that. Gmail IMAP is the read path. | VERIFIED / CLAIMED |

## 6. A1 checklist (to run with `backend/sealed/fo/auth.py`)

1. `list_fo_conversations`: which channels appear; is there an `email` conversation from a past test send?
2. `get_fo_conversation` on that email conversation: do turns include the recipient's reply? What `role` does it get?
3. `initialize` result and capabilities; `resources/list`, `prompts/list`.
4. OAuth metadata at `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`.
5. Time a `get_fo_reply` poll loop and record real latency.

## 7. A1 / A3 / A4

### A1 — live introspection, read-only (2026-10-17, `spikes/fo_introspect.py`)

| Check | Result | Label |
|---|---|---|
| `list_fo_conversations` channels | 8 conversations: 7 `chat`, 1 `email` ("Re: Quote request test", the Oct 15 vendor reply). **No `voice` conversation exists.** Conversations our MCP client starts are titled **"ChatGPT"** (Fo labels MCP clients that way). | VERIFIED |
| `get_fo_conversation` on the `email` conversation | Returns only `conversation` metadata (`conversationId`, `title`, `channel: "email"`, `startedAt`). **No `turns` key at all**; text content is just the title. The vendor's reply is **not readable through MCP**. | VERIFIED (2nd time; same as Oct 15) |
| → Decision | **Read dealer replies from Gmail IMAP.** Fo CLAIMS replies "arrive back in this thread" (A3); spike 1c watches the per-dealer conversation to test that claim. | VERIFIED + CLAIMED |
| `get_fo_conversation` on a `voice` conversation | Not possible: none exist. Rerun after spike 1d. | UNTESTED |
| `resources/list` | One resource: `ui://wajo-fo/call` ("Call Fo", `text/html;profile=mcp-app`): an inline call panel for `start_fo_call`; CSP allows `mcp.api.wajo.ai`, `livekit.api.wajo.ai`; needs microphone. | VERIFIED |
| `resources/templates/list`, `prompts/list` | Both empty. | VERIFIED |
| Capabilities | `logging`, `resources` (list_changed), `tools` (list_changed), extension `io.modelcontextprotocol/ui`. | VERIFIED |
| `/.well-known/oauth-protected-resource/fo` | `resource: https://mcp.api.wajo.ai/fo`, auth server `https://mcp.api.wajo.ai`, bearer in header, scope `mcp`. | VERIFIED |
| `/.well-known/oauth-protected-resource` (root) | `resource: https://mcp.api.wajo.ai/mcp`: a **second MCP server**. | VERIFIED |
| `/.well-known/oauth-authorization-server` | Endpoints `/oauth/authorize`, `/oauth/token`, `/oauth/register` (DCR); PKCE S256 only; grants `authorization_code`, `refresh_token`; public clients (`none`). | VERIFIED |
| Second server `https://mcp.api.wajo.ai/mcp` | `wajo-agentmcp` 0.1.0, tools listable without auth: `build_agent`, `chat_agent`, `get_agent_reply`, `register_webhook`, `list_webhooks`, `test_webhook`, `delete_webhook`, connectors, sessions, `search_documentation`, … An agent-builder API, not Fo itself. | VERIFIED (tool list only; nothing called) |
| `conversation.reply` webhook | `register_webhook` description: signed POST (Standard Webhooks headers) "whenever one of your conversations finishes a turn (`conversation.reply`)", plus `schedule.fired`, `message.proactive`. Payload: conversation id, run id, channel, final message capped at 2,000 chars. Public HTTPS URL required; max 5 per account. Whether it fires for Fo's email conversations, and what text it carries for a dealer reply, is unknown. **Not registered.** | DOCUMENTED (tool description) / UNTESTED |

### A3 — Fo about itself (2026-10-17, `spikes/fo_ask_self.py`, one new conversation, no sends)

`ask_fo` returned `complete` in 23.7 s (no `working`, no polling). Fo's answers, all **CLAIMED**:

| Question | Fo's answer (condensed, quotes verbatim) |
|---|---|
| What can you do? | "research and comparisons, scheduling, email and texts, phone calls to businesses, purchases, documents and spreadsheets, and multi-step errands". |
| Channels | "email, SMS/iMessage, phone calls, and Telegram (once you link it)". |
| Call a business + transcript? | "Yes, I can call a business, and afterward I can pull you a transcript or a short summary of the call." |
| Stick to a script? | "I work from a brief you give me, **not a rigid script**: I make your asks and answer questions that come up, and anything past what you authorized I bring back to you instead of agreeing to." |
| Memory | "Yes, I keep **durable memory across conversations**: preferences, your people, deadlines, projects". |
| When it asks for confirmation | "before one-way doors: money, sending anything you didn't explicitly ask for, bookings, cancellations, anything irreversible. Reversible work I just do and report." |
| Purchases/payments | "yes, with your approval, and a card connected in Wajo payments settings. Without one I can build the whole checkout and hand you the pay step." |
| `conversation.reply` webhook | "I can't confirm that exists". Also: "**Replies to outreach I send arrive back in this thread on their own**". (Contradicts A1 for the Oct 15 reply; to be tested in spike 1c.) |

**Impact:** durable memory means a ceiling mentioned once, anywhere, could resurface in any later message or call; the "never mention a budget to Fo" rule applies to every chat on the account. "Not a rigid script" means call scripts must be short, true, and end with a written-quote ask; call summaries are audited. Purchases are possible with a connected card: keep **no card connected** to the demo Wajo account (ASSUMED safeguard, to verify in Wajo settings).

### A4 — what we VERIFIED (from `spikes/FINDINGS.md`, 2026-10-17)

| Finding | Label |
|---|---|
| Direct OAuth from Python works (mcp SDK 2.3.0): dynamic client registration, PKCE, browser sign-in once, then token reuse. Scope `mcp`, access token 1 h, refresh token included. | VERIFIED |
| Tool listing needs no auth; the first tool call triggers OAuth. Server `wajo-fo` 0.1.0, protocol `2025-11-25`. | VERIFIED |
| All tools now publish output schemas; `status` and `conversationId` are only in `structured_content`. | VERIFIED |
| `ask_fo` annotations: destructive, **not idempotent**, open-world. Read tools are read-only and idempotent. | VERIFIED |
| `list_fo_conversations` channel enum also includes `cron`, `hook`, `x`. | VERIFIED |
| `get_fo_conversation` turns carry `runId`, and the schema mentions a **`conversation.reply` webhook** with the same `runId`. Not exposed via MCP and not in the official spec. | VERIFIED (schema text) / UNTESTED |
| Server advertises `resources` capability and an `io.modelcontextprotocol/ui` extension. Not explored. | VERIFIED |
| Trivial turn ~16 s; email turns 35–53 s, returned `complete` directly. `working` + `get_fo_reply` path not yet seen. | VERIFIED |
| Two concurrent `ask_fo` calls both succeeded with distinct ids, but finished together after 53 s: likely one turn per account at a time. | VERIFIED (behaviour) / cause ASSUMED |
| **Fo refused to send a body written in the user's voice and signed "Het"**; offered to send openly as the user's assistant instead. | VERIFIED |
| Refresh works without a browser; **refresh tokens rotate**; SDK 2.3 forgets expiry for stored tokens (fixed in `auth.py`). | VERIFIED |
| Gmail IMAP reader written, not run. Verbatim body, subject tag and threading still untested. | VERIFIED (status) |

**Correction to §4–5:** the official spec lists no webhook, but the tool schema mentions `conversation.reply`. Ask Wajo in Discord whether it can be enabled for hackathon accounts. Until then, design for polling.
