# Multi-portal ingestion + cross-source dedup (unified D1 + D2)

> **Status: DESIGN LOCKED (2026-05-25), NOT YET BUILT.** This document is
> the approved end-to-end design for the unified multi-portal-ingestion +
> dedup feature. No schema or code has landed. Each slice below ships in a
> later session after the per-slice operator sign-off noted at the end.
> This supersedes the **Shape A "recommended default"** language in the
> ROADMAP "Dedup + canonical listing track" (Phases D1 / D2) — we adopt
> **Shape B**. See ROADMAP.md for sequencing context.

## Context

Today `listings` is a 1:1 mirror of sreality.cz keyed on `sreality_id`.
The roadmap has two coupled-but-unbuilt tracks: **Scraper Phase 2**
(ingest **bezrealitky / bazos / reality.idnes** into `listings` — the
operator's chosen target portals for this build) and the **Dedup track**
(D1 strict + D2 fuzzy). Phase 2 is hard-blocked on D1: without
cross-source dedup, a property on N portals is counted N times, breaking
`find_comparables`, `browse_stats`, and notification fan-out.

The operator wants more than the roadmap's original D1 scope. The real
requirement is a **canonical "property"** with, per property: a
cross-portal **price-history chart**, a **link history** (which portals,
when), **inactive-only-when-all-sources-inactive** lifecycle, and
**efficient Browse/Watchdog filters** — "listed on 3+ sites", "price
decreased/increased 2+ times", "price dropped 10%+". Plus a **daily**
property-level change-notification job. These requirements outgrow the
roadmap's "Shape A" (single canonical `listings` + companion table) and
justify a proper two-table model.

This design treats D1 + D2 + multi-portal ingestion as **one coherent
feature**, designed end-to-end, sequenced into independently-shippable
slices.

## Architecture decision: two-table model (`properties` + `listings`)

Adopt the roadmap's **Shape B** (ROADMAP.md Dedup track), not Shape A.
Rationale: `listings` is *already* a per-source observation — it owns its
`is_active`, `last_seen_at`, `content_hash`, `listing_snapshots`,
fetch-failure tracking, and 9 FK tables. Keep it as the per-source layer
and add a **thin `properties` parent** for the canonical/dedup layer. The
existing scraper write path (`scraper/db.py:upsert_listing` 84-166,
`mark_inactive` 237-263) barely changes. Shape A would force one row to
represent N portals with N prices and N active-states — a semantic
collision with that write path.

**Singleton backfill is the safety move.** At migration time every
existing listing becomes its own 1:1 property. The system behaves
identically until dedup actually merges rows. This freezes the heavy
backend read surfaces:

- **Frozen (zero edits):** `toolkit/comparables.py` (`_shared_filter_where`
  238-436, `find_comparables` 570), `toolkit/velocity.py:44`,
  `toolkit/transit_axis.py:276`, neighborhoods / freshness / snapshots /
  summaries / condition_* / building_extraction / image_similarity, and
  all 9 FK tables (snapshots, images, collections, notes, tags, visual
  layer, building units, manual estimates, condition scores). They key on
  `sreality_id` and keep working.
- **Must change (this IS the feature, not breakage):** frontend Browse
  (`frontend/src/lib/queries.ts` — repoint from `listings_public` to
  `properties_public` for "one dot per property"), the `browse_stats` RPC
  family, and the notification matcher grain (`api/notifications.py`).

### Three locked design decisions

1. **`sreality_id` stays the PK, untouched.** Non-sreality sources get a
   synthetic id from a high-band sequence (`start 9_000_000_000_000`,
   above any real sreality id). The real per-source identity is a new
   `UNIQUE(source, source_id_native)`. This avoids migrating every FK and
   join. The column name becomes a mild misnomer (documented in a comment).
2. **Derived filter aggregates live in a separate `property_stats` table,
   recomputed by the daily job — not eager columns on `properties`
   maintained by the scraper/matcher.** Computing "price dropped 2+ times"
   walks the union of a property's source snapshots; that belongs in a
   batch job, not the hot insert path or a per-query subquery over 50k map
   pins. A stats bug is then fixable by re-running the job.
3. **Property `is_active` rollup happens in the daily job, not inside
   `mark_inactive`.** `mark_inactive` is per-`(category_main,
   category_type)` and runs mid-walk; an eager cross-source rollup would
   race. The daily job recomputes `bool_or(children.is_active)` atomically.
   Consequence: a property's inactive flag lags ≤ 1 day — acceptable given
   notifications are daily.

### Approved tooling/dependencies

Operator pre-approved the new deps, satisfying CLAUDE.md rule #7
("no new dependencies without justification"):
- **An HTML parser** (e.g. `selectolax`/`lxml`) for the portal scrapers —
  the portals serve real HTML, not sreality's JSON API.
- **Playwright (headless browser)** for JS-rendered/anti-bot crawler
  sources; paired with the raw-capture staging table below.
- **Pillow** for image-dedup pHash (un-gates migration 095 / Slice 5).
- **No paid third-party scraping API** — self-hosted fetch only.
- **Target portals: bezrealitky, bazos, reality.idnes.**

### Confirmed design choices (operator instincts validated)

- **URL hash is same-source idempotency only**, never the cross-site key
  (different sites → different URLs). Cross-site matching is marker-based.
- **Shared `ScrapedListing` ingestion contract** (one normalized shape
  every portal scraper emits) → write-through into `listings` → matcher.
  No per-scraper temp DBs. A lightweight raw-capture staging table is
  warranted **only** for HTML/playwright crawler sources, to decouple a
  flaky fetch from normalize+match.
- **Price chart built on the frontend** (Recharts, already in
  `frontend/package.json`) from recorded `listing_snapshots` points across
  all of a property's children. Nothing materialized.
- **Never auto-merge low-confidence fuzzy matches** — they go to an
  operator review queue.

## Migration design (append-only, numbered from 091)

DDL sketches; finalized per-slice. All applied via Supabase MCP after
operator approval, committed in the same change (CLAUDE.md flow).

- **091_properties_foundation.sql** — `properties` parent (identity +
  representative display columns: `geom`, `district`, `disposition`,
  `area_m2`, `category_*`, `is_active`, `first/last_seen_at`,
  `current_price_czk`, `repr_listing_id`). `non_sreality_listing_id_seq`.
  `listings` ALTERs: `property_id` (FK, nullable during backfill), `source`
  (default `'sreality'`), `source_url`, `source_id_native`; backfill
  `source_id_native = sreality_id::text`; `UNIQUE(source, source_id_native)`;
  index `(property_id)`. GiST on `properties.geom`. RLS enabled.
- **092_properties_backfill.sql** — one `properties` row per existing
  listing (singleton), link `listings.property_id`, then
  `SET NOT NULL`. Data-only/additive ⇒ reversible before any merge.
  Health-check assertion `count(properties) == count(listings)` (mirror
  `migrations/089` reconciliation pattern).
- **093_property_stats.sql** — `property_stats(property_id PK,
  source_count, distinct_site_count, price_drop_count, price_rise_count,
  max_price_drop_pct, current_price_czk, computed_at)`; indexes on the
  filterable columns. Populated by the daily job (slice 1).
- **094_property_identity_candidates.sql** — D2 review queue
  `(left_property_id, right_property_id, confidence, markers_matched jsonb,
  tier, status proposed|merged|dismissed, reviewed_at, reviewed_action)`,
  ordered-pair CHECK + UNIQUE. RLS enabled.
- **095_image_phash.sql** — `images.phash bigint` (D2 cheap pass).
  Pillow approved (add to `pyproject.toml` with the Slice 5 work). Hamming
  via `bit_count(a # b)`.
- **096_property_public_views.sql** — `properties_public` (mirrors
  `listings_public` columns + `property_stats` derived columns + `tom_days`
  computed as in `migrations/054`), `property_sources_public` (link history:
  one row per child listing with source/url/active/price). `grant select
  ... to anon`. SECURITY INVOKER, anon reads only (CLAUDE.md frontend rule).
- **097_browse_stats_properties.sql** — property-grain Browse stats RPC,
  a clone of the latest `browse_stats` successor pointed `FROM
  properties_public`, reusing the same WHERE shape + the new derived
  predicates.
- **098_notification_grain.sql** — `notification_dispatches` gains
  `property_id` + `change_kind`; new `UNIQUE(subscription_id, property_id,
  change_kind)`. Migrate `match_once` / `_build_match_clauses` /
  `list_dispatches` to property grain.

## Matcher + ingestion design

**Tier 0 — same-source idempotency (insert-time, free):** the
`UNIQUE(source, source_id_native)` index makes a re-fetch update the
existing row. (This is all a URL hash could ever do.)

**Tier 1 — cheap cross-source proximity (insert-time):** a new
`upsert_listing_with_property` wraps the existing `upsert_listing` in the
same transaction. Before inserting a *new* listing, probe `properties`
with `ST_DWithin(geom, 20m) AND price ±2% AND area ±1m²`. Unique hit →
attach to that property + cheap rollup of `is_active`/`last_seen_at`/
`repr_listing_id`/`current_price_czk`. Zero hits → new singleton
property. Multiple hits → new singleton + enqueue
`property_identity_candidates` (never guess). One spatial probe per *new*
listing only.

**Tier 2 — heavy fuzzy sweep (daily/weekly batch):** new
`scraper/dedup_sweep.py` (or `toolkit/property_identity.py`). Compares
recently-inactive vs currently-active properties. Ladder: address
normalization (new `toolkit/addresses.py`, hermetic-tested) →
disposition≈area equivalence (reuse `_DISPOSITION_LOOSE`
`comparables.py:116`, e.g. `1+1 ≈ 2+kk`) → pHash Hamming (Pillow) →
`compare_listing_images` (`toolkit/image_similarity.py:118`, already
exists + cached) only for the ambiguous few. Writes candidates with
`status='proposed'`; **never auto-merges**. Merge is an operator action
on a `/dedup/candidates` page that re-points children's `property_id` and
recomputes stats (architectural rule #3 holds — merged listings keep
history).

**Daily property-change notification job (new, second matcher):** runs
alongside `match_once`. Reads `property_stats` (computed earlier in the
same run), diffs against the prior run, emits change events
(`new_site`, `now_3plus_sites`, `price_drop_10pct`, …). Dedup grain
`(subscription_id, property_id, change_kind)`. Event matching against
subscription specs reuses `_build_match_clauses` pointed at
`properties_public`.

## Derived filters → registry, kept Browse↔Watchdog in lockstep

The four new filters are **property-grain aggregates** — a category the
registry has never had (`filter_registry.py:120` `pg_column` always points
at a `listings` column). So:

- **Materialized in `property_stats`, indexed, filtered with plain
  predicates** (`ps.distinct_site_count >= 3`). Not per-query subqueries.
- Add 4 `FilterDef`s to `toolkit/filter_registry.py` (new category
  `Multi-portal`, `agendas={BROWSE, WATCHDOG}`) — this drives the Pydantic
  / agent-JSON / FilterForm / serializer generation, i.e. most of the
  frontend work, automatically.
- **Do NOT touch `_shared_filter_where`** (it's hard-bound to `FROM
  listings l` and shared with comparables/velocity/transit, which stay
  property-agnostic). Instead add a shared `_property_stats_clauses(spec)`
  helper imported by both `browse_stats_properties` and the property-change
  matcher — mirroring exactly how `_city_quality_clauses`
  (`comparables.py:158`) is shared today between `_shared_filter_where` and
  `notifications._build_match_clauses` (336-345).

## Slice sequence (multi-session)

- **Slice 0 — Foundation, zero behavior change.** Migrations 091+092.
  `upsert_listing_with_property` maintaining `property_id` + rollups for
  sreality only. Count-reconciliation health check. Nothing in Browse /
  notifications / toolkit / frontend changes. Safe, reversible, unblocks
  everything.
- **Slice 1 — Property-grain read path.** Migrations 093+096+097. Daily
  job computing `property_stats` + `is_active` rollup. `queries.ts`
  repointed to `properties_public` (one dot per property; still 1:1 so
  visually identical, but plumbing is property-grain). Headline frontend
  work.
- **Slice 2 — Notification grain → property.** Migration 098. Migrate the
  matcher to property grain. Add the daily property-change matcher + the 4
  `FilterDef`s wired through registry + `browse_stats_properties` +
  watchdog.
- **Slice 3 — D1 multi-portal ingestion + insert-time Tier 1.** First
  non-sreality scraper on the `ScrapedListing` contract (+ raw-capture
  staging only for crawler sources). Geo+price+area Tier 1 matcher. Now
  properties genuinely have multiple children; slice 1/2 plumbing lights up.
- **Slice 4 — D2 fuzzy sweep + review UI.** Migration 094.
  `toolkit/addresses.py` + background sweep + `/dedup/candidates` page +
  merge action.
- **Slice 5 — D2 image tier.** Migration 095 + Pillow pHash (dependency
  pre-approved) + vision escalation reusing `compare_listing_images`.

## Operator sign-off needed before each migration lands

- **Slice 0:** (a) synthetic high-band id sequence for non-sreality
  sources; (b) `property_stats` as a separate table vs columns on
  `properties` (recommend separate); (c) daily-job `is_active` rollup vs
  eager (recommend daily, ≤1-day lag).
- **Slice 4/5:** conservative (queue + approve) vs aggressive auto-merge
  (recommend conservative); whether Browse defaults to one-row-per-property
  with a "show all source observations" toggle. (Pillow already approved.)

## Verification (per slice, when built)

- **Migrations:** apply via Supabase MCP on a branch; verify with SELECTs
  (`count(properties) == count(listings)` after 092; spot-check a backfilled
  property's `repr_listing_id`/rollups).
- **Scraper write path:** `--dry-run` and `--detail-only <id>` against a
  single sreality listing to confirm `property_id` assignment +
  idempotency (re-run = no new property). Tests via
  `.github/workflows/test.yml` (operator has no local Python).
- **Tier 1 matcher:** seed two listings at the same coords/price/area from
  two sources, confirm they collapse to one property; seed an ambiguous
  multi-hit, confirm a `property_identity_candidates` row instead of a
  guess.
- **Filters/notifications:** confirm `distinct_site_count >= 3` returns the
  same set through Browse and the watchdog matcher (lockstep), and the
  daily change job fires each `change_kind` exactly once per
  (subscription, property).
- **Frontend:** run the SPA, confirm Browse shows one dot per property and
  Listing Detail renders the multi-source price chart + link history.
