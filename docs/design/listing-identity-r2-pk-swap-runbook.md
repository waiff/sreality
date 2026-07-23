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
3. **(2026-07-23, Gate-2 wave-5 tail) The "always `legacy IS NOT NULL`" predicate above
   was necessary but not sufficient.** It correctly keeps the *gap* and *mismatch*
   buckets from false-firing on legit-NULL-legacy rows — but it also means, taken alone,
   those two buckets go **completely blind** to any row whose legacy id is NULL once
   Gate-2 flips (new non-sreality-portal rows, by design). A writer bug that stamps
   NEITHER id on such a row would report 100% clean forever. `check_dual_write_parity`
   now carries a third bucket, `orphans` (`legacy IS NULL AND listing_id IS NULL`), that
   watches exactly that shape — existence-only (there's no legacy value left to
   cross-check the surrogate against). Same fix in `check_merge_latency`: it now joins
   `dedup_pair_audit` on `left_listing_id`/`right_listing_id` (populated for every row,
   legacy or not) instead of `left_sreality_id`/`right_sreality_id`.

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

**Phase D, steps 2-6 DONE (2026-07-20, PRs #853/#854).** Two new dispatch-only
scripts/workflows, both idempotent and re-run-safe:

- `apply_r2_phase_d_prep.py` — (a) drops `NOT NULL` on every `R2_CARRIERS` legacy
  column still enforcing it, derived live via the same `_legacy_column_not_null`
  predicate Phase B2 used (reused, not reimplemented) — **17 columns, not the
  runbook's estimated 14**: the design-time number predated checking
  `pg_attribute`, same class of drift Phase A's own audits kept finding (see the
  Phase 0/A section above). (b) Swaps `estimation_cohort_entries`'s PK from
  `(estimation_run_id, sreality_id)` to `(estimation_run_id, listing_id)` — safe
  immediately, no writer-deploy dependency, since Phase A4 already backfilled its
  `listing_id` to 100%. Needed a **fresh** dedicated unique index
  (`estimation_cohort_entries_run_listing_id_pk_idx`), not a reuse of Phase B2's
  `..._run_listing_id_key` — an index already owned by one constraint can't back a
  second, the same reason this runbook's §5.4 pre-builds `listings_id_pk_idx`
  instead of reusing `listings_id_key`. (c) Pre-builds `listings_sreality_id_uidx` /
  `listings_id_pk_idx` CONCURRENTLY + `listings.id SET NOT NULL` — §5 steps 3-5,
  Gate 1's prerequisites. A first live dispatch hit
  `InvalidTableDefinition: column "sreality_id" is in a primary key`: the generic
  NOT NULL loop ran BEFORE the `estimation_cohort_entries` PK swap, and Postgres
  refuses `DROP NOT NULL` on a still-PK-bound column. 16 of 17 columns had already
  succeeded (the loop checks live state per-column, so it's naturally idempotent);
  reordering the two calls (PK swap first) and re-dispatching fixed it cleanly —
  fixed in PR #854, which also added migrations 337/338 as the plain-form tracking
  records (mirroring migration 333's pattern; **Phase B/B2 never got this
  treatment and have no tracking migration at all** — a pre-existing gap, not
  something this PR tries to retroactively fix).

  **Ledger + replay divergence (standing note, from the 2026-07-21 audit).** Two
  distinct facts to keep straight: (1) migrations **337/338/339** are repo files
  only — they were applied via the dispatch-only workflow scripts and have NO row
  in `supabase_migrations.schema_migrations` (333–336 all DO — the earlier
  "cf. migration 333" framing was imprecise; 333 was MCP-applied and ledgered,
  its index merely pre-built CONCURRENTLY). A ledger-driven `supabase db push`
  would see 337/338/339 as PENDING and re-apply them; 339 is NOT a no-op on
  already-swapped prod (`CREATE UNIQUE INDEX IF NOT EXISTS
  dirty_broker_listings_listing_id_key` would build a brand-new duplicate index —
  the original was renamed to `dirty_broker_listings_pkey` by `ADD CONSTRAINT …
  USING INDEX`). Never `db push` these against the production project. (2) A
  **fresh replay of `migrations/` cannot reproduce prod anyway**: the Phase B FK
  graph (19 `listing_id → listings(id)` FKs) and every Phase B2 guard (12 unique
  indexes/constraints, 4 pair-cache expression indexes, 17 validated NOT NULL
  CHECKs) exist only via `apply_r2_constraints.py`/`apply_r2_unique_guards.py` —
  no migration file creates them. Disaster recovery for the R2 window is
  PITR/base-backup, NOT migration replay; a faithful-rebuild path would need
  plain-form Phase B/B2 migrations backfilled first.
- `drop_r2_legacy_fks.py` — drops the 19 legacy child FKs onto
  `listings(sreality_id)`, read live off `pg_constraint` (matched the runbook's
  count of 19 exactly, unlike the NOT NULL count above). Integrity is already held
  by the parallel `listing_id -> listings(id)` FKs Phase B validated. Verified live:
  0 legacy FKs remain.

**Gate 1 is now recorded as `migrations/348_listings_pk_swap_catchup.sql` (2026-07-21).**
The swap ran live through `apply_listings_pk_swap.py`, so `001_initial.sql`'s
`sreality_id bigint primary key` was still what the CI schema replay rebuilt — meaning
the Gate 2 migration, which needs `sreality_id` nullable, would have aborted in replay
("column sreality_id is in a primary key") on a change that is a no-op against prod.
348 is a **catch-up**: every step is guarded on `pg_constraint`/`pg_attribute`, so it
is a true no-op (and takes no lock on `listings`) against the already-swapped
production database, and it is deliberately NEVER applied there. It closes the replay
divergence only for the PK itself — the broader "a fresh replay cannot reproduce prod"
caveat above (Phase B/B2 constraints have no plain form) still stands.
`tests/test_listings_pk_swap_migration.py` is the offline guard that the chain keeps
reaching it.

**Phase D, step 7 (parity-green precondition) CONFIRMED (2026-07-20)**, via
`verify_pipeline.yml`'s `check_dual_write_parity`: `status=ok value=0` — zero gap,
zero mismatch — across all 22 armed `R2_CARRIERS` (all 22 have a
`dual_write_watermark` row; none unarmed).

**`dirty_broker_listings`'s own PK swap — DONE (2026-07-21, PRs #857/#859).** This
table isn't an `R2_CARRIERS` member (no pre-existing `listing_id` column, no Phase
A4 backfill), so it got its own migration (336: nullable `listing_id` dual-write
column) + writer-code dual-write at both `INSERT` sites — shipped in PR #853. It
was deliberately held back at that point: checked live ~10 minutes post-merge, a
genuine MIX of old-code (no `listing_id`) and new-code (populated) writes was
observed, because this table's two writer sites are hit by both the always-on
realtime worker (redeploys in minutes) AND the per-portal GH Actions cron
scrapers, which are subject to the **SHA-freeze gotcha** (§6's Gate 1
choreography already calls this out for a different reason) — a run queued
before the merge still executes the pre-merge code, so full rollout isn't
instant even though the code is merged. Re-checked ~6.5 hours later (well past
even the slowest 6h-cadence portal's cycle): **zero** rows anywhere in the table
had a NULL `listing_id` — the fleet had fully rolled over. Landed in two PRs, in
the order the #825 lesson demands: **#857** (schema only — a new dispatch-only
`apply_dirty_broker_listings_pk_swap.py`/`.yml`, defensive backfill + `listing_id
SET NOT NULL` + PK swap `sreality_id` → `listing_id` + `sreality_id DROP NOT
NULL`, dispatched live and verified — `dirty_broker_listings_pkey` now
`PRIMARY KEY (listing_id)`), merged and dispatched BEFORE **#859** (the writer-code
retarget, both `ON CONFLICT` sites moved from `(sreality_id)` to `(listing_id)`,
verified via `EXPLAIN` against prod: `Conflict Arbiter Indexes:
dirty_broker_listings_pkey`) — reversing the order would have deployed code
whose arbiter had no matching index yet. **Phase D is now fully complete**; every
§5 prerequisite for Gate 1 is met.

**Honest caveat found by the 2026-07-21 post-Phase-D audit — the chosen order was
NOT symmetric-risk-free either.** A PK swap (unlike an additive dual-write) is the
one step where BOTH orders carry a window: the schema swap dropped the ONLY
unique index on `dirty_broker_listings.sreality_id` at 05:14:17Z, so any OLD-code
writer still executing `ON CONFLICT (sreality_id)` between then and its own
redeploy raised 42P10 ("no unique or exclusion constraint matching the ON
CONFLICT specification") at plan time and aborted its whole enclosing
transaction — and #859 didn't merge until 05:18:27Z, with the Railway worker
restarting ~05:21:25Z: a ~7-minute broken window for the two enqueue sites
(sreality batch drain + idnes ingest). Audited outcome: a brief idnes/snapshot
write dip, fully recovered by queue retry, zero rows lost — acceptable ONLY
because the table's writers redeploy in minutes and the failure mode is a
retried transaction, not data loss. **Guardrail for every FUTURE carrier PK
swap (and Gate 1 itself):** there is no zero-window ordering with only
(old-index, new-index-as-PK) states; if a window matters, keep BOTH unique
indexes live through the transition — build the new unique index, deploy code
arbitrating on it, THEN swap the PK and drop the old index — or accept and
time-box the window deliberately, during a paused-writer window (§6 already
pauses writers for Gate 1, which is why Gate 1 does not inherit this hazard).

**Phase C read cutover — MOSTLY DONE (2026-07-21, PRs #866-#879, migs 343/344).**
Ten PRs, each verified against prod rather than reasoned about. What shipped, and
the failure each one actually prevented:

- **#870 `exclude_ids` NULL-safety — the severest finding of the whole sweep, and
  a CORRECTNESS bug, not a provenance one.** `l.sreality_id <> ALL(...)` is
  three-valued: for a post-flip listing it evaluates to NULL and a WHERE keeps
  only TRUE, so the predicate SILENTLY DELETES those rows from the cohort rather
  than merely failing to exclude them. `_build_target` puts the run's own subject
  into `exclude_ids`, so it is non-empty on essentially EVERY listing-anchored
  estimation — post-flip nearly every estimate would quietly draw its cohort from
  sreality-only inventory and shift the price distribution, with a green run.
  Guarded, plus a surrogate-keyed `exclude_listing_ids` twin. Same PR fixed
  transit_axis's `PARTITION BY l.sreality_id`, which would have collapsed every
  non-sreality listing into ONE partition and kept exactly one of them (`rn = 1`)
  for the whole axis cohort.
- **#866 notifications**: the `new_source` dedupe_key concatenated a bare
  `sreality_id`; `dedupe_key` is NOT NULL and `||` yields NULL on any NULL operand,
  so the first post-flip listing would have aborted the ENTIRE collection-monitor
  pass with a not-null violation — every collection stops notifying, not just that
  row. COALESCEd onto the surrogate (existing keys stay byte-identical, so nothing
  already dispatched re-fires). Also found while tracing it: `_MONITORED_CTE` gated
  on `p.repr_listing_id IS NOT NULL`, silently dropping exactly the new-portal
  properties out of monitoring; now gates on the surrogate. Plus 7 read joins.
- **#867 broker queue CONSUMER** — the audit's MEDIUM, uncensused until PR #861.
  Also fixed two `count(DISTINCT coalesce(property_id, -sreality_id))` rollups
  (count(DISTINCT) skips NULLs → silent undercount).
- **#873 (mig 343) browse read model**: `properties_public`'s repr join moves onto
  the surrogate — this IS the "repr goes NULL" failure (price_unit/floor/broker_*/
  description/street fallback all NULL, cards render blank). `browse_projection`
  and `listing_broker_public` expose the surrogate. Verified: 548,498/548,498
  properties carry `repr_listing_ref_id` and it resolves identically; 50,000
  sampled rows, 0 mismatches on every repr-supplied field.
- **#874 merge/unmerge/split replay**; **#875 image R2 key + drain shard** (the key
  rendered the literal `"None/0001.jpg"` — every non-sreality image colliding on
  one prefix and overwriting; and `hashint8(NULL) % n = k` is NULL, so those images
  matched NO shard and would never have downloaded at all); **#876 cohort emits
  listing_id + snapshot LATERAL** (rule 8); **#877 (mig 344) maintenance walkers**
  (five partial indexes + four backfill scripts re-keyed END TO END, not just at
  the cursor — a half-swap pages one id-space while updating another); **#878
  extension gate + note write + portal_lookup** (its estimation-join DISCRIMINATOR
  had to move too, else every resolved non-sreality listing routes into the fragile
  URL-string-equality arm); **#879 agent cohort keying + comparable provenance**
  (`int(l["sreality_id"])` is a TypeError on None — the agent DIES; and
  `_persist_cohort_entries` resolved a NOT NULL column through the legacy key
  inside a bare `except`, so provenance would vanish with a green run).

**Still open in §4 — three groups, in priority order:**

1. **Dedup identity chains (4 PRs, the biggest remaining item) — ALL FOUR SHIPPED.**
   **PR1 (#883, mig 345), PR2 (#884, mig 346), PR3 (#889), and PR4 are done.** Do NOT start the
   rest piecemeal: all four pair caches carried `CHECK (sreality_id_a <
   sreality_id_b)`, and 77% of rows sort DIFFERENTLY by surrogate (56,375 rows
   checked; positional mirroring is 100% clean, so the read cutover is provably
   behaviour-preserving — it is the WRITE order that is blocked). Required order:
   (PR1 — DONE) migration dropping the 4 CHECKs + adding `listing_id_a/_b` to
   `dedup_batch_requests`;
   (PR2 — DONE) make the batch spool identity-agnostic FIRST, because Anthropic
   batches take up to 24h to return and `custom_id` is a persisted UNIQUE key whose
   scheme changes — prefix the new form (`cmpL-`/`splL-`/`fplL-`) or flush the spool,
   else in-flight requests get re-spooled and DOUBLE-BILLED. **Live state made this
   far cheaper than feared and the window is now closed behind us:** the spool was
   EMPTY (0 `batch_id IS NULL`, 0 `status='pending'`; all 18,250 rows terminal, last
   ingest 2026-07-16) and its gate `dedup_engine_batch_defer_enabled` has no
   `app_settings` row and defaults false — the engine has never deferred, so nothing
   could be in flight. Mig 346 encodes that as an executable pre-condition (RAISEs if
   any row is `pending`) instead of a remembered one. `custom_id` is now built inside
   `toolkit/dedup_batch_defer.py` (callers no longer pass one) from the SURROGATE
   under `clsL-`/`cmpL-`/`splL-`/`fplL-`, using **min/max** so it is invariant under
   the positional order of `(a, b)` — which is what stops PR3's re-canonicalisation
   from re-spooling anything. The columns still follow the caller positionally,
   because `build_*_request` orders the payload's image sides to match. The writer
   accepts EITHER id-space and resolves the other (two UNIONed index arms, not an
   OR — an OR of two arms full-scans this table), so PR3 can pass `listing_id` here
   without touching this module again; `sreality_id_a` lost its NOT NULL for the
   same reason. **PR3 trap this exposed:** the pair is canonicalised INDEPENDENTLY at
   four layers (engine defer site, `build_*_request`, cache lookup, persist) — all on
   `sorted()` over sreality_id today. Change one without the others and the payload's
   image sides stop matching the columns (swapped `n_images_a/_b`, swapped A/B in the
   prompt); and `sorted()` over a NULL sreality_id raises outright post-Gate-2;
   (PR3) the core swap, which
   must be ATOMIC across dedup_engine + image_similarity + visual_match + clip_dedup
   — the chain `ListingKey → closure(a,b) → cache SQL → in-memory dict key` is one
   id-space and splitting it produces SILENT wrong answers, not errors (the pHash
   dict lookups `.get(..., 0)` their default, so the free fast-path just stops
   firing); (PR4 — DONE) read surfaces — see below for what it actually found
   (not the two anticipated traps, which turned out already closed).

   **PR3 — DONE.** `ListingKey` gained a `listing_id` field, populated by both
   `scripts/dedup_engine.py` loading SELECTs (`_ELIGIBLE_COLS` + `_cell_eligible_sql`
   append `l.id`). All four layers now canonicalise by `LEAST/GREATEST(listing_id)`
   atomically: `resolve_pair`'s in-run `seen_listing_pairs` dedup, the `_build_*_fn`
   defer sites (pass `listing_id_a`/`listing_id_b` straight to
   `enqueue_deferred_request`, dropping the sreality_id round-trip PR2 made
   optional), `toolkit/image_similarity.py` (`compare_listing_images` keeps its
   agent-facing `sreality_id_a/_b` signature — this tool has its own external
   contract, tracked separately under "LLM tool schemas" below — but resolves
   `listing_id` once via a single `listings` SELECT and canonicalises on it),
   `toolkit/visual_match.py` (all 3 function groups × 6 entry points — `sreality_id_a/_b`
   AND `listing_id_a/_b` are now both required params; canonical order is decided by
   `listing_id`, sreality_id/image-id-list ride side-coupled with whichever
   listing_id they belong to), and `toolkit/clip_dedup.py` (`pair_max_cosine` now
   takes `listing_id_a/_b` and joins `images.listing_id`, no ordering change needed —
   it was already a symmetric MAX aggregate). The 3 `_cache_lookup` SQLs moved from
   exact `sreality_id_a = %s AND sreality_id_b = %s` to
   `LEAST/GREATEST(listing_id_a, listing_id_b)`; every `_cache_store`/persist now
   writes `sreality_id_a/_b` in its `ON CONFLICT ... DO UPDATE SET` (previously
   omitted — the exact bug §0.5 point 5 flagged). `scripts/ingest_dedup_batch.py`
   (not originally named in this bullet, but required once the persist signatures
   changed) now selects and forwards `listing_id_a/_b` from `dedup_batch_requests`.

   **Deliberately deferred, not silently dropped:** (a) `api/property_dedup.py`
   (the `/dedup` operator panel's evidence reader, `decision_evidence` /
   `_phash_audit_chunk`) is a pure READ surface over `dedup_pair_audit`'s own
   already-nullable `left/right_sreality_id` columns — it does no canonicalisation
   and rides with PR4 ("read surfaces"), not PR3 — see the PR4 writeup below for
   what that surface actually turned out to need. (b) The `_ProbeCache` /
   per-listing image-fact helpers in `scripts/dedup_engine.py`
   (`_phash_pairs_cached`, `_clip_incomplete_any`, `_downloads_incomplete_any`,
   `_last_evidence_at`, `_both_have_site_plan`, `_floor_plan_ids_cached`,
   `_high_render_image_ids`) still query `images.sreality_id` — a separate,
   larger surface (7+ functions over the `images` table, not the 4 pair-cache
   tables PR1/PR2 prepared); `images.listing_id` is already populated
   (migration 320) so this is a clean mechanical follow-up, just out of PR3's
   scope. (c) `classify`'s defer site (`_build_classify_fn`) still passes
   `sreality_id_a` alone to `enqueue_deferred_request` — classify has no pair to
   canonicalise (single listing), so there is no ordering trap there; left as-is.

   **PR4 — DONE.** The two anticipated traps (reads needing `LEAST/GREATEST`,
   and the pair-cache `DO UPDATE SET` omitting `sreality_id_a/_b`) turned out to
   already be closed: PR3 fixed the `DO UPDATE SET` omission on all four caches,
   and `api/property_dedup.py` — PR4's named target — never queries the four
   pair caches directly (it reads `dedup_pair_audit`, which per mig 322's own
   comment has no unique constraint on its pair columns and needs "a reader
   repoint only — no guard to replace"). Census instead found a DIFFERENT, real
   pre-Gate-2 read-surface bug of the same "legacy handle instead of the
   surrogate" shape as #873's Browse fix: `properties.repr_listing_id` (legacy,
   sreality-valued) and `repr_listing_ref_id` (the surrogate FK to `listings.id`,
   mig 323) are in sync TODAY (0/551,293 properties diverge, verified live) but
   only because every repr listing still carries a legacy `sreality_id` value
   (real or synthetic) — post-Gate-2 that stops being guaranteed. Three read
   surfaces still joined the repr listing on `listings.sreality_id =
   properties.repr_listing_id`: `api/property_dedup.py`'s `list_candidates` (the
   review card's `source`/`source_url`/`description`), `api/curation.py`'s
   `get_collection` (a collection's property rows' `source`), and
   `scripts/recompute_property_stats.py`'s `_PUBLISH_INELIGIBLE_SQL` (the
   publication-gate sweep — a miss here would leave non-sreality-repr properties
   permanently unpublished post-Gate-2, since there's deliberately no timeout
   sweep to catch them later). All three repointed onto `listings.id =
   properties.repr_listing_ref_id` (already indexed:
   `properties_repr_listing_ref_id_idx` + the `listings` PK), matching #873's
   pattern exactly. `api/notifications.py`'s collection-monitor CTE already did
   this correctly (gates on `repr_listing_ref_id IS NOT NULL`) — the template
   this PR copied. **Still open, NOT part of this 4-PR chain** (unchanged from
   (b) above): the `_ProbeCache` surface — verified live this is NOT a live bug
   today (`images.sreality_id`/`images.listing_id` are both 0/8,344,823 NULL,
   fully populated and reliably joinable), but repointing onto `images.listing_id`
   is the established target pattern (`toolkit/clip_dedup.py` already does this)
   and remains a mechanical follow-up whenever it's picked up.
2. **Browse FRONTEND hydration** (the DB half shipped in #873). `.in('sreality_id', …)`
   at queries.ts ~1182/1212/1238/1448 → the surrogate; React keys and the maplibre
   feature id; `listingPath()` fallback for a card whose repr has no sreality_id
   (Browse links are GENERATED, not stored, so `/listing/null` is reachable);
   `listings_with_city_quality()` must DROP+CREATE to return both ids (re-grant
   after — a dropped function loses its grants). **Trap:** `fetchPropertyReprId`
   must NOT simply return the surrogate — `listingPath()` builds the LEGACY route,
   and the two id-spaces overlap numerically, so it would silently load the WRONG
   listing. Return the natural key and redirect canonically.
3. **May-lag read models** (health/dedup/broker/image matviews, remaining `*_public`).
   `listing_fetch_failures` is the highest-leverage blocker — it has no carrier
   column and gates three health matviews. Health should still precede Gate 2 to
   avoid a silent-green blind spot. Lowest value, do last: `dedup_label_events` —
   note `property_merge_events.listing_id` is SREALITY-valued despite its name.

Also still open: the LLM tool schemas still name `sreality_id` (a contract change
touching `skills.system_prompt`, an operator-edited DB row currently drifted from
its git mirror — wants tolerant input handling so a stale prompt degrades rather
than breaks).

**Gate 1 is NOT blocked by any of the above** — those all gate Gate 2. Every §5
prerequisite for Gate 1 is met; it needs only the operator window (§6).

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
legacy rows would false-fire forever). **Correction (2026-07-23, see the Phase A bug list
above, item 3): "stays correct post-flip" was true for THESE two buckets but incomplete —
it also meant they went totally blind to legit-NULL-legacy rows, a live monitoring gap once
Gate-2 flips. A third bucket, `orphans` (`legacy IS NULL AND listing_id IS NULL`), now covers
that shape.**

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
- **Broker-resolver queue CONSUMER** (found by the 2026-07-21 post-Phase-D audit — this
  session re-keyed `dirty_broker_listings`'s producers + PK onto `listing_id` but the
  sole consumer still runs in sreality_id space): `resolve_brokers.py` `_CLAIM_DIRTY`
  (:537 `SELECT sreality_id …`), `_DELETE_DIRTY` (:538 `DELETE … WHERE sreality_id =
  ANY(…)`), and the `_attribute`/`_link` joins (:983/:985 `l.sreality_id = ANY(…)`) —
  re-key all onto `listing_id` (now the PK). Post-flip a non-sreality enqueue writes
  `sreality_id = NULL`: the claim returns NULL, the DELETE never matches NULL → broker
  attribution silently stops for new rows AND the queue leaks undeletable rows. Minor
  today: the DELETE also lost its index with the PK swap (seq scan; harmless at this
  table's size, self-fixes with the re-key).

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

> **OPERATOR HANDOFF — Gate 1 is ready as of 2026-07-21.** Every technical
> prerequisite in §5 is met and verified live. What remains is not code; it is
> the four things only the operator can do. In order:
>
> 1. **Confirm Supabase PITR is enabled and RECORD the exact pre-window UTC
>    timestamp.** This is the recovery target and the whole safety net — a 63 GB
>    logical dump is not the backup for this (tens of minutes, stale the instant
>    it finishes, and restoring it loses every always-on-writer row since).
> 2. **Take the two small dumps**: `pg_dump --schema-only` (seconds), plus data
>    for the small Class-B carriers only (`notification_dispatches`,
>    `estimation_runs`, `estimation_cohort_entries`, `property_merge_events` —
>    all <100k rows).
> 3. **Say go.** The swap transaction itself is catalog-only and reversible
>    in-place (re-promote `PRIMARY KEY USING INDEX listings_sreality_id_uidx`,
>    re-add the legacy FKs `NOT VALID` → `VALIDATE`); the backups guard the data
>    operations AROUND it, not the swap. There are zero NULL `sreality_id` rows,
>    which is exactly the condition that makes rollback trivial.
> 4. **Be reachable for the window** — pausing the realtime worker needs a
>    Railway env change (`REALTIME_WORKER_ENABLED=false`), which an agent cannot
>    do. The rest of the choreography below (pg_cron pauses, the GH-cron
>    SHA-freeze check, the transaction, resume) is scriptable.
>
> **HOW it runs (built 2026-07-21):** `scripts/apply_listings_pk_swap.py` +
> the dispatch-only `apply_listings_pk_swap.yml`. An agent session has NO local
> DB path — no `psql`/`pg_dump` binary, no `SUPABASE_DB_URL` in the shell — and
> the MCP is forbidden for the window (below), so the workflow, which holds the
> repo secrets, is the execution vehicle. Same pattern as Phase D's
> `apply_dirty_broker_listings_pk_swap`. Modes: `preflight` (read-only, verifies
> every §5 gate + both quiet signals), `window` (pause pg_cron → re-verify →
> swap → verify → resume, resume in a `finally`), `resume-cron` (emergency
> lever), `rollback`. `window`/`rollback` need `confirm=APPLY`. NOTE a new
> `workflow_dispatch` 404s until it is merged to the default branch.
>
> **Live preflight, 2026-07-21:** all §5 gates PASS — PK still
> `PRIMARY KEY (sreality_id)`; `listings_sreality_id_uidx` and
> `listings_id_pk_idx` both present, valid, unique; `listings.id` NOT NULL; **0**
> legacy FKs on `sreality_id` and **19** surrogate FKs on `id`. pg_cron is
> exactly **six** jobs (1,3,5,6,7,8) — that IS the complete listings-touching set
> the §6 text says to prove rather than assume, and the script pauses whatever it
> finds active rather than a hardcoded list. Operator enabled PITR the same day
> (7-day window); recovery target recorded as **2026-07-21T12:31:00Z**.
> Careful with the Supabase restore picker: it labels the zone "(UTC+01:00)"
> (standard time) while Europe/Prague is on CEST (+02:00) in July.
>
> **Do NOT wait for the remaining §4 items.** Browse frontend, the dedup chains,
> the may-lag read models and the LLM schemas all gate **Gate 2**, not Gate 1.
> Gate 1 keeps `sreality_id` populated on every row, so nothing that still reads
> it breaks.
>
> One caveat carried forward from the 2026-07-21 audit: a PK swap has **no
> zero-window ordering** in general (see the `dirty_broker_listings` note in the
> Progress section). Gate 1 is exempt *because* this choreography pauses the
> writers — which is precisely why the pause steps are not optional.

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
