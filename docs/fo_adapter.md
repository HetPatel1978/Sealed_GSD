# Fo adapter: validation and controlled live acceptance

**Current call contract:** new calls require a checked buyer/phone/opening plan
before one dialing permission, then durable read-only observation until the final
call report. See [call lifecycle, validation and provider limits](calls.md).

## Authentication

Set `FO_API_KEY` in the gitignored root `.env` to use Wajo's direct API-key
authentication. `LiveFoClient` sends it as `Authorization: Bearer <key>` to the
Fo MCP endpoint (`https://mcp.api.wajo.ai/fo` by default). Keep it out of frontend
`VITE_*` settings, prompts, logs and committed files. Restart the backend after
changing `.env` so it loads the new setting.

A nonempty API key takes precedence over the existing OAuth file. Key mode does
not construct/read OAuth storage, refresh OAuth tokens or silently fall back to
OAuth if the key is rejected. With `FO_API_KEY` blank/unset, the existing OAuth
path remains available and refreshes tokens before expiry. Key changes do not
clear account reservations, change job permissions or authorize dealer outreach.

**Current email contract:** new job emails use [Fo-composed drafts from briefs](fo_briefs.md)
(decision 16). The smoke command below deliberately remains the legacy exact-text test; its
recorded payload and review marker are unchanged. New brief-based emails require a checked
draft before confirmation and permit compliant wording/signature changes. A controlled $90
counter-reply passed live draft review and Gmail sent-copy auditing on October 18, 2026.

The adapter implementation is tested locally and a legacy controlled send was verified in Gmail.
The brief contract has also passed the controlled counter-reply acceptance below. [Sent-copy auditing](sent_audit.md) is now
implemented, including an adapter callback that retains the account reservation until verification.

On October 18, 2026, live read-only validation passed: OAuth authentication, two consecutive
`list_fo_conversations` calls through the same client, and clean session shutdown. The check printed
only status/count metadata and sent no email or phone call.

The first approved live email was sent to `hfp5252@psu.edu` using run `smoke_01`. Fo replied
"Sent, exactly as written"; the classifier now accepts the comma after "Sent". A read-only Gmail
check confirmed the requested recipient, CC and subject and found the requested body intact plus
Fo's appended signature and social links. The stored outcome is sent with `audit_mismatch`, and
the original audit warning is retained as history. After the user approved resolving that specific
signature-only difference, its active account pause was released without changing the sent record
or audit evidence. Nothing was resent. This run still does not pass a strict verbatim-body audit.

## Local verification

From `backend/`, run `python -m pytest`. The tests cover the installed MCP SDK's real request-timeout
exception over in-memory channels, concurrent duplicate suppression, restart/replay, job/run isolation,
late conversation IDs, preview rejection, payment warnings, disk failure, session reconnection and cleanup.

## Prepare one email

From `backend/`, with the virtual environment active:

```powershell
python -m sealed.fo.smoke --to rfb5381@psu.edu
```

This prints the exact recipient, CC, subject and body, and runs the pre-send guard. CC defaults to
`GMAIL_ADDRESS` in the repo-root `.env`; `--cc` overrides it. The dry run creates no live client or state
file. It reads no job or ceiling. The fixed body explicitly describes a controlled integration test
and asks for a sample written total including fees and an availability date.

After the teammate approves this email, send that same request:

```powershell
python -m sealed.fo.smoke --to rfb5381@psu.edu --yes-send
```

Configure `FO_API_KEY` or the existing Fo OAuth file. OAuth tokens refresh without opening a browser. No credentials
or raw HTTP/SDK exception strings are printed. The adapter state defaults to `.sealed/fo_state.json`,
which is gitignored. Use that same state file for every adapter instance on this account.

The default run ID is `smoke_01`. Repeating it returns the recorded result without another email.
Changing the recipient/body with the same message identity is rejected. `--run-id` is for a deliberately
separate approved test, not retrying an uncertain send.

For acceptance, check that the email reached the teammate, compare the actual body and headers with the
dry-run output, and have the teammate reply. `sent: true` is Fo's report, not a Gmail audit; automatic
sent-copy verification and quote ingestion are step 3. There are no automatic follow-up emails or calls
in this smoke command.

## State and recovery rules

- Run one adapter process and one state file per Fo account. The lock serializes all transport instances
  within that process; it is not a lock between separate processes.
- State version 2 scopes messages by job/run/message and conversations by job/run/dealer. Each message
  stores a payload fingerprint. Pending state is flushed before `ask_fo`, and writes atomically replace
  the file so a failed write cannot leave partially written JSON as the next startup's state.
- `working` and SDK timeouts poll `get_fo_reply`; they never resend the original `ask_fo`. IDs recovered
  during polling are retained for subsequent polls, confirmation and follow-ups.
- A preview must pass the engine's guard before confirmation. Unexpected payment instructions are
  stopped before confirmation, including when a callback mistakenly approves them. The exact known
  booking body is exempt from payment-word detection because it requests purchase paperwork and
  explicitly says no payment is being made by email.
- Unresolved or flagged activity keeps an active marker across restarts. Further sends stop for human
  review. Inspect Fo and Gmail, retain the message records, and reconcile the state only after the
  outcome is known. A limited [operator-reviewed call release](call_review.md) is available; automatic
  review/release is not implemented. Do not delete the state file or change
  run IDs to force a resend.
- Nonempty unscoped legacy files are rejected unchanged. They need explicit review rather than a
  guessed migration that might duplicate an earlier send.

## New brief-mode acceptance

Use `python -m sealed.fo.brief_smoke --to consenting-teammate@example.org` for a dry run.
It supplies a controlled-test brief, not the legacy exact email above. After explicit approval,
`--yes-send` checks Gmail connectivity before sending and automatically audits the actual copy.
An existing review marker prevents sending; this command never clears it. `--audit-only` checks
a recorded brief attempt without a Fo call or resend. See `docs/sent_audit.md` for full behavior.

## Controlled counter-reply demonstration

`python -m sealed.fo.bargain_smoke --target 90` rereads the latest authenticated reply in the
original test conversation. It supports only the reviewed $100 sample, rejects changed or expired
terms, validates that quote in the real ledger, and runs the real round-two strategy. With a public
target of $90 and no competing quote, the resulting action is a $90 counter. This is reviewed sample
extraction; the general quote parser and automatic engine delivery remain unfinished.

After the user authorizes that counter, `--yes-send` asks Fo to compose its wording from the public
brief and checks the full draft before confirmation. The incoming RFC Message-ID and Gmail thread
are bound into the outbound fingerprint. The actual Gmail copy must pass the content/routing checks,
reply to that Message-ID and remain in that thread. No booking or payment is authorized. The trace
is saved locally in gitignored `.sealed/bargain_smoke_01.json`; repeated attempts never resend.

Live acceptance passed on October 18, 2026: one counteroffer was delivered to the consenting test
recipient with the owner on CC, in the original Gmail thread and linked to the authenticated $100
reply. Fo composed the $90 request; all 26 actual-copy rules passed, including sender authentication,
price roles and absence of commitments. Earlier drafts were canceled before send approval and
reconciled using explicit Fo cancellation plus zero Gmail copies; their records remain intact.
The successful trace is `.sealed/bargain_revision_03.json`. The account has no active review pause.
Backend validation: 354 tests. This controlled demonstration does not complete the general parser,
automatic engine delivery or live job coordinator.
