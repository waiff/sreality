> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

## Listing identity track

Retire the `sreality_id` smart key (one bigint doing three jobs: surrogate PK, one
portal's natural key, and — via its sign — an implicit source discriminator) in favour of
a clean surrogate `listings.id` for joins and the natural key `(source,
source_id_native)` for external references. Diagnosis, target design, and the full
carrier census: `docs/design/listing-identity-refactor.md`. The executable runbook for
everything still open: `docs/design/listing-identity-r2-pk-swap-runbook.md` (the old
doc's R2–R5 runbook is superseded — a 9-agent adversarial review found four broken steps).

### Shipped (2026-07-19 → 07-20)
- **Phase 0** (#817, mig 311) — `source_trust_rank()` SQL fn + Python mirror replacing
  four inconsistent inline orderings; the sign↔source CHECK turning folklore into an
  enforced invariant; `dedup_label_events` + `property_estimates_public` redefines; the
  portal-lookup estimation-join collapse; frontend raw-id leak fixes.
- **R1** (#818, migs 312/313) — the clean surrogate `listings.id`: sequence-backed
  (epoch 10,000,000), all pre-existing rows backfilled in `(first_seen_at, sreality_id)`
  order, UNIQUE + validated present-CHECK. PK deliberately stays on `sreality_id`.
- **Natural-key completion** (#820 + #825 fix, mig 314) — `(source, source_id_native)`
  was incomplete *and actively regressing* (the sreality detail-drain never stamped it).
  Now stamped inline at INSERT on all three write paths, backfilled, and enforced.
- **R4 app-layer cutover** (#821–#824 + #826 fix, mig 315) — canonical
  `/listing/{source}/{native_id}` route with a permanent legacy resolver + canonicalizing
  redirect, notification-outbox and Chrome-extension deep links on the natural key,
  resolution over the unfiltered `listing_natural_key_public` view.

### Open — the R2 → PK-swap track (in progress)
Run as ONE committed track; valueless half-done. Phases and their gates are specified in
`docs/design/listing-identity-r2-pk-swap-runbook.md`:
- **Phase A** (✅ shipped 2026-07-20, PR #831, migs 320-328) — additive `listing_id`
  columns on 22 carriers, dual-write at every writer site, `check_dual_write_parity` in
  `verify_pipeline` anchored on a per-carrier `dual_write_watermark`, and the child
  backfill script + dispatch workflow. Order is load-bearing: backfilling before
  dual-write ships can never converge against the always-on worker. The backfill itself
  still has to be run to convergence.
- **Phase B** (✅ shipped 2026-07-20, PRs #837/#838/#839) — `CREATE INDEX CONCURRENTLY`,
  FK `NOT VALID` → `VALIDATE` (images is 8.08M rows; #838 added `DeadlockDetected` to the
  retry set alongside `LockNotAvailable` — the ingest path locks child+`listings` in the
  opposite order). New unique guards alongside the old: 8 carriers promote to a named
  `UNIQUE` constraint, the 4 pair caches key on `(LEAST(a,b), GREATEST(a,b)[, disc])` and
  stay index-only forever (Postgres refuses to promote an expression index to a named
  constraint). 17 per-child validated NOT NULL CHECKs, derived live from which legacy
  columns are themselves NOT NULL. All verified live: 20 FKs + 8 unique constraints + 4
  pair indexes + 17 checks, zero left unvalidated anywhere but two unrelated pre-existing
  ones.
- **Phase C** (arbiter retarget sub-step ✅ shipped 2026-07-20, mig 333) — every writer
  into a listing-scoped carrier now arbitrates `ON CONFLICT` on `listing_id`, matching
  Phase B2's guards (images/videos `(listing_id, sequence)`; the four snapshot-keyed
  analytical caches `(listing_id, snapshot_id)`; the cohort table
  `(estimation_run_id, listing_id)`; the four pair caches on the order-independent
  `LEAST/GREATEST` expression index without re-canonicalizing stored a/b). The rule-2
  latest-snapshot guard is rekeyed onto `listing_id` too, backed by a new composite
  index built CONCURRENTLY (`listing_snapshots_listing_id_scraped_at_idx`) since Phase
  B only gave that carrier a bare `listing_id` index. Every retarget verified live via
  `EXPLAIN`. Read-cutover step 1 (✅ shipped 2026-07-20, migs 334/335) additively
  exposes `id`/`listing_id` on `listings_public`, `property_sources_public`,
  `listing_natural_key_public`, `listing_snapshots_public`, `images_public` — invisible
  to every current reader, since the frontend always selects explicit columns, never
  `*`. The ListingDetail resolver chain (✅ shipped 2026-07-20) is DONE: the canonical
  `/listing/{source}/{native}` route now resolves the surrogate `id` instead of
  `sreality_id`; the legacy `/listing/{id}` route is unchanged (the URL already IS the
  sreality_id, no forward-compat gap there). Turned out narrower than expected —
  `BrokerChip`/`ManualEstimatesBlock`/`FreshnessBlock`/`brokers.ts`/`api.ts` needed no
  changes, since they already read `listing.sreality_id` from the loaded row. Verified
  via live `authenticated`-role query replay (agent can't complete an interactive
  Google-OAuth click-through) + new resolver-chain tests + clean `tsc`/`vitest`/`eslint`.
  **Still open:** browse hydration, dedup `ListingKey` + pair caches, merge/unmerge
  replay, notification producers, `image_key()`, the sreality_id-cursored maintenance
  walkers, 25 read models.
- **Phase D** — pre-flip prep: ingest `ON CONFLICT` → the natural key, child `DROP NOT
  NULL`s, the two child PK swaps, pre-built unique indexes on `sreality_id` and `id`.
- **GATE 1** — the PK-swap window (`sreality_id` → `id`), catalog-only and reversible.
- **GATE 2** — stop drawing `synthetic_listing_id_seq` (the true point of no return).
- **Phase H / R5** — optional cleanup, deferrable indefinitely. Existing `sreality_id`
  values are NEVER dropped or NULLed — frozen, valid, unique, permanently resolvable.
