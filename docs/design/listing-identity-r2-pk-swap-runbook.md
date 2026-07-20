# R2 → PK-swap: reviewed execution runbook (v1, 2026-07-20)

> **Status: reviewed, ready to execute.** This is the corrected, execution-ready runbook for
> the last deferred track of the listing-identity refactor — R2 (child `listing_id` FKs),
> R3 (dual-write + parity), remaining R4 (join/read-model cutover), and the destructive PK
> swap `sreality_id` → `id`. It supersedes the R2–R5 sections of
> `docs/design/listing-identity-refactor.md` (PR #813 — **still unmerged**; merge it, then
> land this file next to it and mark the old runbook superseded).
>
> Produced by a 9-agent adversarial review on 2026-07-20 (4 census agents against the live
> DB + code at `6fe8b42`, 4 attack agents on the runbook mechanics, 1 completeness critic).
> The doc's runbook direction survived; **four of its steps did not** (ordering inversion,
> unexecutable PK promotion, missing `DROP NOT NULL`, misplaced destructive gate). Every
> claim below was live-verified unless marked "executor verifies".
>
> **Operator hard constraints (unchanged):** existing `sreality_id` values are NEVER
> NULLed — frozen, valid, unique key forever. Destructive steps stop for a pg_dump + the
> operator's explicit OK. Everything else is additive and worker-safe (the always-on
> realtime worker writes `listings` + children 24/7).

## Progress

**Phase A — SHIPPED (2026-07-20, PR #831), migrations 320-328 applied.**
- **A1**: nullable `listing_id` on 22 carriers (23 columns), six migrations split by
  table group so no transaction holds ACCESS EXCLUSIVE across many hot tables. Column
  names as planned, with the three collisions taking `_ref_id`
  (`properties.repr_listing_ref_id`, `property_notes.origin_listing_ref_id`,
  `property_merge_events.listing_ref_id`). The partial `WHERE listing_id IS NULL`
  indexes were moved OUT of A1 to Phase B — before the backfill they would match every
  row, i.e. an 8M-entry index built only to drain to empty.
- **A2**: dual-write at every writer site, the surrogate always resolved IN SQL.
  `manual_rental_estimates_history` turned out to have no Python writer at all — it is
  filled by a trigger copying the OLD row, so migration 327 redefines that function and
  its dual-write comes free. All upserts also heal `listing_id` in `DO UPDATE SET`.
- **A3**: `check_dual_write_parity` in the existing `verify_pipeline` harness +
  `dual_write_watermark` (326, RLS in 328). The carrier list lives once in
  `toolkit/listing_identity.py`; parity and backfill import the same object, pinned by a
  test — a carrier in one and not the other is the exact silent hole this refactor's
  audits kept finding.
- **A4**: `scripts/backfill_child_listing_ids.py` + a dispatch-only workflow. NOT yet
  run — it must run only after the A2 deploy is live.

Two bugs were caught during Phase A that are worth remembering:
1. **The parity check was silently green when unarmed.** Its counting query is
   aggregate-only, so a carrier with no watermark row returned `(0,0,0)` — identical to
   clean. Armedness is now read from the watermark table. Found by running the generated
   SQL against production rather than trusting it.
2. **CI caught an avoidable round-trip**: `record_images`/`record_videos` resolved the
   surrogate with a separate `SELECT` and carried it through Python. Now inline, like
   every other site — the id never travels through Python where a mis-zip could point
   rows at the wrong listing.

**A4 backfill — CONVERGED (2026-07-20).** 10.7M rows filled across 22 carriers in ~30
minutes of runtime (images 8.26M, listing_snapshots 1.41M, properties 544k). Every
carrier reports zero unfilled. Three things were learned the hard way and are now
encoded:
1. **`dedup_pair_audit` cannot be fully backfilled.** It carries `CHECK
   (left_sreality_id <> right_sreality_id)` as NOT VALID, so ~4,542 historical
   self-paired rows (a dedup-engine bug, 2026-06-24..07-13, none since) are tolerated
   where they sit — but Postgres re-checks every CHECK on any row an UPDATE touches, so
   they cannot be written. They keep the legacy handle only. Carriers can now declare a
   `skip` predicate, applied to COUNTING as well as updating — skip one but not the
   other and "remaining" never reaches zero, which makes the self-chaining workflow spin
   forever.
2. **A self-chaining workflow needs its exit code and its marker to be right.**
   `| tee` without `set -o pipefail` made a CRASHING run report success (three runs died
   and relaunched each other before it was caught). And Python's logging writes to
   STDERR, so a marker grepped out of stdout-only capture was never found — the chain
   re-dispatched even at zero work. Both fixed (#835, #836); the marker is now
   `print()`ed to stdout so it does not depend on logging configuration.
3. Per-window commits mean an aborted run keeps its progress — the first failed run had
   already committed 258k correct rows.

**Phase B DONE (2026-07-20, PRs #837/#838).** `scripts/apply_r2_constraints.py`:
indexes CONCURRENTLY built on every surrogate column; 19 FKs to `listings(id)` added
NOT VALID then validated, across exactly the carriers whose legacy column already
carried one (read live from `pg_constraint`, never hardcoded — Class B ledgers never
grow one). Lock lesson worth remembering: `ADD FOREIGN KEY` takes SHARE ROW EXCLUSIVE
on child AND `listings`, while the ingest path locks the same two tables in the
OPPOSITE order (a new listing's singleton property is created inside the
listings-insert transaction) — `DeadlockDetected` is expected there, not exotic; #838
added it to the retry set alongside `LockNotAvailable`.

**Phase B2 DONE (2026-07-20, PR #839).** `scripts/apply_r2_unique_guards.py` ran
clean on the first live dispatch — no `DeadlockDetected`/`LockNotAvailable` retries
needed, 12 unique guards + 17 `CHECK` constraints all converged in ~51s. Verified
live (not just trusted the script's own log): 8/8 promoted `UNIQUE` constraints
present, 4/4 pair-cache expression indexes valid, 17/17 checks `convalidated`, 0
invalid `listing_id`-named indexes anywhere. The only two unvalidated constraints
left in the whole database are pre-existing and unrelated:
`dedup_pair_audit_distinct_listing` (deliberately permanent, §2 A4) and Supabase
Realtime's own `messages_payload_exclusive`.

What shipped — two independent additions per carrier, both derived from
the *current* (not original) legacy shape — several carriers' constraints drifted
across multiple migrations since they were first created (`listing_description_
enrichments`'s unique widened from 2 to 3 columns in mig 249;
`notification_dispatches.sreality_id` went NOT NULL→nullable and lost its unique
guard entirely across migs 096/206/274) — verified fact-by-fact against `migrations/`
before writing any DDL:
1. A new unique index mirroring the legacy one, keyed on the surrogate column(s), for
   the 12 carriers whose legacy column(s) carry a UNIQUE/PK today (declared
   explicitly — which tables get a NEW invariant is a design decision, not something
   to infer from the schema). 8 of those promote to a named `UNIQUE` constraint via
   `ADD CONSTRAINT ... USING INDEX`; the 4 pair caches (`listing_{image_comparisons,
   visual,floor_plan,site_plan}_matches`) key on `(LEAST(a,b), GREATEST(a,b)[,
   discriminators])` per §0.5 and stay **index-only forever** — Postgres's `USING
   INDEX` promotion explicitly refuses expression indexes, so there is no constraint
   form to promote to; the plain unique index alone is sufficient for both
   enforcement and Phase C's `ON CONFLICT` arbiter inference. The 4 pair tables are
   NOT uniform: `listing_image_comparisons` has no discriminator at all (a `model`
   column exists but was never in its unique key), `listing_visual_matches` has two
   (`room_type`, `model`), the other two have one (`model`).
2. A validated `CHECK (col IS NOT NULL)` (the mig-313 trick) on every `R2_CARRIERS`
   column whose legacy sibling is itself NOT NULL — derived live per-column via
   `pg_attribute` (mirrors Phase B's `_legacy_has_fk`), so it automatically covers
   the full registry with no second hand-maintained list to drift. Net effect: 13 of
   the 22 carriers get one; the 3 hot-ingest children (`images`, `listing_snapshots`,
   `listing_videos`) and the fully-nullable Class B ledgers (`dedup_pair_audit`,
   `notification_dispatches`, `estimation_runs`, `building_runs`, `properties.repr`,
   `property_notes.origin`) never had a NOT NULL legacy column and correctly get none.

**Phase C, arbiter retarget sub-step DONE (2026-07-20).** Every writer that INSERTed
against a listing-scoped carrier with an explicit `ON CONFLICT` target now arbitrates on
`listing_id`, not the legacy `sreality_id`, matching Phase B2's guards exactly:
`record_images`/`record_videos`/`_BATCH_IMAGES_SQL` → `(listing_id, sequence)`;
`listing_summaries`/`listing_condition_scores`/`listing_marker_extractions`/
`building_unit_extractions` → `(listing_id, snapshot_id)`;
`estimation_cohort_entries` → `(estimation_run_id, listing_id)`; the 4 pair caches →
`(LEAST(listing_id_a, listing_id_b), GREATEST(listing_id_a, listing_id_b)[, disc])` (a/b
values themselves are NOT re-canonicalized, per §0.5 — only the arbiter is
order-independent, DO UPDATE SET still overwrites every column from the SAME call's
fresh values). `listing_description_enrichments`'s bazos-enrichment writer already used
a targetless `ON CONFLICT DO NOTHING`, so it needed no change — it was already immune to
this failure class. The rule-2 latest-snapshot guard (`upsert_listing` +
`_BATCH_SNAPSHOT_SQL`'s LATERAL) is rekeyed onto `listing_id` too, backed by a new
composite index (mig 333, `listing_snapshots_listing_id_scraped_at_idx`, built
CONCURRENTLY live — Phase B had only given this carrier a bare `listing_id` index,
insufficient for an ORDER BY scraped_at DESC LIMIT 1 on a table written every scrape
cycle). Every retargeted arbiter was confirmed live via `EXPLAIN` against prod (each
plan reports the expected `Conflict Arbiter Indexes:`) since these statements are local
`sql = (...)` variables, not module-level `_SQL` constants — outside the offline
SQL-corpus sweep's discovery net (a pre-existing gap, not introduced here). `listings`
itself still arbitrates on `sreality_id` (unchanged) — it stays the PK until Gate 1.

**Phase C, read cutover — step 1 DONE (2026-07-20, mig 334).** `listings_public`,
`property_sources_public`, and `listing_natural_key_public` now expose `id` as a
trailing column; `listing_snapshots_public` exposes `listing_id` alongside its
own (unrelated) `id`. Purely additive — every frontend query lists explicit
columns (`DETAIL_COLS`-style, never `select('*')`), so this is invisible to every
existing consumer, confirmed live: `authenticated` SELECT grants unchanged on all
4 views, sample reads return `id` alongside the legacy `sreality_id`. Sets up,
but does NOT itself perform, the actual read cutover.

**Phase C, ListingDetail resolver chain DONE (2026-07-20, mig 335).** Investigated the
actual mechanics before touching code: `sreality_id` stays populated for EVERY row
through Gate 1 and the entire bake period — the forward-compat risk is narrower than
§4's framing suggested, and only bites a *future* non-sreality row created after Gate 2
stops drawing the synthetic sequence, which has no sreality_id to reach it by at all.
Tracing every `sid` use in `ListingDetail.tsx` found almost all of it already read
`listing.sreality_id` from the loaded row (which stays valid forever) rather than the
route resolver — `BrokerChip`/`ManualEstimatesBlock`/`FreshnessBlock`/`CurationBlock`
needed ZERO changes. The real fix was narrow:
- `fetchListingIdByNaturalKey` (canonical `/listing/{source}/{native}` route) now
  resolves the surrogate `id` (mig 334's new column), not `sreality_id`.
- The legacy `/listing/{id}` route (`fetchListingBySreality`) is UNCHANGED and stays a
  single round trip forever — the URL literally IS the sreality_id, so there's no
  forward-compat gap to close there. `listingPath()` (22 call sites app-wide) still
  generates this legacy form; the canonicalizing redirect (mig 314-era) rewrites the
  URL bar client-side after load.
- `DETAIL_COLS` (`listings_public`'s SELECT list) now includes `id`, so once
  `listingQ.data` loads — via EITHER route — `sourcesQ`/`imagesQ` key off
  `listingQ.data.id` (mig 335 added `images_public.listing_id` for this) and
  `checksQ`/the snapshot-fallback stay `listingQ.data.sreality_id`-keyed, since
  `listing_freshness_checks` has no `listing_id` column at all (rule #9 — not an R2
  carrier). `fetchPropertySources` moved from `sreality_id` to `id` too
  (`property_sources_public.id`, same column mig 334 exposed).
- Caught by tracing `listingQ`'s query-key change (`['listing', sid]` →
  `['listing', legacyId, natKeyId]`, two slots instead of one): `FreshnessBlock`'s
  "Ověřit aktuálnost" mutation invalidated the literal `['listing', sreality_id]` key,
  which no longer partial-matches the natural-key route's actual cache entry (its
  `sreality_id` doesn't sit at either new key slot) — silently stopped refreshing the
  listing after a freshness check on canonical-route pages. Fixed by invalidating the
  bare `['listing']` prefix instead of guessing the shape.
- **Verification**: the SPA is fully login-gated (Phase 1), so an agent can't complete
  an interactive Google-OAuth browser click-through. Compensated with (a) the exact
  query shapes replayed live as the `authenticated` role via the Supabase MCP
  (`SET LOCAL ROLE authenticated`) — natural-key resolve → id → listing → sources →
  images, full chain, real row (`idnes`/`sreality_id=-11876`/`id=105053`); (b) new
  resolver-chain tests in `ListingDetail.test.tsx` mocking `@/lib/queries` and
  rendering both route shapes via `MemoryRouter`, asserting the right loader fires
  with the right argument for each route (5/5 pass); (c) `tsc --noEmit` + `vitest run`
  (372 passed) + `eslint` (0 errors) all clean. A human click-through on both URL
  formats is still worth doing before/shortly after merge, but isn't a hard blocker
  given the above.

**Phase D, step 1 DONE (2026-07-20).** Retargeted the `listings` table's own ingest
`ON CONFLICT` — the two sites §5.1 named, `upsert_listing` (db.py:559) and
`_BATCH_UPSERT_SQL` (db.py:1949) — from `sreality_id` to `(source, source_id_native)`.
Verified live before editing: `listings_source_native_uidx` is a full (non-partial)
UNIQUE INDEX so arbiter inference always succeeds; `listings_source_id_native_present`
is a validated CHECK with 0 NULLs across 562,681 rows; `_listing_update_set_sql`'s
generated SET clause excludes `sreality_id`/`source`/`source_id_native` entirely (the
latter is COALESCE-healed on a separate line), so a conflict on the new arbiter can
never rewrite a frozen identity column. Confirmed via `EXPLAIN (COSTS OFF)` against
prod for both call shapes (`upsert_listing`'s explicit `source` value and
`_BATCH_UPSERT_SQL`'s implicit column DEFAULT — Postgres materializes defaults before
arbiter evaluation, so the default case resolves correctly too): both plans show
`Conflict Arbiter Indexes: listings_source_native_uidx`. Full local pytest green
(2813 passed, 30 skipped). **Still on the synthetic sequence for new rows — this step
only changes which index the conflict check uses, not what gets written.** Per §5.1,
this needs to **bake ≥1 full scrape cycle across all 9 portals in production** before
being considered validated; that observation happens post-merge, not in this PR.

**Next: Phase D, steps 2-7** (child `DROP NOT NULL` + the two child PK swaps, the
`sreality_id`/`id` unique indexes needed before the Gate 1 swap, the 19 legacy child
FK drops, the parity-green precondition check) — see §5. In parallel, Phase C's
remaining read cutover (§4 second half) is still open: browse hydration, dedup
`ListingKey` + pair-cache reads, merge/unmerge replay, notification producers
(incl. the `new_source` dedupe_key NULL-concat fix), the Chrome extension app-link
gate + redistribution, `image_key()`, estimation forward provenance, the
sreality_id-cursored maintenance walkers (geocode/street/geo_cell partial indexes),
then the 25-read-model "may lag" wave. `brokers.ts` and `api.ts`'s manual-estimates
path turned out to need NO changes (see above) — drop them from the checklist.

## 0. What the review corrected (read this first)

1. **The doc's R2→R3 order is backwards.** Backfilling children before dual-write deploys
   means the always-on writer refills `listing_id IS NULL` forever and parity can never
   converge. Correct order per table: **add column → deploy dual-write → backfill the
   now-frozen tail → index → FK**. (R1 didn't have this problem only because the parent
   column had a sequence DEFAULT; child columns have no default.)
2. **"Promote the existing UNIQUE on id to PK" is unexecutable.** `listings_id_key`'s index
   is constraint-owned (mig 313) — PG 17 refuses to attach a PK to it, and R2's child FKs
   will bind to it, so it can never be dropped. A **second, fresh unique index on `id`**
   must be pre-built `CONCURRENTLY` for the PK promotion; `listings_id_key` is kept forever.
3. **`ALTER COLUMN sreality_id DROP NOT NULL` is missing from the doc entirely.** Dropping
   the PK does NOT clear `attnotnull` on PG 17; without this statement the first NULL write
   fails regardless of the sign CHECK. It can only run after the PK drop, in the same tx.
4. **The destructive gate is aimed at the wrong step.** The PK-swap tx is catalog-only and
   reversible (re-promote a PK on `sreality_id` USING the retained unique index while zero
   NULL rows exist). The true point of no return is the **writer deploy that stops drawing
   `synthetic_listing_id_seq`** — the first NULL-`sreality_id` row is data the old key can
   never re-accept. → **Two gates** (§6, §8), each with a fresh pg_dump.
5. **Pair caches: do NOT physically re-canonicalize a/b.** Swapping sides must move
   side-coupled payloads (visual_match.py:120-121, image_similarity.py:168-169 store
   side-ordered image lists) in lockstep across 4 tables — high corruption risk, zero need.
   Instead: order-independent uniqueness via
   `CREATE UNIQUE INDEX … ON t (LEAST(listing_id_a,listing_id_b), GREATEST(…), <disc>)`,
   legacy columns + their CHECK/UNIQUE stay frozen-valid, ON CONFLICT retargets to the
   LEAST/GREATEST expressions at cutover. New code must not assume `listing_id_a <
   listing_id_b`.
6. **The doc's writer census is wrong in both directions** — three named "writers" write
   nothing (broker resolver drains; image drain only UPDATEs; `_resolve_input` only
   resolves), and it omits real sites (see §3 table). The verified census below replaces it.
7. **`notification_dispatches` drifted out from under the doc** (mig 274): `sreality_id` is
   now NULLABLE, the once-ever guard is `UNIQUE(dedupe_key)` — NOT `UNIQUE(subscription_id,
   sreality_id)`, which no longer exists. Post-flip it fails **silently** (NULL rows
   inserted), not loudly. One real trap: the `new_source` producer's `dedupe_key` concat
   (api/notifications.py:1790) goes NULL → NOT NULL violation aborts that whole pass.
8. **The read-model surface is ~3× the doc's list**: 25 live views/matviews reference the
   listings identity (10 matviews + 15 views; §5). Two more relations reference an
   *unrelated* `admin_boundaries.sreality_id` — leave those alone.
9. **Two column-name collisions**: `property_merge_events.listing_id` already exists holding
   legacy sreality values, and `properties.repr_listing_id` likewise. Pick one convention
   for the new columns on these two tables (suggested: `listing_ref_id` /
   `repr_listing_ref_id`) — never reuse/rename in place.
10. **Three tables can't take the generic recipe**: `dirty_broker_listings` (PK IS
    `sreality_id`; reclass to Class D — ephemeral queue, no backfill/parity, PK swap to
    `listing_id` pre-flip), `estimation_cohort_entries` (`sreality_id` inside composite PK —
    needs a PK migration), `property_merge_events` (name collision above + replay must
    COALESCE old/new rows).
11. **Two carriers the doc's census missed entirely** (critic pass):
    `listings.condition_levels_propagated_from` (mig 174) — a bigint *inside listings
    itself* holding `= s.sreality_id` (stamped by condition_scoring.py:333, read by the
    sibling-heal in backfill_condition_scores.py:313); invisible to FK walks. Dormant
    (scoring paused) but must repoint before scoring resumes. And
    `manual_rental_estimates_history.sreality_id` (NOT NULL) — the append-only twin of
    manual_rental_estimates; needs the same column + writer treatment.
12. **The pg_dump gate as specced is the wrong tool.** The DB is 63 GB (snapshots 14 GB,
    listings 7.7 GB); a full logical dump takes tens of minutes to >1h, is stale the moment
    it finishes, and a restore would lose all always-on-writer data since. The right net:
    **Supabase PITR timestamp + `pg_dump --schema-only` + data dump of only the small
    Class-B carriers** (§6, §8).

**Step 0 of the track:** merge PR #813, commit this file next to it, and fold these
corrections into `listing-identity-refactor.md` (mark its R2–R5 runbook superseded) — so
the on-disk doc future sessions load by default is not the known-wrong version.

## 1. Verified starting state (live, 2026-07-20)

- `listings`: 557,472 rows; PK `listings_pkey(sreality_id)` unchanged; `id` bigint DEFAULT
  `nextval('listings_id_seq')` (seq at 10,062,205), UNIQUE `listings_id_key`, NOT NULL via
  validated CHECK only (`attnotnull=false` — matters for the swap); `source_id_native` 0
  NULLs, enforced via validated CHECK (also `attnotnull=false`); sign CHECK live;
  `synthetic_listing_id_seq` at −382,238 and still being drawn.
- FK graph: exactly **19 FK columns / 15 tables**, all on `listings(sreality_id)`, none on
  `id`. Heavy: `images` 8.08M, `listing_snapshots` 1.27M. Unindexed FK cols (only two):
  `properties.repr_listing_id` (541k rows), `property_notes.origin_listing_id` (1 row).
- NOT NULL child legacy cols: 14 of 19 (nullable: images, listing_snapshots, listing_videos,
  properties.repr_listing_id, property_notes.origin_listing_id).
- Pair caches (`listing_{floor_plan,image_comparisons,site_plan,visual}_matches`): each has
  `CHECK(sreality_id_a < sreality_id_b)` + `UNIQUE(a, b[, model|room_type])`.
- Class B carriers confirmed: `property_merge_events.listing_id` (NOT NULL, 88.6k),
  `dedup_pair_audit.left/right_sreality_id` (nullable, 81.2k, **no pair-unique** — reader
  repoint only), `notification_dispatches.sreality_id` (nullable, 813, guard =
  `UNIQUE(dedupe_key)`), `estimation_runs.input_sreality_id` (nullable),
  `building_runs.input_sreality_id`, `estimation_cohort_entries.sreality_id` (composite PK),
  `listing_description_enrichments.sreality_id` (NOT NULL, `UNIQUE(sreality_id, snapshot_id,
  model)`), plus the two critic finds: `manual_rental_estimates_history.sreality_id`
  (NOT NULL) and the listings-internal `condition_levels_propagated_from` (§0.11).
- `estimation_runs.input_listing_id` **does not exist** (target-design item 6 not started).
- Nothing reads `listings.id` yet; `listings_public` doesn't expose it;
  `listing_natural_key_public` (mig 315) exposes only `sreality_id, source,
  source_id_native` — the natural-key URL resolver **round-trips through `sreality_id`**
  (queries.ts:1054 → ListingDetail.tsx:87 `sid`), i.e. the shipped URL cutover is cosmetic;
  internal identity is still `sreality_id` everywhere.
- `listings` is in NO publication; `relreplident='d'` — no logical-decoding consumer to
  disturb at the swap.
- 0 orphans, 0 NULL legacy ids on images/snapshots — backfill joins are total; FK VALIDATE
  will pass.
- `listing_detail_queue` is already flip-safe (keys `(source, native_id)`, tolerates NULL
  `sreality_id`) — no work until R5.

## 2. Phase A — additive columns + dual-write + backfill (R2a+R3 interleaved, autonomous)

**A1. Migration(s): add nullable `listing_id` (no default — instant) to:**
all 15 Class A tables (pair tables get `listing_id_a`/`listing_id_b`; the two collision
tables get the chosen alt name), plus Class B: `property_merge_events` (alt name),
`dedup_pair_audit.left/right_listing_id`, `notification_dispatches.listing_id`,
`estimation_runs.input_listing_id`, `building_runs.input_listing_id`,
`estimation_cohort_entries.listing_id`, `listing_description_enrichments.listing_id`,
`manual_rental_estimates_history.listing_id`.
Also add partial indexes `ON <heavy child>(<pk>) WHERE listing_id IS NULL` (images,
listing_snapshots) — they make parity index-only AND drive the backfill.
Skip: `dirty_broker_listings` column comes with its PK-swap migration in §6-prep; Class C
golden stores get columns for new-freeze stamping only; Class D queues get nothing.
**Each ALTER in its own transaction** under `SET lock_timeout='3s'` + bounded retry — never
one tx holding 20 ACCESS EXCLUSIVE locks (a single `apply_migration` runs in one tx; split
into per-table migrations or run via a session-mode script).

**A2. PR: R3 dual-write, ALL writer sites at once** (per-table completeness matters more
than PR granularity; both insert sites of each high-volume child MUST ship together):

| Table | Sites to patch |
| --- | --- |
| images | scraper/db.py:932 `record_images` + db.py:2067 `_BATCH_IMAGES_SQL` (add `JOIN listings l ON l.sreality_id=j.sreality_id`, SELECT `l.id` — in-tx safe, upsert ran first) |
| listing_snapshots | db.py:616 `upsert_listing` (change `RETURNING (xmax=0)` → `RETURNING (xmax=0), id`; pass id to the snapshot INSERT) + db.py:1967 `_BATCH_SNAPSHOT_SQL` (already JOINs listings — just SELECT `l.id`) |
| listing_videos | db.py:977 `record_videos` + scripts/backfill_listing_videos.py:33 |
| properties.repr | db.py:769 `_create_singleton_property` (already SELECTs from listings — add `id`), toolkit/property_identity.py:243 split-insert, scripts/recompute_property_stats.py:80 attach + :302 repr-recompute UPDATE |
| property_notes.origin | api/curation.py:284 |
| manual_rental_estimates | api/manual_estimates.py:55 (+ the history-append writer for `manual_rental_estimates_history` — executor locates the parallel site) |
| analytical caches | toolkit/condition_scoring.py:739, condition_markers.py:503, summaries.py:353, building_extraction.py:636 |
| pair caches | toolkit/visual_match.py:318/576/857, toolkit/image_similarity.py:345 |
| dedup_pair_audit | api/property_dedup.py:354 + scripts/dedup_engine.py:1685 |
| property_merge_events | toolkit/property_identity.py:147 (stamp legacy AND new) |
| notification_dispatches | api/notifications.py:1239, 1546, 1670, 1705, 1752, 1790 (six sites; system_alerts.py:57 carries no listing id — skip) |
| estimation_runs | api/estimation_runs.py:1731 `_insert_run` + api/notifications.py:954/984 + scripts/smoke_agent.py:208 |
| building_runs | api/building_runs.py:982 |
| estimation_cohort_entries | api/agent.py:960 |
| listing_description_enrichments | toolkit/bazos_enrichment.py:351 + :365 |

Rules: resolve the id **in SQL in the same tx** (JOIN or RETURNING), never by zipping
Python-side RETURNING order (`INSERT…SELECT RETURNING` order is unspecified — silent
misalignment). NOT writers (do not touch): broker resolver, image drain (UPDATEs only),
`_resolve_input`. `dirty_broker_listings` writers (db.py:718 + 2061) swap at §6-prep, no
dual-write. Note `condition_scoring.propagate_condition_levels` (:333) stamps
`listings.condition_levels_propagated_from = s.sreality_id` — a listings column, not a
child; repoint in the R4 wave.

**A3. PR: parity check** — extend the existing harness, don't invent one:
`check_dual_write_parity` added to `_CHECKS` in scripts/verify_pipeline.py (runs 6-hourly
via verify_pipeline.yml, rings the in-app bell on red). Per table:
- `backfill_pending` (informational, shrinks): `legacy IS NOT NULL AND listing_id IS NULL
  AND cursor <= watermark`;
- `writer_gap` (MUST be 0, alarms): same predicate with `cursor > watermark` — catches ANY
  missed writer, censused or not;
- `mismatched` (MUST be 0): scalar carriers `listing_id <> (SELECT l.id FROM listings l
  WHERE l.sreality_id = child.legacy)`; pair carriers **set-membership**, not positional.
Watermark = one row per table captured at A2 deploy (id for serial-PK tables; `dispatched_at`
for notification_dispatches — its uuid PK is unordered; `created_at` for cohort entries).
Predicate is always `legacy IS NOT NULL AND …` — never bare `listing_id IS NULL` (legit-NULL
legacy rows would false-fire forever, and it stays correct post-flip).

**A4. Backfills** (script PR: `scripts/backfill_child_listing_ids.py`, template =
backfill_listing_surrogate_id.py **with two fixes**): keyset-paginate the child PK with a
rising watermark (`WHERE id > :last ORDER BY id LIMIT n`) instead of re-scanning NULLs
(O(batches²) on 8M rows); terminate on `count(*) WHERE listing_id IS NULL AND legacy IS NOT
NULL = 0`, not on "batch updated 0" (SKIP LOCKED can return an empty batch while stragglers
are worker-locked); run on **`db.connect_session()`/direct** — the transaction pooler's
~2-min statement_timeout has killed long jobs twice before. Order: small tables → snapshots
(1.27M) → images (8.08M, off-peak). Expect **non-HOT** updates on the heavies (fillfactor
100, packed pages): ~1× transient heap bloat + churn in all 8 images indexes + WAL bursts —
temporary autovacuum bump on the child, pace batches, VACUUM after.

**Mis-stamp correction recipe** (if parity's `mismatched` fires — a writer stamped the
WRONG id): revert the buggy writer, then re-run that table's backfill with
`WHERE listing_id IS DISTINCT FROM l.id` (join on the legacy column) — overwriting wrong
non-NULL values, which the standard NULL-only backfill would leave in place — then parity
back to green before proceeding.

## 3. Phase B — indexes, FKs, new unique guards (still additive, autonomous)

Per table, after its backfill converges (parity `backfill_pending = 0`):
1. `CREATE INDEX CONCURRENTLY` on `listing_id` (+`_a/_b`). Give
   `properties.repr_listing_ref_id` and `property_notes` their first-ever index here.
2. FK `ADD … REFERENCES listings(id) NOT VALID` — brief SHARE ROW EXCLUSIVE on **both**
   child and listings; `lock_timeout` + retry, one per tx (19 of these; a pileup on listings
   freezes ingest). Old FK and new FK coexist fine.
3. `VALIDATE CONSTRAINT` — non-blocking (SHARE UPDATE EXCLUSIVE child / ROW SHARE parent)
   but IO-heavy; run on session-mode with `statement_timeout=0`, off drain peaks; images
   takes minutes.
4. New unique guards **alongside** the old (all CONCURRENTLY):
   - images/videos: `UNIQUE(listing_id, sequence)`;
   - analytical caches: `UNIQUE(listing_id, snapshot_id[, model])`;
   - enrichments: `UNIQUE(listing_id, snapshot_id, model)`;
   - pair caches: `UNIQUE (LEAST(listing_id_a,listing_id_b), GREATEST(listing_id_a,
     listing_id_b), <disc>)` — no positional CHECK, no physical re-canonicalization (§0.5);
   - cohort: `UNIQUE(estimation_run_id, listing_id)`.
5. Per-child validated `CHECK (listing_id IS NOT NULL)` (mig-313 trick) once parity is
   clean — this is the gate for §4's ON CONFLICT retargets.

## 4. Phase C — writer ON CONFLICT retargets + remaining R4 read cutover (autonomous)

**Retarget writers' arbiters** to the new uniques (only after §3.5, else arbiter-miss
`unique_violation` wedges the drain — the #825 failure class): images/videos
`(listing_id, sequence)`; caches `(listing_id, snapshot_id[, model])`; pair caches the
LEAST/GREATEST expressions; cohort `(estimation_run_id, listing_id)`. Also rekey the
rule-2 latest-snapshot guard (db.py:603 `WHERE sreality_id=%s`) onto `listing_id`.

**Read cutover, grouped PRs** — split by *must precede the flip* vs *may lag*:

MUST precede flip (the flip-gate checklist, §8):
- Frontend resolver chain: redefine `listing_natural_key_public` to ALSO expose `id`
  (additive; partially reverses the doc's "exposing id is moot" claim — moot for URLs, NOT
  for the internal resolver), expose `id` on `listings_public` + sibling `*_public` views,
  key `sid`/loaders/`.eq('sreality_id')` reads on it (ListingDetail.tsx:87,
  queries.ts:1041–1439, brokers.ts, api.ts manual-estimates path).
- Browse hydration + repr: browse_projection/browse_list carry the surrogate; hydration
  `.in('sreality_id', …)` (queries.ts:1188/1214/1424) moves to id — else every post-flip
  card renders blank (repr goes NULL). 4-surface browse contract + property-singleton
  display mirror apply.
- Dedup: `ListingKey.sreality_id` (non-nullable int, dedup_engine.py:274) → listing_id;
  self-pair guards; image_similarity sorted() canonicalizer → LEAST/GREATEST; pair-cache
  + pHash read chains (property_dedup.py:695–905).
- Merge/unmerge replay: property_identity.py:305/333/409 must COALESCE — prefer the new
  column, else resolve legacy → id via join (ledger holds pre-deploy legacy-only rows).
- Notifications: producers write listing_id; **fix the `new_source` dedupe_key NULL
  concat** (notifications.py:1790 → key on listing_id or source||':'||source_id_native);
  read joins (notifications.py:639, notification_outbox.py:174/223).
- Chrome extension: move the app-link **gate** off `l.sreality_id` (content.ts:565 → the
  `found`/source_id flag portal_lookup already returns) + note write :1381
  (`origin_listing_id`).
- `image_key()` write argument (image_storage.py:40) → id for NEW images only.
- Estimation **forward provenance**: beyond `input_listing_id`, the estimation builder +
  trace must emit `listing_id` for COMPARABLES (`comparables_used/excluded`,
  `input_spec.exclude_ids`, cohort rows) — else every post-flip estimation touching the
  68.5% non-sreality inventory loses comparable provenance in its immutable trace. (Frozen
  old JSONB stays resolvable forever — legacy ids never NULLed.)
- Chrome extension **redistribution**: the extension is hand-shipped `dist/` with no
  auto-update — rebuild + redistribute to every operator + confirm installs. An old build
  keeps the sreality_id gate and mis-writes notes post-flip; there is no server-side fix.
- **Maintenance walkers cursored on `sreality_id`** (missed by the doc): the live partial
  indexes `geocode_candidates`, `street_name_key_null`, `geo_cell_key_null` ×2,
  `source_active_street` are btree(sreality_id) and their backfill scripts page by it —
  recreate on (id) + swap script cursors, else geocode/street/geo_cell enrichment silently
  stops for exactly the new rows.
- `condition_levels_propagated_from` stamp (condition_scoring.py:333).

MAY lag briefly (degraded, not broken — but health should still go before the flip to avoid
silent-green): the 25-read-model wave — health matviews (136/176/214/216; post-flip NULL
rows silently vanish from health counts = silent-green blind spot), dedup/broker/image
matviews (`dedup_funnel_resolutions_mv`, `dedup_llm_cost_by_category_mv`,
`broker_region_type_stats`, `category_trends_mv`, `image_storage_overview_mv`,
`images_failure_overview_mv`, `properties_map_mv`, `snapshot_churn_24h_mv`), remaining
`*_public` views, `dedup_label_events`, `portal_lookup` collapse onto `input_listing_id`
(+ `property_estimates_public` on COALESCE), ClickUp payloads (unaffected — carry sreality
URLs). NEVER touch: `srealityListingUrl()` stays bound to `source_id_native`
(frontend + extension portals.ts) — renaming it to any surrogate emits 404 sreality.cz links.

## 5. Phase D — pre-flip preparation (additive, autonomous)

1. PR: ingest **ON CONFLICT → `(source, source_id_native)`** — exactly two SQL sites:
   `upsert_listing` (db.py:559) and `_BATCH_UPSERT_SQL` (db.py:1949). Verified safe today:
   the unique index is full (non-partial) so arbiter inference works, and
   `_listing_update_set_sql` excludes `sreality_id`/`source`, so a natural-key-arbitrated
   conflict can never clobber a frozen id. **Keep drawing the synthetic sequence in this
   deploy.** Bake ≥1 full scrape cycle across all 9 portals.
2. Migration: relax the 14 NOT NULL child legacy columns (`DROP NOT NULL` each, own tx);
   PK swaps: `dirty_broker_listings` PK → `(listing_id)` (+ writer swap db.py:718/2061,
   no backfill — drain the queue first), `estimation_cohort_entries` PK →
   `(estimation_run_id, listing_id)`. Both tiny.
3. `CREATE UNIQUE INDEX CONCURRENTLY listings_sreality_id_uidx ON listings(sreality_id)`
   — **mandatory before the swap**: the PK is currently the ONLY unique index on
   sreality_id; without the replacement, post-swap `ON CONFLICT (sreality_id)` code errors
   instantly (total ingest outage) and the rollback lever disappears. NULLS DISTINCT
   (default) is correct.
4. `CREATE UNIQUE INDEX CONCURRENTLY listings_id_pk_idx ON listings(id)` — the fresh index
   the PK will be promoted from (`listings_id_key` cannot take a second constraint and can
   never be dropped once child FKs bind to it — keep it forever).
5. `ALTER TABLE listings ALTER COLUMN id SET NOT NULL` — instant (PG proves it from the
   validated CHECK; no scan). Own retried tx.
6. Drop the 19 legacy child FKs — **one per transaction**, `lock_timeout` + retry, days
   before the window (each takes ACCESS EXCLUSIVE on child AND listings). Integrity is held
   by the new listing_id FKs. FK drops are re-addable (`NOT VALID` → `VALIDATE`) — not
   gate-destructive.
7. Parity green (writer_gap = mismatched = 0 across all tables) is a hard precondition.

## 6. Phase E — GATE 1: the PK-swap window (pg_dump + operator OK)

**Backup first — but the right one.** The DB is **63 GB** (listing_snapshots 14 GB,
listings 7.7 GB, images-table 2.7 GB); a full logical pg_dump takes tens of minutes to
>1 h, is stale the instant it finishes, and restoring it would lose all always-on-writer
data written since. The gate's safety net is instead: (a) **confirm Supabase PITR is
enabled and record the exact pre-window UTC timestamp** as the recovery target; (b)
`pg_dump --schema-only` (seconds) so constraints/PK can be diffed or rebuilt; (c) a data
dump of ONLY the small Class-B carrier tables (notification_dispatches, estimation_runs,
estimation_cohort_entries, property_merge_events — all <100k rows). State in the gate ask
that the swap tx itself is **reversible in-place** (§ rollback below) — the backup guards
the data operations around it, not the catalog swap. (Optional prep: several dead
`*_backfill_backup` / `*_pre204_backup` tables inflate the 63 GB and can be pruned first —
destructive, so its own operator OK.)

Run the window from a **script over a direct/session-mode connection** — NEVER the
Supabase MCP (pooled ~2-min statement_timeout; memory records MCP statements timing out
client-side yet COMMITTING — a half-observed destructive tx is the worst outcome).

Choreography: set `REALTIME_WORKER_ENABLED=false` (Railway restart, takes minutes) → wait
for `worker_heartbeats` to stop → confirm no active `scrape_runs` → pause **every pg_cron
job whose body touches listings** — jobids 1/6/7/8 (health matviews, rebuild_browse_list,
properties_map, dedup_funnel) **plus jobid 3** (data_quality_snapshots, computed FROM
listings) and **verify jobid 5** (emit_verification_stale_alert) and whichever job
refreshes the other listings-joining matviews (broker_region_type_stats, category_trends_mv,
image/snapshot overview mvs) — prove each is paused, don't assume the 4-job list → dodge
the hourly GH cron scrapers (check `gh run list`; remember the **SHA-freeze gotcha** —
verify each scheduled run's checkout SHA, a queued run can execute pre-merge code) → then
ONE catalog-only transaction:

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';           -- bounded retry loop around the whole tx
ALTER TABLE listings DROP CONSTRAINT listings_pkey;
ALTER TABLE listings ADD CONSTRAINT listings_pkey PRIMARY KEY USING INDEX listings_id_pk_idx;
ALTER TABLE listings ALTER COLUMN sreality_id DROP NOT NULL;
COMMIT;
```

Catalog-only (no scans — id's NOT NULL is already real). Then resume worker/cron. Old
writers keep working unchanged: they still draw synthetic negatives (valid under the sign
CHECK) and, having deployed §5.1, arbitrate on the natural key; even un-redeployed
`ON CONFLICT (sreality_id)` code keeps inferring from `listings_sreality_id_uidx`.

**Rollback (while zero NULL rows exist):** re-promote `PRIMARY KEY USING INDEX
listings_sreality_id_uidx`, re-add legacy FKs NOT VALID → VALIDATE. Document this in the
gate ask so the operator's OK is informed.

**Skip the `GENERATED ALWAYS AS IDENTITY` conversion** (recommended — the plain sequence
default is functionally identical; revisit with the properties.id polish). If ever done:
own short window, single tx: `DROP DEFAULT` → `DROP SEQUENCE listings_id_seq` →
`ADD GENERATED ALWAYS AS IDENTITY` → `setval(…, GREATEST(max(id), old last_value))` — a
gap between DROP DEFAULT and ADD IDENTITY wedges the drain (worker never supplies id).

## 7. Phase F — bake

Days, not hours. Watch: parity stays green; ingest volumes per portal normal
(`scrape_runs`, health matviews — now id-keyed); no `unique_violation` in drain logs;
browse cards render; dedup lanes process. New rows still carry synthetic negative
`sreality_id` — nothing user-visible changed yet.

## 8. Phase G — GATE 2: stop drawing the synthetic sequence (fresh PITR timestamp +
schema/carrier dumps + operator OK)

**This is the true point of no return** (first NULL-`sreality_id` row = data the old key
can never re-accept). Precondition: every item on the §4 "MUST precede flip" checklist
verified live — including the extension `dist/` redistribution confirmed installed —
parity green, §7 bake clean. Same backup recipe as GATE 1 (fresh PITR timestamp +
schema-only + small-carrier dumps).

Deploy the flip writer **behind a config flag** (instant rollback for future rows): new
non-sreality rows stop drawing `synthetic_listing_id_seq` and insert `sreality_id = NULL`;
sreality rows unchanged (real positive ids forever — the sign CHECK enforces all of it).

**Safe abort** (if post-flip breakage with NULL rows already written): flip the flag back
(or: pause worker → `UPDATE listings SET sreality_id = nextval('synthetic_listing_id_seq')
WHERE sreality_id IS NULL` — valid under the sign CHECK ELSE arm — → redeploy the previous
**post-R3 dual-write** image → resume). The UPDATE must precede any writer rollback: a
rolled-back writer's ON CONFLICT (sreality_id) never matches NULL and the duplicate INSERT
dies on the natural-key unique → drain wedge. This abort assigns NEW synthetic ids to those
rows — allowed; the freeze constraint covers existing values, and no child rows reference
the aborted NULLs by sreality_id (children are id-keyed by now).

## 9. Phase H — R5 cleanup (deferrable indefinitely; own gates)

Only after weeks of clean bake: repoint Class C golden-set replay tooling (fetches media
via `images.sreality_id`) to resolve golden sreality_id → listings.id → images.listing_id,
and prove it by re-running one golden set; THEN optionally drop legacy child columns (their
partial `WHERE listing_id IS NULL` indexes go with them); drop `synthetic_listing_id_seq`
**last** — it is the §8 abort lever. `listings.sreality_id` itself is NEVER dropped or
NULLed for existing rows. Keep `listings_id_key` + `listings_sreality_id_uidx` forever.

## 10. Testing, CI mechanics & rehearsal

- **SQL-correctness CI gate obligations** (tests/test_sql_schema_prepare.py replays every
  migration from zero, then PREPAREs discovered SQL): (a) each dual-write PR must land its
  `ADD COLUMN` migration **in the same PR** as the SQL referencing it, or the replay-PREPARE
  fails — the #825 deploy-order rule, but for CI; (b) dynamically-built SQL
  (`_listing_update_set_sql`, the `_BATCH_*_SQL` concatenations, the LEAST/GREATEST
  ON CONFLICT expressions) may be undiscoverable — budget one allowlist edit per such PR.
- **String-assertion test lockstep**: 71 test files reference sreality_id; many `_FakeConn`
  tests assert exact SQL substrings (e.g. test_condition_scoring asserts
  `condition_levels_propagated_from = s.sreality_id`). Map each surface PR to its paired
  test files and update them in lockstep — and treat `_FakeConn` green as illusory for
  constraint behavior (it cannot catch CHECK/UNIQUE/FK/arbiter-inference violations).
- **New gated live-schema invariant tests** to add (the repo's tests/*live*.py pattern):
  every new listing_id FK is `convalidated`; the four pair caches carry the functional
  `UNIQUE(LEAST, GREATEST, disc)` index; post-flip `sreality_id` is nullable and `id` is
  the PK; the sign CHECK holds with NULL-sreality_id rows present.
- **Rehearse both windows** (§6 DDL + §8 flip + §8 abort) on a Supabase branch or local PG
  17 with the schema replayed and a synthetic concurrent writer, before production.
- **Same-PR doc/skill updates** (CLAUDE.md rule; docs-budget.yml warns when skipped) —
  scope INTO the phase PRs, not a trailing cleanup: `docs/architecture.md` rule 2 (latest-
  snapshot guard keys listing_id), rule 15 + the dedup design docs (ListingKey + pair-cache
  re-key), rules 16/18 (notification_dispatches + operator_state repoint), rules 12/13
  (`input_listing_id`); `database` skill (session-mode-only heavy backfills, lock_timeout +
  retry on listings ALTERs, the PK-swap window recipe); `scraper-ops` (arbiter move,
  drain-wedge + silent-row-explosion failure modes, flip-window worker pause);
  `toolkit-api` (estimation join + notification producers); `llm-pipelines` (analytical
  caches keyed listing_id+snapshot_id). If the track is committed, ROADMAP index cell + a
  `roadmap/<track>.md` entry ride the first PR. Merge PR #813 first (Step 0, §0).

## Provenance

Review artifacts (9-agent workflow `wf_5023e764-c1e`, 2026-07-20): 4 census agents (live
DB via Supabase MCP + code at `6fe8b42`), 4 attack agents (PK-swap DDL, flip cascade,
backfill scale, R3 parity), 1 completeness critic. All 12 critic findings are folded into
the sections above (§0.11–12, §2 A4 correction recipe, §4 provenance/extension items,
§6/§8 backup + cron corrections, §10 CI mechanics).
