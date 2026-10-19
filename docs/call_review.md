# Release a reviewed, uncertain Fo call

Most unclear calls can now be settled from the dashboard without stopping the backend: the paused
step asks you to confirm you checked Fo, then offers "The call happened", "Try again" or "Skip it"
(`POST /jobs/{id}/resolve`, see [api.md](api.md)). Use the CLI below when the dashboard does not offer
those options, for example after a later pause on the same job.

New checked-plan calls have [durable result tracking](calls.md). This procedure is
for a call that still needs explicit operator review, including a missing final
report or a flagged identity/follow-up result.

An unclear call keeps an account reservation so that subsequent calls, emails and
research cannot run alongside an unknown action. Restarting does not remove it.
Cancelling a Sealed job stops its queued outreach; it does not cancel tasks that
Fo may already have scheduled internally.

## Review the existing call first

Open that dealer's existing Fo conversation and check the call result. Confirm
that the call has ended and that Fo has no pending retry, reminder or automatic
follow-up for this call. For a voicemail-only attempt with an unrequested retry,
the operator can instruct Fo:

> Cancel any scheduled retry, reminder or automatic follow-up associated with this
> dealer call. Do not call, email or contact anyone. Confirm that the original call
> has ended and no follow-up for it remains scheduled.

Do not proceed on a vague acknowledgment or a pending cancellation. Save the
confirmation you actually reviewed to a local text file under `.sealed/`.
The CLI records your attestation and the file's SHA-256 digest; it does not infer
task cancellation from natural language, and does not independently verify Fo's
internal scheduler.

## Release the reservation

Stop the backend with Ctrl+C in its terminal and stop any live CLI worker. From
`backend/`, use the exact job and message identifiers shown in the paused job's
events (substitute your identifiers below):

```powershell
.\.venv\Scripts\python.exe -m sealed.fo.review `
  --job-id YOUR_JOB_ID --message-id m_01 `
  --confirmation-file ..\.sealed\call_review_confirmation.txt `
  --call-finished --no-pending-followups
```

This command is limited to an uncertain call with `send_unclear`, `call_review` or
`call_result_missing`, an exact active
job/message match, a payload fingerprint and a matching saved conversation. It
requires both confirmations and acquires the same Fo process lock as the API/CLI.
A running worker, a different active reservation, an email or another outcome
blocks the release. Do not remove a lock file or the Fo state file to work around
a refusal.

The command only clears that reservation's active marker. Original attempt status,
reply, fingerprint, conversation and reason remain, with an appended review record.
It makes no network requests and does not mark a call as delivered, retry outreach,
modify engine/outbox history or resume the paused job. Reusing the original message
still encounters the existing duplicate suppression. Buyer-name discrepancies
remain grounds to keep that negotiation paused until separately addressed.

Restart the backend in the same terminal:

```powershell
.\.venv\Scripts\python.exe -m app.run
```

Refresh the dashboard and click **Find dealers** with the same requirements. A
search blocked solely by the old account reservation, with no provider attempt,
can now be queued again on this explicit submission. The worker still checks the
account reservation before dispatch. Searches that were actually attempted, have
a conversation, returned invalid reports or have an uncertain result are not
automatically repeated.

Backend restarts clear in-memory ceilings. Existing jobs may need their private
ceiling restored before a later authorized job action. This release does not
authorize that action or another dealer call.
