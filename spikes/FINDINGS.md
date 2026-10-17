# Spike findings

## Spike 1a: calling Fo MCP directly from Python (2026-10-17)

**Result: YES, direct OAuth works.** No Claude Code fallback is needed.

Setup: official `mcp` Python SDK **2.3.0** (`streamable_http_client` + `OAuthClientProvider`), Python 3.11, script `spikes/fo_oauth.py`. Message sent (twice, each in a new conversation): *"Hi Fo, this is a connection test from my own code. No action needed, just reply OK."*

### What happened
| Step | Result |
|---|---|
| Connect + `initialize` + `list_tools` | Worked **without any sign-in** (0.2 s). Server: `wajo-fo` 0.1.0, protocol `2025-11-25`. |
| First `ask_fo` | Server returned 401, SDK ran OAuth: dynamic client registration succeeded (Wajo issued its own `client_id`, public client, `token_endpoint_auth_method: none`), PKCE S256, browser sign-in, redirect to `http://localhost:3030/callback`. |
| Token | Bearer access token, `expires_in: 3600`, `scope: mcp`, **refresh token included**. Saved to `.fo_oauth.json` (gitignored, verified with `git check-ignore`). |
| Second run | Reused the saved token: **no browser, no sign-in.** |
| Fo's answer | `"OK"`, status `complete` directly from `ask_fo`. No polling needed. |

### Timing
| Call | Time |
|---|---|
| connect + initialize | 0.2 s |
| `ask_fo` run 1 | 18.3 s (includes the OAuth round trip and the user signing in) |
| `ask_fo` run 2 (saved token) | **16.0 s**, complete in one call |

A trivial turn takes ~16 s; on Oct 15 an email-sending turn took 35-45 s. `get_fo_reply` was not exercised because neither turn returned `working`.

### Exact response shapes

`ask_fo` raw `CallToolResult`:
```json
{
  "content": [{ "type": "text", "text": "OK" }],
  "structured_content": {
    "response": "OK",
    "status": "complete",
    "conversationId": "<uuid>"
  },
  "is_error": false,
  "result_type": "complete"
}
```
The text block carries only `response`; `status` and `conversationId` are **only** in `structured_content`.

Published output schemas (all tools now declare one):
- `ask_fo` / `get_fo_reply`: `{ response: string, status: "complete"|"working"|"failed", conversationId: string, progress?: string, elapsedSeconds?: int }`. Required: `status`, `conversationId` (plus `response` for `get_fo_reply`).
- `list_fo_conversations`: `{ conversations?: [{ conversationId, title, channel, startedAt? }] }`, newest first, max 50. `channel` enum documented as: chat, email, sms, whatsapp, telegram, slack, voice, cron, hook, x.
- `get_fo_conversation`: `{ conversation: { conversationId, title, channel, startedAt? }, turns?: [{ role: "user"|"assistant", text, at?, runId? }] }`.
- `start_fo_call`: `{ joinUrl: uri, expiresAt: date-time }`.

Tool annotations: `ask_fo` is `destructive_hint: true, idempotent_hint: false, open_world_hint: true`; `get_fo_reply`, `get_fo_conversation`, `list_fo_conversations` are read-only and idempotent; `start_fo_call` is not read-only.

### Surprising / worth knowing
1. **Fo now publishes output schemas.** On Oct 15 there were none. We can parse `structured_content` instead of free text for status and ids. (Fo's `response` is still free text.)
2. **Tool listing needs no auth.** Only tool calls trigger OAuth (401 on first `ask_fo`).
3. **`get_fo_conversation` turns carry `runId`**, and the schema mentions a `conversation.reply` webhook carrying the same `runId`. A webhook isn't exposed to us via MCP, but it suggests Wajo has one.
4. **Server advertises `extensions: io.modelcontextprotocol/ui`** and `resources` capability (list_changed). Not explored.
5. **Fast simple turns return `complete` straight from `ask_fo`**; the `working` + `get_fo_reply` polling path is still untested from our code.
6. The SDK stores `expires_in` (relative), not an absolute expiry. After an hour the first call should 401 and the SDK should refresh with the refresh token. **Not yet verified.**

### Implications for the Fo adapter
- Use `structured_content` for `status` / `conversationId`; keep the text block only for logging.
- Run OAuth once (interactive), then reuse `.fo_oauth.json`; the backend never needs a browser after that, as long as refresh works.
- Budget per send: ~16 s minimum, ~45 s for real email turns, plus polling.

### Still open
- `working` path with `get_fo_reply` (expect it on email-sending turns).
- Token refresh after 1 hour.
- Concurrency: two `ask_fo` calls on different conversations at once.
- Spike 1b items (verbatim body, subject tag, confirmation turn, threading), which need real sends to a teammate.

## Spike 1b: real send through Fo (2026-10-17, 16:29 UTC)

Script: `spikes/fo_send_test.py`; builder: `backend/sealed/fo/messages.py` (`fo_instruction` wrapper + verbatim `email_body`). Recipient: rfb5381@psu.edu only. Tag: `SEALED-B617C518`. Body contained no digits or prices (checked before sending).

**Result: Fo refused to send.** Nothing was emailed (confirmed: no Gmail thread with the tag, nothing from fo@wajo.ai in the last day). `ask_fo` returned `complete` after 53.2 s with:

> The send got stopped on my end: the message is written in your voice and signed "Het," and I can only send openly as your assistant, not as you. Two ways forward:
> 1. I send the same email from my address, openly as your assistant ("I'm Fo, writing on behalf of Het Patel..."), same subject and same asks.
> 2. You send it yourself from your own inbox so it reads in your voice; the text is ready to copy from above.
>
> Want me to go with option 1?

We did not answer. Option 1 means Fo rewrites the body, which breaks "verbatim" and needs a decision.

| Question | Answer |
|---|---|
| Body verbatim? | **Not testable yet**: Fo won't send first-person text signed by the user. |
| Subject tag kept? | Not testable yet (nothing sent). Fo said it would keep "same subject". |
| `working` / `get_fo_reply` path? | **No.** Both turns returned `complete` directly from `ask_fo` (53 s each). Still untested. |
| Confirmation turn? | Not the Oct 15 "Good to send it?". Instead a policy refusal plus a counter-offer, as a `complete` reply ending in a question. |

**Design consequence:** Fo enforces "send openly as the assistant, never impersonate the user". The message builder must write `email_body` in Fo's voice (e.g. "I'm Fo, Het Patel's assistant…", signed as Fo on Het's behalf). Then test whether Fo sends *that* verbatim.

### Concurrency
Two `ask_fo` calls in new conversations at the same instant over one MCP session (the send above + "what is today's date?"):
- **Both completed, no mix-up**: distinct `conversationId`s, each reply matched its own question ("It's Saturday, October 17, 2026.").
- **But the trivial question took 53.3 s** (vs 16 s alone in spike 1a), and both finished within 0.2 s of each other. Looks like Fo processes **one turn per account at a time** (or queued the second behind the first). Not proven; plan the send queue as serial.

## Spike 2: Gmail over IMAP

`spikes/gmail_read.py` is written (read-only `[Gmail]/All Mail`, X-GM-RAW search on the tag, CC-copy audit with unified diff, 30 s polling for the reply from rfb5381@psu.edu, quoted-history stripping via `email-reply-parser`, threading check on tag / In-Reply-To / References / X-GM-THRID).

**Not run yet:** no `.env` with `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD`, and no email was sent to audit. Connectivity check once `.env` exists: `python spikes/gmail_read.py --query "Quote request test"`.

## Token refresh (2026-10-17, 16:37 UTC)

**Works, no browser.** `spikes/token_refresh.py` ran with 288 s left on the access token:
- POST `https://mcp.api.wajo.ai/oauth/token` with `grant_type=refresh_token`, `client_id`, `resource` → new access token, `expires_in: 3600`.
- **The refresh token rotates** on every refresh. The new one must be saved immediately, or the next refresh fails.
- The new token worked for a read-only `list_fo_conversations` (8 conversations), sent as a plain Bearer header.

**SDK gotcha (important for the adapter):** mcp SDK 2.3 does not remember expiry for tokens loaded from storage. It sends the expired token, gets 401, then starts a **full browser sign-in** instead of refreshing. `backend/sealed/fo/auth.py` fixes this: `FileTokenStorage` records an absolute `expires_at`, and `refresh_if_needed()` refreshes before connecting when under 5 minutes are left.

## T1: read-only introspection (2026-10-17, `spikes/fo_introspect.py`)

- `get_fo_conversation` on the `email` conversation from the Oct 15 vendor reply: metadata only, **no `turns`** (2nd time). Dealer replies are not readable through MCP that way → Gmail IMAP.
- No `voice` conversations exist yet. MCP-started conversations are titled "ChatGPT".
- `resources/list`: one `ui://wajo-fo/call` call panel. Templates and prompts: empty.
- Root `/.well-known/oauth-protected-resource` points to a second server, `https://mcp.api.wajo.ai/mcp` (`wajo-agentmcp`): agent builder with `register_webhook` for `conversation.reply` (public HTTPS URL, payload = Fo's final message ≤ 2,000 chars). Nothing registered or called.
- Full details: `docs/research/fo_mcp.md` §7.

## T2: Fo about itself (`spikes/fo_ask_self.py`)

`complete` in 23.7 s, no `working`. CLAIMED: calls businesses and can give a transcript/summary; works "from a brief, not a rigid script"; durable memory across conversations; asks before money/irreversible actions; can purchase with a connected card; "replies to outreach I send arrive back in this thread". Details in `docs/research/fo_mcp.md` §7.

## Spike 1c: Fo-voice email (PREPARED, NOT RUN)

`spikes/fo_send_voice_test.py`: dry run by default; `--yes-send` only after a team member approves; recipient rfb5381@psu.edu only. Measures preview content (full body/subject/recipient) and answers "yes" only if it all matches; `working`/`get_fo_reply` path; `--tag T --watch 15` watches the per-dealer Fo conversation and new conversations for the reply (tests Fo's "arrives back in this thread" claim). Verbatim/tag on the sent copy: `spikes/gmail_read.py --tag T` (needs `.env`).

## Spike 1d: outbound call (PREPARED, NOT RUN)

`spikes/fo_call_test.py`: dry run by default; `--yes-call` only after a team member approves; number from `SEALED_TEST_PHONE` (local `.env`, never committed; masked in logs). Measures confirmation, whether the call happens, script adherence (from Fo's summary), new `voice` conversation + transcript, end-to-end time.

## Spike 1c: Fo-voice email, RUN (2026-10-17, 20:46 UTC, tag `SEALED-12152C09`)

Recipient rfb5381@psu.edu only. Body in Fo's voice, no digits, no prices (guard passed before sending).

| Question | Result | Label |
|---|---|---|
| Did Fo send it? | Yes. `ask_fo` → `complete` in 40.4 s: "Sent. It went from your Gmail to rfb5381@psu.edu with the exact subject and body, and you're on CC." | VERIFIED |
| Confirmation preview? | **None.** No question, no draft shown; Fo sent directly (we had explicitly asked it to send). The `fo_preview` guard stage did not trigger. | VERIFIED |
| `working` / `get_fo_reply` path? | Not triggered (5th time a turn completed in one call). | VERIFIED |
| Subject tag kept? | **Yes**, subject exact. | VERIFIED (Gmail) |
| Body verbatim? | **Yes**, identical after line-ending normalisation; empty diff. | VERIFIED (Gmail) |
| From address | **`Het Patel <hetp2030@gmail.com>`**, not `fo@wajo.ai` (Oct 15 used fo@wajo.ai). To: rfb5381@psu.edu, Cc: hetp2030@gmail.com. Fo used the user's connected Gmail. | VERIFIED |
| Threading | Sent copy has a normal Gmail Message-ID; Oct 15 reply showed `In-Reply-To` = Fo's message id, so header threading works. | VERIFIED |

**Consequences**
- Because mail goes out from the user's own Gmail, **every dealer reply lands in the user's inbox**, reply-all or not. Gmail IMAP is a complete read path for written replies.
- With no preview turn, the only checks on what Fo actually sent are the pre-send guard (our text) and the **post-send audit of the copy in Gmail**. The audit should move off the cut list.
- The dealer sees the user's address in From while the body says "I'm Fo, an AI assistant…". Honest (disclosed) but mixed; decide whether to ask Fo to send from its own address.

## Spike 1d: outbound call, RUN (2026-10-17, 20:48 UTC)

Call to a team member's own phone (Het's) (number from `SEALED_TEST_PHONE`, never logged in the repo).

| Question | Result | Label |
|---|---|---|
| Confirmation first? | **No.** `ask_fo` → `complete` in 42.8 s: "Call's dispatched to your phone now, and I've set myself a check at 4:58 PM to make sure the transcript summary reaches you one way or another". | VERIFIED |
| Did the call happen? | Yes: a new `voice` conversation appeared ~10 s after the request. | VERIFIED |
| Script adherence | Transcript turns: Fo "Hey Het! What's up?" → Fo "…of a buyer about one specific car. Could you please email the out-the-door total for that car to my email address? Thank you, goodbye." → callee "Thank you". **Fo added a greeting by name** that was not in the script (it recognised the account owner's number). The start of the script is missing from the transcript. | VERIFIED (transcript) / what was said live: per Het |
| Transcript via MCP? | **Yes**: `get_fo_conversation` on the `voice` conversation returns turns (`assistant` = Fo, `user` = callee) with timestamps. Partial (beginning of the script missing). | VERIFIED |
| Where the call lives | A **separate** `voice` conversation, not inside the chat conversation that requested it. Its internal title is `voice-out:<dialed number>` (shown as "Untitled" in the list) → the adapter can match a call to a dealer by phone number. | VERIFIED |
| Unrequested actions | Fo **scheduled its own follow-up check** ("at 4:58 PM") to report the transcript. | VERIFIED (Fo's words) |
| End to end | 46.9 s from request to `complete`; call itself ~30 s. | VERIFIED |

**Consequences**
- Calls work from a text request with no confirmation, so the pre-send guard on the call script is the only gate before Fo dials. Keep calls behind the serial queue and a team member's approval for real dealers.
- Fo improvises at least the opening line. On dealer calls the script must stay short, and call summaries/transcripts are audited for claims beyond the script.
- The voice conversation title contains the dialed phone number: mask it in every log.

### Spike 1c: test replies from rfb5381@psu.edu (watched 20:51–21:16 UTC, `spikes/watch_replies.py`)

| Reply | In Gmail? | In the dealer's Fo conversation? | New Fo conversation? | Label |
|---|---|---|---|---|
| Reply-all (20:58 UTC): "Total price would be $5 for the mug and $5 for print. Delivery price and date would be based on location and distance. But we offer one day delivery. The price is valid for 1 week" | **Yes.** From rfb5381@psu.edu, To Het, Cc Het; `In-Reply-To` = the sent message's Message-ID; same Gmail thread (X-GM-THRID). | **No** (turn count unchanged: 2) | **No** | VERIFIED |
| Reply only to the sender | Not received within the 25-minute window. Because Fo sent from Het's own Gmail, "the sender" is Het: such a reply would land in Het's inbox exactly like a reply-all. "Reply only to Fo" is not possible with this sending mode. | — | — | VERIFIED (not received) / ASSUMED (routing) |

**Conclusions**
- Fo's claim "replies to outreach I send arrive back in this thread" did **not** hold here: nothing appeared in the Fo conversation, and Fo created no email-channel conversation for the reply (on Oct 15, when Fo sent from fo@wajo.ai, a reply did create one, with 0 readable turns).
- **Gmail IMAP is the read path**, and with Fo sending from the user's Gmail it is complete: replies come to the user regardless of reply-all. Threading works on `In-Reply-To` + Gmail thread id, and the subject tag survives.
- The real reply is a good parser test case: per-item prices ("$5 for the mug and $5 for print"), no total, delivery "based on location", validity "1 week" → not an out-the-door total, `firm: false`, firm-up needed.
