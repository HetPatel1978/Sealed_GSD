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
