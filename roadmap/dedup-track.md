> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

## Dedup + canonical listing track (parallel)

Today the `listings` table is effectively a mirror of sreality.cz
keyed on `sreality_id`. As multi-portal ingestion (Scraper Phase 2)
brings bezrealitky / idnes / remax / maxima / etc. into the same
table, "the same property" will start showing up multiple times —
both within a single run (cross-portal collision) and across runs
(taken down and relisted under a new broker after expiring). This
track is the work to identify those duplicates and present one
canonical listing per real-world property.

**Directional architectural shift surfaced by the operator.** The
`listings` table evolves from "mirror of sreality" to "mirror of
every observed property across all sources, deduplicated."
Architectural rules #1 (append-only migrations), #2 (snapshot on
content change), and #3 (never delete listings) all carry over —
applied at the canonical level rather than the per-source level.
The migration is significant; this track plans the path but does
not commit to it without an operator decision on the canonical
shape (see D1 below).

> **Design locked (2026-05-25): see
> [`docs/design/multi-portal-dedup.md`](docs/design/multi-portal-dedup.md).**
> The operator's expanded requirements (cross-portal price-history
> chart, link history, all-sources-inactive lifecycle, "listed on 3+
> sites" / "price dropped 10%+" filters, daily property-change
> notifications) outgrew Shape A. We now adopt **Shape B** (a thin
> `properties` parent + existing `listings` as per-source children) and
> treat D1 + D2 + Scraper Phase 2 as **one feature** shipped in six
> independently-signed-off slices (0 foundation → 5 image tier). The
> design doc is the source of truth; the Shape-A-as-default text in the
> D1/D2 subsections below is **superseded** and kept for history. Each
> slice still needs the per-slice operator sign-off listed in the doc
> before its migration lands.
>
> **Progress:** Slices 0, 1, and 2a are **built and applied**. Slice 0
> (migrations 091+092 + scraper wrapper) + Slice 1 (migrations 093+094, the
> recompute job + hourly workflow, Browse Map/Table/Cards on
> `properties_public`). Slice 2a (migration 095) denormalised the filter
> columns onto `properties` so `browse_stats_properties` is perf-equivalent to
> the listing-grain RPC, repointed the Stats tab to it, and added the four
> derived filters (`distinct_site_count_min`, `price_drop_count_min`,
> `price_rise_count_min`, `max_price_drop_pct_min`) through the registry into
> Browse (Map/Table/Cards + Stats). Slice 2b (migration 096) moved
> notifications to the property grain (dispatch once per real property, not per
> portal listing), added a second matcher (`match_changes_once`) that fires
> `price_drop` change-events for properties dropping in the lookback window,
> and surfaced the four derived filters in Watchdog. Slice 3a (migration 097)
> built the portal-agnostic insert-time Tier-1 matcher: a geo+price+area probe
> (`ST_DWithin 20m`, price ±2%, area ±1m², same-source excluded) that attaches
> a new listing to a near-matching property, creates a singleton on no match,
> or enqueues a `property_identity_candidates` row on ambiguity — plus the
> `ScrapedListing` contract + a negative synthetic-id sequence for non-sreality
> rows. It's inert for today's sreality-only data (verified). Slice 3b
> (migration 098) shipped the first portal scraper (operator chose **bazos**):
> `scraper/bazos_parser.py` (deterministic selectolax HTML→`ScrapedListing`),
> `scraper/bazos_client.py` (adaptive-throttle fetch reusing `RateLimiter`),
> `scraper/bazos_main.py` (index→detail→stage-in-`portal_raw_pages`→parse→
> `ingest_scraped_listing`, no `mark_inactive` on a partial walk), and the
> manual `scrape_bazos.yml` workflow. `portal_raw_pages` decouples fetch from
> parse so pages re-parse without re-fetching.
>
> **Complete (2026-05-28).** The remaining slices shipped: the merge/unmerge core
> + review API (migration 100, `toolkit/property_identity.py`, `/dedup/*`), the
> Tier-2 fuzzy sweep + auto-merge classifier (`scripts/dedup_sweep.py`,
> `dedup_sweep.yml`), the `/dedup` operator review UI, the Listing Detail
> cross-source price chart + "listed on N sites" panel, the image pHash tier
> (migration 102, `scraper/image_phash.py`, `compute_image_phash.yml`), and region
> stats on the property grain (migration 103). Auto-merge is conservative —
> only ≤30m + an independent corroborator (near-exact address, low-Hamming pHash,
> or vision); everything else queues. Every merge is reversible via `unmerge_group`.
> The bazos pilot is now **scheduled (every 6h)** and lands data after three pilot
> fixes (return-the-PK for image attribution; cast geom params so null-coord rows
> insert; extract coords from the page-wide maps link). Cross-source matching is
> geo-based, so it lights up as bazos coordinates accumulate; the sweep already
> produces real bazos↔sreality candidate pairs. Next portals (bezrealitky / idnes)
> reuse the same `ScrapedListing` → `ingest_scraped_listing` framework.

> **Active program (2026-07): dedup vision cost + backlog quality.** The executing
> cost plan is [`docs/design/dedup-cost-reduction.md`](../docs/design/dedup-cost-reduction.md);
> the 2026-07-12 investigation is
> [`docs/design/dedup-vision-and-backlog-overhaul.md`](../docs/design/dedup-vision-and-backlog-overhaul.md)
> (validated the batch warmer draws a near-disjoint pair set from the live engine —
> ~1% overlap, ~0.5%-consumed — and that a single `_both_have_site_plan` step-aside
> vetoed the free arms on 98.6% of a 142-pair operator merge burst). Program order
> (operator-set 2026-07-13): vision-model bake-off → free-signal precision →
> engine-side §4.1 batch rebuild → recency-first compare ordering. Each phase is its
> own PR + operator flip.

### Phase D1: Strict cross-source dedup (proposed — superseded by the design doc above)

Catch the obvious duplicates: the same listing observed on two
portals at once, or the same source-listing re-fetched under a
slightly different URL. This is a precondition for Scraper Phase 2
— without it, multi-portal ingestion multiplies every listing by
the number of portals it appears on. Also a precondition for Phase
U2.7's "notify once per real property" guarantee.

**Canonical shape (operator decision required before this phase
starts)**

Two viable shapes. Both preserve all existing snapshot history and
respect architectural rules #1 / #2 / #3.

- **Shape A — single canonical table, per-source observations as
  history.** Keep `listings` as the canonical row (one per real
  property). Existing `sreality_id` becomes one of many possible
  `source_id_native` values. New companion table
  `listing_source_observations(listing_id, source,
  source_id_native, source_url, first_seen_at, last_seen_at)`
  records every source that has surfaced this listing. Existing
  `listing_snapshots` gains a `source` column so per-source
  content drift is still visible in the diff timeline. Lowest
  migration cost; downstream queries (`find_comparables`,
  `browse_stats`, RPCs, frontend) keep working with minimal
  changes. **Recommended default.**
- **Shape B — two-table model: `properties` + `listings`.** New
  canonical `properties` table; existing `listings` becomes per-
  source observations linked back via `property_id`. Cleaner
  separation of concerns, but every downstream query has to learn
  the join. Tens of files touch this; the visible payoff is small
  if Shape A's denormalised approach already handles the same use
  cases. Reopen when Shape A's limits show up in production.

**Matcher (insert-time, has to be cheap)**

- **Tier 1 — exact canonicalised URL.** Lower-case scheme + host,
  strip query, strip trailing slash, sha256. Hash match against an
  existing canonical row → append a new
  `listing_source_observations` row and a snapshot if content
  differs; do not insert a new canonical row.
- **Tier 2 — (lat, lng, price_czk, area_m2) within tolerance.**
  `ST_DWithin` within ~20 m, price within ±2%, area within
  ±1 m². High precision; catches "same listing surfaced on two
  portals simultaneously."
- **Tier 3 — agent phone / email when exposed.** Same
  (phone, area, district) triple within 30 days = likely the same
  listing relisted by the same agent. Lower precision; auto-merge
  gated on at least one more matching marker.
- **Ambiguous tier.** Anything that matches at lower confidence
  goes to a new `listing_duplicate_candidates` queue for operator
  review. Default to "no merge" rather than "guess merge."

**Migration scope**

- New numbered migration co-authored with Scraper Phase 2's
  `source` / `source_url` / `source_id_native` columns (single
  migration touching the same surface).
- Shape-A path: add `listing_source_observations` +
  `listing_duplicate_candidates`; add `source` to
  `listing_snapshots`. Backfill: every existing row gets one
  `listing_source_observations` entry with
  `source='sreality', source_id_native=sreality_id::text`. No
  data loss.
- `_shared_filter_where` learns to filter by source via the new
  observations join (read path stays on `listings`).

**Notification feature link (Phase U2.7)**

Phase U2.7's `notification_dispatches` table currently keys on
`sreality_id`. Once D1 ships the canonical id is the dedup key, so
a single property surfaced on bezrealitky AND sreality fires one
notification instead of two. The U2.7 schema gets a one-line
update at D1 land time: `sreality_id` → `listing_id` referencing
the canonical row. Same `(subscription_id, listing_id)` uniqueness
guarantee, just at the right grain.

### Phase D2: Fuzzy property identity (proposed)

Catch the harder case: a listing taken down and relisted weeks
later with different wording, different broker, possibly different
photos. Markers per the operator's brief (everything else — price,
broker, URL, listing copy — is allowed to vary):

- **Address** (street name + house number when present; full
  address is the highest-precision signal).
- **City / district / cadastral area.**
- **Floor** (when known).
- **Disposition + area triangulation.** A 51 m² 1+1 and a 50 m²
  2+kk are likely the same flat — relisted with a different
  disposition label. Use a tight equivalence map across nearby
  dispositions (`1+1 ≈ 2+kk`, `2+1 ≈ 3+kk`, etc.) combined with a
  ±10% area band.
- **Image similarity.** Two-tier to keep cost down:
  - Cheap first pass: perceptual hash (`pHash` / `aHash`) on the
    hero image via Pillow. Catches re-uploads of the same photo
    with minor recompression / resizing.
  - Vision tier for the ambiguous: reuse
    `compare_listing_images` from Phase 6 (Claude vision).
    Higher cost; only invoked when the cheap markers say "maybe."

**Matcher (background sweep, NOT insert-time)**

D1's matcher runs at insert time and has to be cheap. D2 is
heavier (image fetches, sometimes vision calls); runs as a
periodic background sweep over recently-inactive listings against
currently-active listings, surfaces candidates, never auto-merges
without operator review. Precision over recall.

- New table `property_identity_candidates(left_listing_id,
  right_listing_id, confidence, markers_matched jsonb,
  suggested_at, status, reviewed_at, reviewed_action)` — append-
  only audit of every candidate the sweep proposes. Status:
  `proposed` → `merged` | `dismissed`. Operator reviews on a new
  `/dedup/candidates` page (frontend).
- On `merged`: the older listing's snapshots are re-pointed at the
  canonical row, both `listing_source_observations` entries
  collapse onto the canonical id. Architectural rule #3 (never
  delete) holds — merged listings keep their history, the
  canonical row just gains it.
- Sweep cadence: weekly is plenty; relisted-after-expired patterns
  unfold on a multi-week timescale, not minutes.

**Address normalisation**

Czech addresses arrive in a variety of formats (street + descriptive
number + orientation number, street + house number, P.O. box). A
normalisation helper lives in a new `toolkit/addresses.py` —
canonicalises whitespace, strips diacritics for fuzzy comparison
only (display form keeps them), parses out descriptive vs.
orientation numbers, returns a stable comparison key. Hermetic
tests against a fixture set of real Czech address strings.

**Open questions (operator to decide before D2 starts)**

- **Conservative vs. aggressive merging.** Default is conservative
  (queue, operator approves). Aggressive auto-merge above a
  confidence threshold is tempting for scale but bakes in
  irreversible false positives.
- **Image-tier model.** `compare_listing_images` is already there
  but is materially expensive per pair (~$0.05). For D2's volume
  a cheaper dedicated image-similarity model may be needed; pick
  when the cohort size makes the bill visible. pHash alone may
  cover most cases.
- **What "merged" actually means in the UI.** Browse should show
  one row per canonical property by default (default-on toggle to
  "show all source observations" for power use); Listing Detail
  shows all source observations on a tab. Confirm before
  implementation.

**Out of scope for D1 + D2**

- Cross-property dedup beyond same-property identification (e.g.
  identifying neighbouring units that are part of the same
  building — that's the Building decomposition track's job).
- Automatic re-merging when a previously-dismissed candidate
  re-surfaces with new markers — manual re-trigger for now.
- The Shape-B full architectural split (`properties` parent table
  + per-source `listings` child). Reopen once Shape A's limits
  show up in production.

