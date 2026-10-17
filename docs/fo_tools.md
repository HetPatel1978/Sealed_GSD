# fo MCP Server — Tool Reference

- **Server:** `fo` (HTTP), `https://mcp.api.wajo.ai/fo`
- **Status:** Authenticated (OAuth), connected
- **Captured:** 2026-10-15

Fo is a "bits-to-atoms mover". It acts in the real world from the user's own Wajo account: email, calendar, phone calls, purchases, and connected integrations.

> **About the "Returns" sections:** none of these tools publishes an output schema. Only the input schemas below are formal. Return values are described from the tool descriptions and the server instructions. They are not a typed contract.

There are 4 tools. In Claude Code they are prefixed `mcp__fo__`.

| Tool | Purpose | Side effects |
|---|---|---|
| `ask_fo` | Send a request to Fo to act on | **Yes**: can send email, change the calendar, place calls, make purchases |
| `get_fo_reply` | Poll for the result of a long-running `ask_fo` | No (read/wait) |
| `list_fo_conversations` | List past Fo conversations across all channels | No (read) |
| `get_fo_conversation` | Read one conversation's transcript | No (read) |

---

## 1. `ask_fo`

**Full name:** `mcp__fo__ask_fo`

### Description
Sends the user's request to Fo, and Fo carries it out from the user's own Wajo account. Fo can read and send the user's email, manage their calendar, place calls, make purchases, and use the integrations they have connected.

By default it continues the user's current Fo conversation. To continue a specific conversation, pass `conversationId` (from `list_fo_conversations` or an earlier reply). Set `newConversation` only when the user clearly changes topic or asks for a fresh start.

A long turn returns `status="working"` with a progress line. The caller then keeps calling `get_fo_reply` and relays the progress until the status is `complete` or `failed`. Don't send another `ask_fo` until the current turn settles. `conversationId` is an internal handle and is never shown to the user.

### Input schema
```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "message":         { "type": "string",  "description": "The user's request for Fo, verbatim. Required." },
    "conversationId":  { "type": "string",  "description": "Continue a specific Fo conversation (an id from list_fo_conversations or an earlier reply). Omit to use the current conversation." },
    "newConversation": { "type": "boolean", "description": "Start a fresh Fo conversation instead of continuing the current one. Default false. Not combinable with conversationId." }
  },
  "required": ["message"]
}
```

| Parameter | Type | Required | Default | Notes |
|---|---|---|---|---|
| `message` | string | **Required** | — | The user's request, verbatim, plus any context Fo needs to act |
| `conversationId` | string | Optional | current conversation | Must come from the server, never invented. Cannot be combined with `newConversation` |
| `newConversation` | boolean | Optional | `false` | Cannot be combined with `conversationId` |

### Returns
- `status`: one of:
  - `complete`: includes Fo's reply text. The reply may be a clarifying question or a request for confirmation, which goes back to the user.
  - `working`: includes a `progress` line. Continue with `get_fo_reply`.
  - `failed`: includes an explanation that is safe to show the user.
- `conversationId`: the conversation the turn ran in. Pass it to `get_fo_reply` or a later `ask_fo`.

---

## 2. `get_fo_reply`

**Full name:** `mcp__fo__get_fo_reply`

### Description
Waits for and fetches Fo's reply after `ask_fo` returned `status="working"`. Each call waits up to `waitSeconds` (default 40) for Fo to finish.

The caller keeps polling and relays the progress in one sentence each time. After about ten minutes of `working`, the caller tells the user that Fo is still on it and keeps working even if they leave, and asks whether to keep waiting. The user is never asked to check back later.

### Input schema
```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "conversationId": { "type": "string",  "description": "The conversation to poll (from the ask_fo reply). Omit to poll the current conversation." },
    "waitSeconds":    { "type": "integer", "minimum": 0, "maximum": 40, "description": "How long to wait for Fo to finish before answering. Default 40; 0 answers immediately." }
  }
}
```

| Parameter | Type | Required | Default | Notes |
|---|---|---|---|---|
| `conversationId` | string | Optional | current conversation | Use the id from the `ask_fo` reply |
| `waitSeconds` | integer (0–40) | Optional | `40` | `0` returns immediately |

### Returns
- `status`: one of:
  - `complete`: includes Fo's reply.
  - `failed`: includes an explanation that is safe to show the user.
  - `working`: includes a `progress` line and `elapsedSeconds`.

---

## 3. `list_fo_conversations`

**Full name:** `mcp__fo__list_fo_conversations`

### Description
Lists the user's previous conversations with Fo across every channel (chat, email, texting, Slack, voice, and this app), newest first, up to 50. Each entry has a `conversationId` that can be passed to `get_fo_conversation` to read the transcript, or to `ask_fo` to continue the thread. Use it when the user refers to something they discussed with Fo before, or asks what they talked about.

### Input schema
```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {}
}
```

There are no parameters.

### Returns
- A list of up to 50 conversations, newest first.
- Each entry includes a `conversationId`. It probably also has summary metadata such as channel, title or preview, and a timestamp, but this is not documented.

---

## 4. `get_fo_conversation`

**Full name:** `mcp__fo__get_fo_conversation`

### Description
Reads the recent transcript of one of the user's Fo conversations: the most recent ~40 turns, oldest first. Use it to recall what the user and Fo discussed before continuing the thread with `ask_fo`.

### Input schema
```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "conversationId": { "type": "string", "description": "The conversation to read, from list_fo_conversations. Required." }
  },
  "required": ["conversationId"]
}
```

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `conversationId` | string | **Required** | Must come from `list_fo_conversations` |

### Returns
- The transcript as a list of turns, about the last 40, oldest first.
- Each turn holds the speaker (user or Fo) and the message content.

---

## Usage rules (from the server's instructions)
1. Send the user's request to `ask_fo` verbatim, with the context Fo needs.
2. If the status is `working`, poll `get_fo_reply` until it is `complete` or `failed`, and relay each progress line in one sentence.
3. Don't send another `ask_fo` while a turn is still working.
4. After about 10 minutes of `working`, tell the user Fo keeps working even if they leave, and ask whether to keep waiting.
5. Continue the current conversation by default. Use `newConversation: true` only when the user changes topic or asks for a fresh start.
6. Only use a `conversationId` the server returned. Never invent one, and never show it to the user.
7. Relay Fo's replies faithfully, including clarifying questions and confirmation requests.
