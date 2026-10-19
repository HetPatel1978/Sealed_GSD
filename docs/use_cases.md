# Five use cases for the Sealed architecture

These applications are selected for architectural fit: several reachable sellers, a clearly defined requirement, comparable written quotes, a private price limit and a buyer who approves the final terms. This is a design assessment, not a market ranking or a promise of savings.

**Current scope:** the implemented product targets one car purchase. Cases 2-5 are proposed extensions; they are not available product modes.

## The reusable workflow

**Define requirements -> select sellers -> authorize Fo outreach -> validate written quotes -> negotiate within fixed rounds -> privately qualify -> obtain buyer approval -> verify written confirmation.**

- **Fo / Wajo:** public seller research, bounded email composition and authorized calls.
- **Sealed:** facts, permitted prices, quote authority, progress and approval controls.
- **Private zone:** maximum acceptable price, excluded from Fo and seller communication.
- **Evidence:** authenticated written terms; a spoken price alone cannot become a firm offer.
- **Recovery:** save attempts and outcomes; do not blindly repeat an uncertain external action.

## 1. Car buying

**Example:** find dealers offering the specified car, collect written out-the-door quotes and negotiate toward the buyer's target.

- **Public requirements:** make/model, year, trim, relevant equipment, location and timeline.
- **Fo's task:** research dealers, request written totals and follow up through authorized email/calls.
- **Compare:** vehicle identity, total price, fees, taxes, delivery and quote validity.
- **Private decision:** evaluate eligible written offers against the ceiling after the fixed rounds.
- **Approval:** the buyer approves exact terms before a request for purchase paperwork; completion needs matching written confirmation.

**Implementation status:** the core workflow exists. Fresh acceptance of the checked call protocol and the complete live dashboard journey remains pending. Used-car mileage, condition, history and equipment still need buyer review; equal model/year does not establish equivalent vehicles.

## 2. Office and IT equipment procurement

**Example:** a small business requests quotes for 50 monitors of the same model for a new office.

- **Public requirements:** exact manufacturer part number, quantity, condition, warranty, delivery location and required date.
- **Fo's task:** find relevant suppliers, request itemized offers and clarify lead times or substitutions.
- **Compare:** unit price, complete order total, tax, shipping, warranty and delivery commitment.
- **Private decision:** keep the approved internal spending limit out of supplier communication; use the public target in permitted counters.
- **Approval:** an authorized buyer reviews the exact order before a purchase-document request.

**Adaptations needed:** quantity-aware schemas, unit-versus-order price validation, part-number matching, supplier discovery and procurement-specific templates. The current one-item car form does not support this bulk-order workflow. Substitutions and partial quantities must remain distinct offers.

## 3. Bulk printing and custom merchandise

**Example:** an event organizer compares quotes for 200 T-shirts using the same design, fabric and size distribution.

- **Public requirements:** quantity, material, color, size breakdown, print method, artwork specifications and deadline.
- **Fo's task:** research printers, request written quotes and clarify setup or delivery charges.
- **Compare:** production total, setup fees, shipping, taxes, turnaround and agreed quality specifications.
- **Private decision:** keep the organizer's maximum budget private while negotiating a fixed public target.
- **Approval:** review the written offer and separately approve the final artwork proof before production is authorized.

**Adaptations needed:** batch and size schemas, file/proof handling, setup-fee validation and a production-approval stage. Secure artwork handling and proof approval are proposed features; the present car workflow does not implement them. A low quote for a different fabric or print method is not a like-for-like alternative.

## 4. Moving services

**Example:** a household asks several movers to quote the same inventory, route and service date.

- **Public requirements:** inventory, origin/destination area, date, access constraints and packing requirements. Share precise addresses only when needed for the authorized task.
- **Fo's task:** identify movers, request itemized written estimates and clarify exclusions or required surveys.
- **Compare:** included labor, packing, transport, access charges, coverage terms, schedule and additional-charge conditions.
- **Private decision:** use the ceiling only after comparable and sufficiently supported written terms exist.
- **Approval:** the buyer reviews the service scope and estimate conditions before any booking-document request.

**Adaptations needed:** service inventories, address handling, conditional-charge extraction, survey steps and moving-specific confirmation terms. An incomplete estimate must remain non-comparable; a survey or unresolved charge condition cannot be converted into a firm total by the model.

## 5. Home-service equipment replacement

**Example:** a homeowner compares written quotes for replacement of a specified water heater with a defined installation scope.

- **Public requirements:** equipment specification, existing setup, installation scope, location and preferred completion window.
- **Fo's task:** research providers, request itemized written quotes and clarify inspection requirements or excluded work.
- **Compare:** equipment, labor, removal, applicable permit costs, warranty, schedule and exclusions.
- **Private decision:** retain the homeowner's ceiling inside the private qualifier; do not invent competing quotes or disclose the maximum.
- **Approval:** the homeowner reviews the scoped work and written terms before a service-confirmation request.

**Adaptations needed:** structured work scopes, site-visit handling, provider-qualification evidence, optional-work separation and change-order approval. These checks are not implemented. The architecture compares supported commercial terms; it does not determine installation suitability or replace a qualified professional's assessment.

## What transfers and what changes

| Reusable architectural pattern | Domain-specific work |
|---|---|
| Public target separated from private ceiling | Requirement forms, price units and qualification conditions |
| Fo drafts and calls bounded by an authorized brief | Seller sources, outreach vocabulary and domain-specific facts |
| Written evidence checked before it enters strategy | Quote schemas, exclusions, equivalent-spec rules and fee structures |
| Fixed rounds and truthful competing prices | Whether offers are comparable enough for a price-match request |
| Exact-term buyer approval | Purchase, production or service approval milestones |
| Durable outbox, events, fingerprints and pending-result observation | Domain-specific confirmation and exception handling |

The current source contains car-specific schemas, wording, validators and discovery behavior. Reuse requires implementation and acceptance testing, not only a different prompt.

For every extension, approval does not automatically authorize payment or unrestricted future outreach. Written confirmation, private-state handling and provider limitations must remain explicit.

[System flow and trust boundaries](system_flow.md) - [Current architecture](architecture.md) - [Project report](sealed_project_report.md)
