# Fo adapter: validation and controlled live acceptance

The adapter implementation is tested locally. An approved live email through this adapter is still
required to complete step 2's acceptance check. Earlier spike sends used a different path.

On October 18, 2026, live read-only validation passed: OAuth authentication, two consecutive
`list_fo_conversations` calls through the same client, and clean session shutdown. The check printed
only status/count metadata and sent no email or phone call.

The first approved live email was sent to `hfp5252@psu.edu` using run `smoke_01`. Fo replied
"Sent, exactly as written"; the classifier now accepts the comma after "Sent". A read-only Gmail
check confirmed the requested recipient, CC and subject and found the requested body intact plus
Fo's appended signature and social links. The stored outcome is sent with `audit_mismatch`, and
the active review marker is retained. Nothing was resent. Automatic handling of actual sent-copy
differences is part of step 3; this run does not pass a strict verbatim-body audit.

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

The existing Fo OAuth file must be configured. Tokens refresh without opening a browser. No credentials
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
  outcome is known. Automatic review/release is not implemented. Do not delete the state file or change
  run IDs to force a resend.
- Nonempty unscoped legacy files are rejected unchanged. They need explicit review rather than a
  guessed migration that might duplicate an earlier send.
