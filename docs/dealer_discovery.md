# Fo dealer discovery

The dashboard's **Find dealers** button now researches public dealer websites
through the existing Fo MCP connection. An optional Claude backup (below) needs
`ANTHROPIC_API_KEY`; Fo-only discovery needs no other key.
Enter the item, exact specification and a US ZIP, find candidates, select up to
five sellers total, then create the negotiation. Manual sellers remain supported.

`SEALED_MODE=sim` returns three explicitly synthetic candidates without constructing
Fo or web clients. Live mode requires the research permission checkbox. Research
does not start a negotiation; starting outreach remains a separate action with
the existing permissions and approval rules. Stock always needs confirmation.

## API

`POST /dealers/discover` returns `202` promptly:

```json
{
  "item": "2026 Honda Civic Sport",
  "spec": {"quantity": 1, "details": "2026 Honda Civic Sport sedan Crystal Black Pearl automatic"},
  "zip": "19104",
  "radius_miles": 20,
  "allow_fo_research": true,
  "allow_claude_research": true
}
```

Only these fields are accepted. Target, ceiling and customer identity are excluded
from the dedicated research schema, prompt and discovery database. Keep private
information out of the public specification too. `allow_fo_research` is required
only for live authorization and is not forwarded to Fo.

Poll `GET /dealers/discover/{id}`. The response includes `id`, `mode`, `status`,
`progress`, `zip`, `radius_miles`, Unix-second `created_at`/`updated_at`, `error`,
`dealers` and `dropped`. POST also includes `cached`. States are `queued`,
`researching`, `validating`, `complete`, `failed` and `needs_review`.

Each dealer includes name, website, address, published email/phone (nullable),
`distance_miles`, `distance_method`, `inventory_status`, `in_stock: null`, optional
`matching_listing_url`, flags and a `source` object with `kind: discovery`, fetched
URLs, `discovery_id` and `candidate_id`. Send the selected name, email, phone and
entire source unchanged in `POST /jobs`. Backend checks the saved result, matching
item/specification/ZIP, exact contact details and a one-hour freshness limit.
Changed selections return `422 discovery_selection_changed`. Editing requirements
in the form clears discovered selections.

Unknown search IDs return `404`; invalid inputs `422`; missing live permission
`409 discovery_permission_required`. Provider failures appear as fixed error codes
in saved status, without exposing provider prose. Manual entry remains available.

## Verification and limits

Fo proposes candidates; code independently fetches up to three official-site
source pages per candidate. It checks published name/address, retains only
literal email/phone evidence, removes duplicates and drops candidates without a
verified contact or a ZIP-centroid distance within 20 miles. HTTPS reads reject
private/mixed DNS answers, pin the validated public IP while verifying TLS against
the hostname, restrict redirects to the same site and bound page size/timeouts.
Pages are read as text; scripts are not executed.

ZIP coordinates come from the public, key-free
[Zippopotam.us API](https://www.zippopotam.us/), requested only with a ZIP code.
Distances are approximate straight-line distances between ZIP centroids, not
address geocoding or driving distances. Postal lookup failure stops validation;
code never substitutes Fo's distance estimate. This replaces the earlier planned
offline ZIP table.

A listing must remain at its cited URL and contain a VIN plus requested
specification terms. Even then, inventory is `listed_unconfirmed` or
`listed_in_transit`; no listing proves current availability. Dealers with only
a verified phone remain usable subject to the existing call authorization.
Strict page checks can exclude valid dealers when sites block automated reads,
format addresses differently or hide contacts behind forms.

Fo receives explicit research-only instructions forbidding outreach, scheduling,
private account access, documents, booking and payments. The existing Fo MCP
connection does not expose a research-only tool sandbox; these instructions are
not a mechanical guarantee about Fo's internal tool use. The prior live probe
demonstrated research and also motivates this limitation. See
[the live probe](fo_discovery_probe.md). No new live dealer outreach was performed
while implementing this flow.

## Durability and account sharing

Research runs in a dedicated thread; short SQLite transactions and independent
HTTP polling keep progress and job reads responsive. Cache keys include account,
mode, Fo state path and public requirements. Completed results are reused for
one hour. Pending or uncertain attempts retain the same search ID.

The discovery worker and negotiation transport share the Fo account turn lock.
An existing uncertain account reservation is preserved. While research holds the
turn, negotiation ticks report `fo_busy_research` and defer dispatch. The API's
process lock still excludes a competing live CLI worker.

If an earlier uncertain call blocks research, use the [reviewed call recovery
procedure](call_review.md). Once the account is released, explicitly resubmitting
the same requirements requeues a search that never reached Fo. Attempted or
uncertain searches are not automatically repeated.

The worker records a durable claim before asking once. Known pending conversation
IDs are polled after restart; unknown/uncertain attempts require review and are
never blindly resent. A completed response that lacks valid inline JSON gets at
most one distinct formatting follow-up. No attachment/document downloads are used.
Raw replies and private conversation IDs stay in gitignored local SQLite state.
Shutdown waits for the worker before releasing the account process lock.

Validation covers mocked Fo polling/restarts, source and distance checks, transport
restrictions, API selection binding and full simulation through explicit approval
and written confirmation. A fresh live Fo discovery acceptance remains separate.

## Claude backup (opt-in)

If the user ticks the second permission box (`allow_claude_research`, live only,
requires `allow_fo_research`), Claude researches the same public request when
Fo's research ends `failed` or `needs_review`, or leaves fewer than three checked
dealers. Claude gets only the public `Request` (no target, ceiling or identity),
uses the `web_search` and `web_fetch` server tools and nothing that acts, and must
return Fo's exact `Report` schema. Its report goes through the same
`validate_candidates` checks. Fo's dealers win on a duplicate website; Claude's
new ones are added and each dealer carries `found_by: "fo" | "claude"`.

- The backup never takes the Fo turn lock and never changes the Fo account
  state, so an uncertain Fo reservation stays exactly as it was. A Fo
  `needs_review` is still reported (`search.fo_status`) even when Claude's dealers
  complete the search.
- The decision to run the backup is written together with Fo's outcome, so a
  poller never sees a final status that is about to change.
- A Claude search contacts no one, so after a restart the backup phase simply
  runs again. If Claude fails, the search returns to Fo's own outcome and
  `search.fallback_error` holds a fixed code (`claude_rate_limited`, ...).
- Giving the permission later on a finished, thin search runs only the backup;
  Fo is never asked again.
- Responses add `search`: `primary`, `fallback_allowed`, `fallback_used`,
  `fallback_reason` (Fo's error code or `too_few`), `fallback_error`, `fo_status`.

Config: `ANTHROPIC_DISCOVERY_MODEL` (default `claude-opus-5-5`; must support the
`web_search_20260209` / `web_fetch_20260209` tools) and
`SEALED_DISCOVERY_FALLBACK=off` to disable it. Simulation never calls Claude.
