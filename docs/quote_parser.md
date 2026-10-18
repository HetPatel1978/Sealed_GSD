# Isolated quote extraction

`sealed.parser.quote` implements the plan's quarantined Claude extractor. Its public
`Context` contains specification, expected currency, trusted receipt time, evaluation
date, and trusted flags for explicit same-spec references and previously requested
booking confirmation. It accepts no `Job`, target, ceiling, contacts or acting tools.

The provider receives only the cleaned seller reply, public specification, expected
currency and receipt time. The one-request HTTP client uses Anthropic's
[Messages API](https://platform.claude.com/docs/en/api/overview) and
[JSON structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
The default is `claude-haiku-4-5-20251001`; `ANTHROPIC_QUOTE_MODEL` overrides it.
`ANTHROPIC_API_KEY` is sent only as an authentication header. Requests have no tools,
redirects or automatic retries. Provider errors never expose response text or keys.

## Evidence and limits

Claude returns a strict record with evidence spans for amounts, specification,
all-in terms, absolute dates, declines, booking confirmations and itemized costs.
Code checks those spans against the actual reply before constructing `ParsedReply`:

- A total must be a positive, finite monetary amount with a quote/total/offer role.
  Dates, buyer offers, MSRP, monthly payments, deposits, discounts and invented
  arithmetic cannot establish a total. Only supported digit money formats pass.
- All-in terms require explicit out-the-door/all-in wording or inclusion of all fees
  and taxes. Detected exclusions/conditions independently prevent a firm result.
- The exact specification must appear in the evidence, or a trusted associated
  reply must explicitly refer to the same/requested specification. Missing details
  remain unknown. Association alone does not prove the same trim/color/model.
- Calendar dates require a literal ISO or English absolute date including the year.
  Relative dates, omitted years and ambiguous multiple dates remain ungrounded.
  Availability and expiry need the correct textual role. Expired quotes are non-firm.
- The existing breakdown validator checks literal parts and sums; no missing fee or
  tax is derived. Validity is retained as `breakdown.valid_until` even without parts.
- Model trust flags are forbidden. Sender authentication and job association remain
  ingestion responsibilities; the real ledger determines firmness. Booking evidence
  cannot complete a booking unless trusted context says paperwork was requested.

These are conservative rules, not semantic proof of arbitrary prose. Unsupported
or ambiguous evidence receives explicit review reasons and cannot become firm via
model booleans. Validation failure codes also enter `ParsedReply.hedge_words` so the
existing ledger fails closed; the report lists them separately as `review_reasons`.
Suspicious seller instructions are flagged independently, in addition
to the model's flag. Raw instructions are never forwarded to Fo by this path.

`ParseStore` caches the validated result in SQLite by account, Gmail ID and an input
digest covering body, context, date, model, prompt and schema. Repeated previews on
the same date reuse it; changed input invalidates it. Parse results never acknowledge
the staged inbox item. The later engine handoff must durably commit its effects
before acknowledging, and must prevent stale quotes from surviving a failed update.

## Preview a known Gmail reply

From `backend/`, with the configured environment active:

```powershell
python -m sealed.ingest.parse --job-id JOB --run-id RUN --spec "exact specification" --prepare-only
```

This reads Gmail and saves the exact candidate model input locally under gitignored
`.sealed/quote_parse_input.json`. It makes zero Claude requests. One job/run is
required so one specification cannot accidentally be applied to unrelated jobs.
The latest associated reply per seller is selected; newer unverified mail prevents
falling back to an older ready quote.

After authorization to share that reply with Anthropic:

```powershell
python -m sealed.ingest.parse --job-id JOB --run-id RUN --spec "exact specification" --approved-input ../.sealed/quote_parse_input.json
```

This stops if the input changed after preparation, parses or reuses its cached
result, and saves `.sealed/quote_parse_preview.json`. It evaluates the parsed result
with the existing ledger and clearly labels that the job deadline is not checked
in this standalone preview. It sends no emails, acknowledges no inbox items and
does not invoke the engine. Without `--prepare-only`, this command can send reply
text to Anthropic, so use it only for authorized input.

## Current status

The parser, persistent cache and read-only preview are implemented. After the user
explicitly authorized the exact prepared sample, one live Claude request on October
18, 2026 extracted $95 USD, all-in terms, availability 2026-10-25 and validity
2026-10-31. The actual reply did not explicitly confirm the requested specification,
so the result correctly remains review-only (`specification_not_grounded`) and is
not a firm quote. No email was sent, no inbox item was acknowledged and no engine
action occurred. The private report is `.sealed/quote_parse_preview.json`.

The [durable engine handoff](reply_delivery.md) is now implemented with public
checkpoints, reply deduplication and a guarded outbox. The [background coordinator](coordinator.md)
connects these components and supports interactive private-vault restoration. The [job API and dashboard](dashboard.md) now expose this coordinator. Reviewed
reconciliation and live end-to-end acceptance remain. The fixed two-round strategy is unchanged; this does
not implement bargaining repeatedly until the target is reached.
