# Follow-ups

Bugs and small gaps noticed in passing while building other features.
None of these block the current work; tackle when convenient.

## /estimations: input_url is lost when the operator submits from a URL

Discovered while building U2's Estimate page (step 6).

The CreateEstimationIn schema enforces `url XOR spec` — exactly one of the two
must be set. The U2 frontend previews via `GET /estimations/preview?url=…`,
lets the operator edit the parsed spec, then submits to `POST /estimations`
with `spec` (never `url`, because the spec has been edited and re-scraping
would clobber edits). The persisted row's `input_url` is therefore always
NULL on rows created from the UI, even when the operator clearly started from
a URL.

Effect: the audit trail on Run Detail can't link back to the original sreality
listing for ui-sourced runs. The UI works around it by not advertising a
"source URL" line for those rows, but the underlying fact is lost.

Fix options (pick one when this matters):
1. Add an optional `audit_url: str | None` field to `CreateEstimationIn`
   that is recorded to `input_url` without participating in the XOR rule.
   No scrape happens; it's pure metadata. ~5 lines of backend.
2. Relax the XOR rule to allow both — `url` only triggers a scrape when
   `spec` is None. Slightly more invasive but no schema additions.

Either works; option 1 is more explicit about intent.
