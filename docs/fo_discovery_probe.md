# Fo dealer discovery trial — October 18, 2026

Fo can research relevant dealer candidates through the existing MCP integration.
This trial does not implement `/dealers/discover` or change the current provider
decision in `decisions.md`. It demonstrates a possible Fo-first research path and
the checks required before using its output.

## Request and observed behavior

The public sample was a 2026 Honda Civic Sport sedan, Crystal Black Pearl,
automatic, near ZIP 19104, within an initial 20-mile radius. No target, ceiling,
customer identity or private email text was supplied. The prompt authorized only
public research and explicitly prohibited contacting people or making commitments.

The original research took **488.1 seconds**. Fo reported browsing inventory and
contact pages, then returned a summary saying the JSON was attached. Neither the
raw `get_fo_reply` result nor `get_fo_conversation` exposed that attachment or its
URL. A distinct follow-up requesting the existing results inline took **42.6
seconds** and returned a JSON code block followed by a document URL. It did not
repeat the original research request. Plain `json.loads(response)` would still
fail because of the formatting; the offline inspection extracted the code block.

Fo returned five candidates. Distances below are Fo's approximate straight-line
estimates from a ZIP centroid, not verified driving distances or a strict address
radius guarantee.

| Candidate | Reported miles | Independent checks |
|---|---:|---|
| Piazza Honda of Springfield | 7.8 | Exact matching vehicle page, business address and sales phone found. Vehicle is **in transit**, not confirmed available. |
| Burns Honda | 14.6 | Published business phone/address found; proposed email literally found in the official listings page HTML. Exact vehicle URL was not accessible; Fo itself reported it redirected to the inventory hub. Treat exact inventory as unknown. |
| Piazza Honda of Philadelphia | 4.1 | Published business address and sales phone found. Exact matching inventory remains unknown. |
| Sloane Honda | 13.0 | Published business address and sales phone found. Exact matching inventory remains unknown. |
| Martin Main Line Honda | 5.8 | Published business address and phone found. Proposed email was not independently verified: direct HTML fetch returned HTTP 403. Do not use that email as verified. Exact inventory remains unknown. |

The [Springfield vehicle listing](https://www.piazzahondaspringfield.com/new/Honda/2026-Honda-Civic-springfield-pa-4587b98aac18073cc8abc758195ee2b4.htm)
matches the requested year, trim, sedan body, color and automatic transmission.
Its displayed status is in transit. Other checks used official
[Burns inventory](https://burnshonda.com/listings/),
[Philadelphia contacts](https://www.piazzahondaphiladelphia.com/contact-us.htm),
[Sloane contacts](https://www.sloanehonda.com/contactus.aspx) and
[Main Line inventory/contact details](https://www.mainlinehonda.com/new-vehicles/).

## Integration implications

- Require inline data and forbid document publishing/attachments in the research
  prompt. This trial shows an instruction alone does not guarantee strict output.
- Use a background research operation with progress and a cached result; an
  eight-minute request should not block the dashboard or SQLite executor.
- Treat every Fo record as untrusted. Independently verify literal contacts,
  vehicle identity, links and location. Keep missing or inaccessible data unknown.
- Distinguish current matching listings, in-transit units and stale search results.
  Burns' indexed listing must not be promoted to verified current stock.
- Support dealers with a published phone but no verified email; do not guess email
  patterns. Any phone-first identity handoff still needs its own live validation.
- Discovery and seller outreach must remain separate. The customer chooses the
  dealers before any contact is authorized.
- Fo is an action-capable agent. The research-only prompt is not a mechanical
  restriction on its tools. One progress update mentioned a calendar step despite
  the public-research-only instruction; the final output contained no calendar
  details. This remains a boundary to review before production integration.

Fo reported that nobody was contacted and nothing was scheduled. This trial did
not independently audit Gmail or call history, so that is Fo's report rather than
separate delivery evidence. No confirmation for outreach was sent. Both research
turns completed and released their account reservations; history was retained.

## Reproducing the recorded trial

From the repository root:

```powershell
.\backend\.venv\Scripts\python.exe spikes/fo_discovery_probe.py
.\backend\.venv\Scripts\python.exe spikes/fo_discovery_probe.py --inline-existing
```

These fixed sample commands reuse completed local records; reruns do not issue
the original ask again. A pending attempt is polled, not resent. They respect the
same worker lock and active-turn marker as the live adapter. Do not delete their
records to force a new request or bypass another account turn.

Private artifacts under gitignored `.sealed/` include
`fo_dealer_discovery_probe.json`, `fo_dealer_discovery_inline.json`, raw MCP
inspection results, `fo_dealer_discovery_results.json` (unmodified extracted data),
`fo_dealer_discovery_validated.json` (inspection annotations) and
`fo_discovery_email_checks.json`. No credentials are printed or added to the repo.


## Integration follow-up

Fo-first background research and the dashboard dealer picker are now implemented. See [operation and limits](dealer_discovery.md). This probe remains historical evidence, not a fresh acceptance of the integrated flow.
