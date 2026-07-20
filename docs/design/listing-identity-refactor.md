# Listing & property identity: retire the `sreality_id` sign-hack — v2

> ⚠️ **Its § Migration runbook (R2–R5) is SUPERSEDED — do not execute it.** A 9-agent
> adversarial review on 2026-07-20 found four of its steps broken (R2-before-R3 ordering
> can never converge against the always-on writer; the PK promotion via `listings_id_key`
> is unexecutable; `ALTER COLUMN sreality_id DROP NOT NULL` is missing entirely; the
> destructive gate is on the wrong step) plus census drift. The corrected, live-verified,
> execution-ready runbook is **`docs/design/listing-identity-r2-pk-swap-runbook.md`** —
> read that for anything R2 / R3 / remaining-R4 / PK-swap. Everything else in this
> document (diagnosis, target design, carrier census, decision framework, shipped record)
> stands and remains the reference.

> **Status: v2 — Phase 0, R1, the natural-key completion, AND the full R4 app-layer
> cutover SHIPPED to production (2026-07-19 → 07-20). Only R2 + the PK swap remain,
> and both are deferrable polish (see below).** Supersedes v1 (2026-07-17) in full. v1
> was adversarially audited on 2026-07-19 by a 17-agent workflow (live DB via Supabase
> MCP, current code at HEAD `b0084b9`, plus three attack agents on the migration
> plan); the shipped work (#817–826) was itself re-reviewed by a 9-agent adversarial
> workflow on 2026-07-20 that caught two regressions (both fixed, see below). v1's
> *diagnosis* survived; its *census* and *runbook* did not — five hard Postgres
> blockers and ~17 missed identity-carrying tables. This document keeps what was
> verified, corrects what was wrong, and re-derives the plan.

## Shipped so far (production)

**Phase 0 — PR #817, merged, migration 311 live + verified.** Closed all seven live
correctness bugs and turned the sign convention into an enforced invariant, with no
re-key: `source_trust_rank()` SQL fn + `toolkit/source_trust.py` mirror (one trust
order, replacing four inconsistent inline orderings) wired into every
representative-sibling selector; the sign↔source CHECK (`listings_sreality_id_sign_check`,
validated clean over 555k rows, forward-compatible form); `dedup_label_events` +
`property_estimates_public` view redefines; the `portal_lookup` estimation-join
collapse; the calibration-sampling fix; and the frontend id-leak fixes (Notifications /
Watchdog place labels, ListingOverview / CollectionDetail source gating, the
non-sortable Browse "ID" column with graceful preset fallback).

**R1 — PR #818, merged, migrations 312 + 313 live + verified.** The clean surrogate
`listings.id` now exists: sequence-backed (epoch ≥ 10,000,000 for new rows), all
556,750 pre-existing rows backfilled to `row_number() OVER (first_seen_at, sreality_id)`
(1..556,750, 0 NULL, all distinct — ascending id tracks chronology), UNIQUE constraint
`listings_id_key` (built `CONCURRENTLY`), validated `CHECK (id IS NOT NULL)`. The PK
stays on `sreality_id`; nothing reads `listings.id` as identity yet. The backfill ran
via `scripts/backfill_listing_surrogate_id.py` (batched `FOR UPDATE … SKIP LOCKED`, so
the always-on writer was never deadlocked — the naive approach *did* deadlock).

Operational lessons confirmed live and folded into the runbook below: every `ALTER
TABLE listings` catalog lock contends with the always-on writer and needs a short
`lock_timeout` + retry to catch a gap; a sequence-backed column default burns a
sequence value on every *upsert* (the `ON CONFLICT` INSERT arm evaluates the default
before detecting the conflict) — harmless given the 10M epoch gap, and confirmed
`listings.id ∉ LISTING_COLUMNS` so upserts never clobber a backfilled id.

**Natural-key completion — PR #820 (+ #825 fix), migration 314 live + verified
(2026-07-20).** § Current design below claims `(source, source_id_native)` "already
exists" as the true natural key. **It did not** — validating that assumption found it
*incomplete and actively regressing*: the sreality detail-drain (`write_detail_batch`,
the primary sreality write path since the cadence split) built its row from
`LISTING_COLUMNS`, which carries neither `source` nor `source_id_native`, and skipped
the `_ensure_property` self-heal, so every sreality listing first-written by the drain
got NULL `source_id_native` (35 at the audit → 421 by fix time). Phase 0 *planned* the
backfill + NOT NULL but never shipped it. Fix: stamp the full natural key **inline at
INSERT** on all three write paths (`upsert_listing`, `ingest_scraped_listing`,
`write_detail_batch`); backfill; enforce via validated `CHECK (source_id_native IS NOT
NULL)` (`listings_source_id_native_present`, same trick as mig 313's id-check).
**#825** then fixed a HIGH regression the inline stamp introduced — `source` was still
only on the post-insert UPDATE, so a non-sreality first-sight insert transiently wrote
`('sreality', <native_id>)` and could collide with a real sreality row on
`UNIQUE(source, source_id_native)` (which `ON CONFLICT (sreality_id)` doesn't
arbitrate) → `unique_violation` → drain wedge; the fix stamps `source` inline too. Net:
`(source, source_id_native)` is now **complete, unique, and enforced** across all
~557k rows — the foundation the rest of this design assumes.

**R4 app-layer cutover — PRs #821–824 (+ #826 fix), migration 315 live (2026-07-20).**
The remaining value this doc identified (below) is now done, *without* R2 or the PK
swap: a canonical **`/listing/{source}/{native_id}`** route with a permanent legacy
`/listing/{id}` resolver and a *canonicalizing redirect* (legacy → natural-key on land,
query + hash preserved) so the negative id never shows in the URL bar (#821); the
notification-outbox deep link (#822) and chrome-extension "open in app" link (#823) at
the natural key; stale-doc fixes (#824). No `listings_public` change was needed —
resolution rides an unfiltered `listing_natural_key_public` view (migration 315, #826,
which fixed a MEDIUM gap where the first resolver used the `property_id`-filtered
`property_sources_public` and 404'd freshly-scraped listings). `listings.id` is *not*
yet exposed on views/API — no consumer needs it, and the natural key (not the
surrogate) is what URLs use, so that R4 sub-item is moot.

## The reframing that makes R2 + the PK swap optional

Because v2 **freezes legacy `sreality_id` values forever** (never NULLed), `sreality_id`
stays a **populated, unique, valid join key permanently**. That collapses the case for
the expensive middle of the plan:

- **R2 (repoint 19 FK columns / 15 child tables to `listing_id`, incl. an 8.08M-row
  `images` backfill and a 1.27M-row `listing_snapshots` backfill) is deferrable polish,
  not correctness.** Child tables can keep joining on `sreality_id` indefinitely; it
  never goes away. The repoint buys naming honesty and the eventual ability to drop the
  redundancy — nothing the platform needs while single-operator.
- **The PK swap (`sreality_id` → `id`) is likewise optional.** It requires either
  dropping every child FK (`CASCADE`) or recreating each against a new `sreality_id`
  UNIQUE constraint — i.e. it *forces* R2. Since `listings.id` already has its own
  UNIQUE constraint, the app can treat `id` as the identity **without** `id` being the
  physical PK.

**The design's actual remaining value was the R4 *app-layer* cutover, not R2 or the PK
swap — and that cutover is now SHIPPED** (see "Shipped so far" above): natural-key
share/detail URLs with a permanent legacy resolver, and the chrome-extension +
notification-outbox links pointed at an id that exists for every portal. That is where
the negative-id contract leaked externally; R2's physical repoint never did. The one
remaining R4 sub-item — exposing `listings.id` on `*_public` views/API — is moot: no
consumer needs it, and URLs ride the natural key, not the surrogate.

**Recommendation (unchanged, now with R4 done): treat R2 + the PK swap as an explicitly
deferred, possibly-never cleanup track.** After Phase 0 + R1 + the natural-key
completion + the R4 app-layer cutover, **zero live defects remain and no negative id
leaks to any surface**; child tables joining on the frozen, valid, unique `sreality_id`
is not debt. R2 (repoint 19 FK cols/15 tables incl. the 8.08M-row `images` backfill)
delivers no value until carried all the way through the destructive PK swap (which
*forces* R2) — so it is valueless half-done and should only be undertaken as a single
committed track, gated on operator backup + explicit OK for the destructive step per
the database skill. Recommended only if a public/multi-user surface later makes the
physical smart-key PK a real liability.

## Verdict (unchanged in substance, corrected in detail)

`listings.sreality_id` is a textbook **smart-key anti-pattern**: one bigint doing three
jobs — global surrogate PK, mirror of one portal's natural key, and (via its sign) an
implicit source discriminator. All seven concrete bugs v1 attributed to it were
re-verified as real and still unfixed on today's HEAD, and the audit found more
(§ Confirmed bugs). The fix — a clean surrogate `listings.id`, the pattern this schema
already uses correctly for `properties.id` and `images.id` — remains reasonable and
possible, **but v1's plan was not executable as written and understated the blast
radius by roughly half**. The corrected plan below is fully additive at every step:
its single most important design change is that **legacy synthetic ids are frozen
forever, never NULLed** — which removes v1's only destructive step, protects every
frozen artifact (golden sets, audit ledgers, sent notification URLs), and makes the
whole migration unhurried.

Equally important, the audit's independent second opinion holds: **every live
correctness bug is code-only fixable today** (Phase 0), and an additive CHECK can turn
the sign convention from tribal knowledge into an enforced invariant with zero re-key.
The re-key (Phases R1–R4) is therefore a deliberate *foundation* investment, not an
emergency — § Decision framework states both cases honestly.

## Current design, live-verified 2026-07-19

`listings.sreality_id bigint PRIMARY KEY`. Real sreality.cz ids for `source='sreality'`;
for the other 8 portals, a value from `synthetic_listing_id_seq` (migration 097), a
sequence counting down from `-1` (now at **-381,167**), shared across all portals —
the number identifies nothing but "not sreality."

| | rows |
|---|---|
| total listings | 555,784 |
| synthetic (negative) | 380,954 — **68.5%** |
| sreality (real ids, range 37,708 .. 4,294,963,276) | 174,795 |
| largest non-sreality: idnes 162k, bazos 71k, ceskereality 60k, realitymix 57k | |

Verified live facts the plan depends on:

- **FK graph: 19 FK columns across 15 tables** reference `listings(sreality_id)`
  (v1 said "17 tables" — miscount at authoring time, not drift): building_unit_extractions,
  dirty_broker_listings, images, listing_condition_scores, listing_floor_plan_matches (a+b),
  listing_image_comparisons (a+b), listing_marker_extractions, listing_site_plan_matches (a+b),
  listing_snapshots, listing_summaries, listing_videos, listing_visual_matches (a+b),
  manual_rental_estimates, properties.repr_listing_id, property_notes.origin_listing_id.
  Migrations 300–310 added **zero** new FKs to listings — the new CLIP/pHash tooling
  (308–310) is correctly keyed on `images.id`, an existing clean surrogate (positive
  precedent: even `phash_pair_notes` reimplements pair-canonicalization correctly on it).
- **No constraint ties sign to source.** listings carries 6 CHECKs + 3 other FKs + the
  PK; none mention `sreality_id`'s sign. Live check: **0 rows** violate
  `(source='sreality') ⇔ (sreality_id > 0)` across all 555k rows — so the invariant is
  real, just unenforced (and immediately enforceable, § Phase 0).
- **The true natural key exists — but v2 UNDERSTATED its incompleteness** (corrected by
  the #820 work above; original v2 text kept for the record): `(source,
  source_id_native)` with a full unique index (migration 091). v2 saw **35 sreality
  rows with `source_id_native IS NULL`** and called it a static backfill of "an
  old-code INSERT path". It was not static — it was an **active regression**: the
  cadence-split sreality drain (`write_detail_batch`) never stamped `source_id_native`,
  so the NULL set *grew* (35 → 421) and would have grown unbounded. So the natural key
  the whole plan leans on was neither complete nor self-maintaining until #820 stamped
  it inline on every write path + enforced NOT NULL. It is now complete, unique, and
  enforced; `source_id_native` is write-once and never re-mapped, so it is now stable.
- **`first_seen_at` has 0 NULLs** → `(first_seen_at, sreality_id)` is a total order,
  usable for chronological id backfill. But there is **no standalone index on
  `first_seen_at`** (only `(source, first_seen_at)`), so an ordered backfill needs a
  temp mapping table or temp index (§ R1).
- **~146 application files** reference `sreality_id` (frontend/src 50, scripts 35,
  toolkit 24, scraper 19, api 16, chrome-extension 2). v1 said ~90.
- Postgres 17.6; `properties.id` is `bigserial` (14 FK columns / 12 tables, all clean
  surrogate usage — confirmed as the correct in-house template).

## What v1 got wrong (assumption-validation report)

The operator asked that assumptions be validated and patchwork reported — including in
existing documents. v1's flaws, so nobody inherits them:

1. **Census method systematically blind.** v1 counted only `information_schema` FKs.
   **~17 more tables hold listing ids as bare FK-less bigints** (§ Census) — including
   the merge/unmerge replay ledger and both golden-set stores. Any plan that walks the
   FK graph silently strands all of them.
2. **Five unexecutable runbook steps** (all attack-agent-verified against PG 17.6):
   - "ADD COLUMN with sequence DEFAULT is instant/metadata-only" — **false**:
     `nextval()` is VOLATILE, which disqualifies the PG11 fast-default path and forces
     a **full table rewrite under ACCESS EXCLUSIVE** — the exact stall v1 promised to
     avoid, contradicting its own correct warning four lines earlier.
   - "Promote `id` to PK in Phase 1" — impossible: only one PK per table, and the old
     PK can't be dropped while 19 FKs depend on it (only `CASCADE` would, silently
     destroying all child FKs). `id` must live as UNIQUE NOT NULL until children repoint.
   - The `GENERATED ALWAYS AS IDENTITY` conversion path was omitted entirely — done
     naively it creates a fresh owned sequence starting at 1, colliding with backfilled
     ids and crashing the always-on worker on its next insert.
   - "`ADD CONSTRAINT … NOT VALID` never blocks writes" — over-generalized: only
     `VALIDATE` is non-blocking (SHARE UPDATE EXCLUSIVE); the `ADD` itself takes SHARE
     ROW EXCLUSIVE on **both child and parent** — brief, but a lock-queue pileup risk
     on a hot table without `lock_timeout` + retry.
   - **Target design #2 (nullable `sreality_id` + CHECK at Phase 1) fails on all 378k
     existing rows** and would require a destructive 378k-row NULLing pass that breaks
     every frozen artifact below. v2 deletes this step permanently.
3. **Misleading evidence.** Migration 184 was cited as a "documented production
   incident" — it is an index-tuning migration, and the proposed surrogate wouldn't
   even eliminate the index it added. Dropped from the case.
4. **Wrong reasoning on the rejected alternative.** Positive-range synthetic ids would
   *not* fix ordering bugs #1/#2/#4 (explicit trust-rank does; the id scheme is
   irrelevant) — the honest rejection is dominance: same 19-FK re-key cost as the
   surrogate, strictly less benefit.
5. **Bug-detail corrections**: the `min(sreality_id)` "reader workaround" is actually a
   ledger-based resolver (`property_merge_events`) using `min()` only as intra-side
   tiebreak; PhashAudit's `?? 0` sentinel has no `>0` gate protecting it (v1's rationale
   applied only to EstimationDetail); frontend raw-id leak count is ~11 files, not "~6
   more, mostly admin" — Browse's ID column, ComparableModal, and RunPanel are ordinary
   non-admin surfaces; ClipAudit existed at v1 authoring time and was missed.

## Confirmed bugs (all re-verified live on HEAD `b0084b9`, none fixed yet)

**Correctness (Phase 0 closes all of these):**

1. **`repr` CTE missing `src_rank`** — `scripts/recompute_property_stats.py:228-229`
   orders `is_active DESC, last_seen_at DESC NULLS LAST, sreality_id DESC` while its
   ~11 sibling picks in the same file lead with `src_rank`. This selector sets
   `repr_listing_id` **and, through it, the property's category, price, disposition,
   subtype, condition levels — plus (via `properties_public`'s unguarded
   `LEFT JOIN listings ON sreality_id = repr_listing_id`) the broker contact, energy
   rating, floor, description, and admin region Browse shows** (bigger blast radius
   than v1 stated). `api/property_dedup.py:281-293` documents the already-reported
   collision this caused.
2. **Opposite-sign tiebreak in condition scoring** — `toolkit/condition_scoring.py:324`
   ends `… l.sreality_id` (ASC), the only selector in the codebase that reverses the
   direction; all five others use DESC. Dormant (scoring paused) but will fire
   inconsistently on resume. New sibling found by the audit: **migration 300's
   `dedup_label_events` view picks each label's media handle via
   `ORDER BY l.sreality_id LIMIT 1` (ASC)** — an id-sign accident that gets **frozen
   into golden sets**, i.e. benchmark identity chosen by sign.
3. **Misleading Browse "ID" sort** — `ListingTable.tsx:25` binds a sortable ID column
   to `sreality_id`; semantically empty across 68.5% of rows; round-trips through the
   URL and **persists into saved filter presets** (`PresetSpec.sort`, opaque JSONB —
   existing saved presets must keep parsing after any fix). Code-only fix; v1 wrongly
   deferred it to the migration.
4. **Calibration sampling silently sreality-only** —
   `scripts/backfill_address_point_streets.py:184-187` (`ORDER BY sreality_id DESC
   LIMIT n` across an 8-portal source list). Note: a clean surrogate would *not* fix
   this (it would bias toward newest instead); the fix is stratified/random sampling,
   independent of any id scheme.
5. **Bifurcated estimation join** — `api/portal_lookup.py:88-89` special-cases sreality
   (`input_sreality_id`) vs everyone else (fragile `input_url` string equality), even
   though the writer already stamps `input_sreality_id` source-agnostically for any
   scraped listing (`api/estimation_runs.py:1191-1192`). Related but distinct:
   `property_estimates_public` (mig 173) has **no** URL arm at all, silently
   under-counting `with_estimates`. The URL arm's only real value is retro-matching
   estimations made before a listing was scraped — keep it as an explicit demoted
   fallback (`input_sreality_id IS NULL AND …`), don't blanket-delete.
6. **R2 image keys** — `image_key()` embeds the id, and the images route regex accepts
   a leading `-`. Confirmed benign: `storage_path` is stored at upload, never
   recomputed; all readers (frontend, toolkit vision) read the stored column. Only the
   future-write argument ever needs swapping; legacy keys stay valid forever.
7. **Raw-id operator leaks, ~11 files** — worst: `Notifications.tsx:232` and
   `Watchdog.tsx:495` render `id -284913` as a **place label**; `ListingOverview.tsx:115`
   gates on `> 0` instead of `source === 'sreality'`; two independently-invented `?? 0`
   sentinels; `CollectionDetail.tsx:488` puts the raw id in an aria-label; plus Browse
   ID column, ComparableModal, RunPanel, DedupAuditHistory, ClipAudit, PhashAudit
   badge, Health. Good in-house pattern to copy: `BrokerDetail.tsx:397` /
   `Pipeline.tsx:659` fall back to `'—'`/price, never the id.

**Trust-order fragmentation (root cause behind #1/#2):** the per-portal trust policy
exists in at least four inconsistent forms — the full `src_rank` CASE
(recompute, 9 portals + ELSE), a binary `(source='sreality') DESC` variant
(`best_street`), no policy at all with accidental ASC (condition scoring, mig-300
view), and no policy with accidental DESC (`repr`, `property_identity` split — those
two at least deliberately agree with each other). "Prefer sreality" is today an
*accident of id sign* in half the selectors. Whatever else is decided, this becomes
**one** shared definition: an IMMUTABLE SQL function `source_trust_rank(text)` +
mirrored Python constant, with a test asserting they agree — and it is worth an
explicit operator decision that the order itself (sreality > bezrealitky > idnes >
mmreality > remax > maxima > ceskereality > realitymix > bazos) is the *intended*
policy rather than an inherited accident.

## The full identity-carrier census (v1's biggest gap)

Class A — **FK-constrained children** (19 columns / 15 tables, listed above). Get the
standard treatment: add `listing_id`, backfill, FK `NOT VALID` → `VALIDATE`.

Class B — **FK-less live-read carriers** (bare bigints; invisible to FK walks; all have
live readers that would break if legacy ids ever vanished — they get the same add +
backfill + reader-cutover treatment as Class A, minus the FK where append-only
semantics argue against one):

- `property_merge_events.listing_id` (mig 100) — **unmerge/split replays it via
  `UPDATE listings … WHERE sreality_id = listing_id`** (`property_identity.py:363`);
  v1 marked merge machinery "clean" — true only for the property side.
- `dedup_pair_audit.left/right_sreality_id` (mig 227) — Decision-history + the entire
  new pHash-audit read chain resolves pairs → images through it (`property_dedup.py:887-905, 1108`).
- `notification_dispatches.sreality_id` (mig 057, NOT NULL, `UNIQUE(subscription_id,
  sreality_id)` is the once-ever dedup guard; outbox + in-app reads join on it).
  Migration 057's own comment already promised this rename.
- `estimation_runs.input_sreality_id` (the one v1 did cover) + frozen JSONB payloads
  (`comparables_used/excluded`, `input_spec.exclude_ids`, trace) — the JSONB stays
  frozen-by-design (rule 8), fine as long as legacy ids remain resolvable.
- `building_runs.input_sreality_id` (035), `estimation_cohort_entries.sreality_id`
  (053), `listing_description_enrichments.sreality_id` (124).

Class C — **frozen benchmark stores** (append-only by contract — "a precision number
never shifts after publication"): `dedup_golden_pairs` (223), `dedup_golden_sets`
(300), `dedup_vision_bakeoff_results` (303), `dedup_model_compare_review` (304). Their
replay tooling fetches media via `images.sreality_id`. **Decision: leave frozen rows on
legacy handles forever** (safe because legacy values are never NULLed and keep a
lookup index); new freezes write `listing_id` alongside.

Class D — **ephemeral queues** (no history; re-key by writer swap alone, rows cycle
out naturally): `listing_freshness_checks` (006), `listing_fetch_failures` (003),
`condition_score_batch_requests` (098), `dedup_batches/_requests` (197),
`listing_description_enrichment_batch_requests` (305). **Confirmed safe as-is:**
`listing_detail_queue` — keyed `(source, native_id)` since mig 108; its `sreality_id`
column is vestigial and unread on the drain path.

Class E — **recompute-on-refresh read models** (no frozen data; definitions swap at
cutover): `browse_list` (keyset already on `property_id` — `keyset.ts:30-34` even
anticipates this refactor), health matviews (136/216/176 join everything on
`sreality_id`), `listings_public`/`properties_public`/`property_sources_public`,
`property_estimates_public` (173).

Class F — **outside the database** (the compat surface v1 never inventoried):
- **Sent deep links**: `notification_outbox.compose_message` builds
  `/listing/{sreality_id}` into emails/Telegram — effectively permanent URLs, including
  negative ones. Requires a legacy-id resolver route *forever*.
- **SPA routing contract**: `routes.tsx:73` `listing/:sreality_id`; ~14 surfaces via
  `listingPath()`; direct-Supabase reads `.eq('sreality_id', …)` on public views.
- **Chrome extension**: request side already clean (`(source, source_id_native)`), but
  the *response* identity + "Otevřít v aplikaci" deep link is `sreality_id` — under
  v1's plan the link would silently vanish for exactly the portals the extension
  exists for.
- **Do-NOT-rename island**: `portals.ts` `srealityListingUrl()` needs sreality's real
  native id; a mechanical rename to the new surrogate would emit 404 sreality.cz
  links. Bind it to `source_id_native` explicitly.
- ClickUp payloads (external, carry sreality URLs/ids — unaffected), R2 keys
  (§ bug 6 — unaffected), saved presets (`sort: 'sreality_id'` must stay parseable).

## Target design v2

1. **`listings.id bigint`, UNIQUE NOT NULL, sequence-backed, converted to
   `GENERATED ALWAYS AS IDENTITY` at the end of cutover; becomes PK only in R4** once
   children have repointed. Pure meaningless surrogate; backfilled in
   `(first_seen_at, sreality_id)` order so ascending id ≈ market chronology (legacy
   epoch 1..~600k, live epoch from 10,000,000 — the gap is deliberate and harmless).
2. **`sreality_id` is never dropped, never NULLed for existing rows.** Its documented
   meaning becomes: *sreality's real natural id (source='sreality'), or a frozen
   pre-cutover legacy alias (negative).* New non-sreality rows post-cutover get NULL.
   One CHECK enforces all of it, and is addable **today** (validates clean against all
   555k rows) while remaining valid for the post-cutover world:
   ```sql
   CHECK (CASE WHEN source = 'sreality'
               THEN sreality_id IS NOT NULL AND sreality_id > 0
               ELSE sreality_id IS NULL OR sreality_id < 0 END)
   ```
3. **`(source, source_id_native)` completed as the enforced natural key**: backfill the
   35 NULLs, `SET NOT NULL`, keep the unique index. Ingest's `ON CONFLICT` target
   moves here at cutover (this is what lets new rows stop drawing synthetic ids).
4. **External references ride the natural key, joins ride the surrogate**
   (Stripe-style separation). Canonical share/detail URL becomes
   `/listing/{source}/{source_id_native}` — self-describing, human-legible, stable,
   and **immune to the positive-integer ambiguity** between old sreality ids and new
   surrogate ids (the surrogate never appears in a URL). `/listing/{legacy_id}` stays
   forever as a resolver: positive → sreality native id, negative → frozen legacy alias.
5. **One trust-rank authority** (`source_trust_rank()` SQL + Python mirror), used by
   every representative-sibling selector; the ordering itself operator-ratified.
6. `estimation_runs.input_listing_id` added; the portal-lookup join collapses to it
   with the URL arm demoted to explicit fallback; `property_estimates_public`
   redefined on `COALESCE` of both during transition.
7. Child FK columns arrive already correctly named (`listing_id`, `listing_id_a/b`) —
   v1's "rename later" question dissolves; legacy columns are simply dropped (or kept)
   in R5.
8. `properties.id` → `GENERATED ALWAYS AS IDENTITY`: optional low-priority polish,
   unchanged from v1.

## Migration runbook (corrected; every step additive and worker-safe)

> ⚠️ **R2–R5 below are SUPERSEDED by `listing-identity-r2-pk-swap-runbook.md`** (four
> broken steps, see the banner at the top of this file). Phase 0 and R1 are kept as the
> historical record of what shipped; do not execute R2 onward from here.

**Phase 0 — correctness + enforcement, no re-key (1–2 small PRs, ship now).**
Fix bugs 1–5 + 7 code-only (incl. `dedup_label_events` view redefine; Browse ID column
rebound to `first_seen_at` with legacy preset-sort values still parsed); introduce
`source_trust_rank()` everywhere a representative sibling is picked; one additive
migration: the sign↔source CHECK above (`NOT VALID` → `VALIDATE`), backfill 35
`source_id_native` NULLs, `source_id_native SET NOT NULL`. Zero schema risk, closes
every live defect, and converts the invariant from folklore to constraint. **Valuable
under either decision below.**

**R1 — `listings.id` (one migration + a backfill script).**
`ADD COLUMN id bigint` (no default — instant) → separate `ALTER … SET DEFAULT
nextval('listings_id_seq')` with the sequence started at 10,000,000 (instant; new rows
populate immediately) → build UNLOGGED `row_number() OVER (ORDER BY first_seen_at,
sreality_id)` mapping table → batched UPDATE joins (10–20k rows/batch, vacuum cadence,
temporarily aggressive autovacuum on listings; updates are HOT-eligible since `id` is
unindexed during backfill) → `NOT NULL` via validated-CHECK trick → `CREATE UNIQUE
INDEX CONCURRENTLY` + `ADD CONSTRAINT … UNIQUE USING INDEX`. **PK stays on
`sreality_id`.** Never combine ADD COLUMN with a volatile DEFAULT.

**R2 — children (per-table PRs; Classes A + B).**
Per table: add `listing_id` (+`_a/_b`), batched backfill by join on the legacy column
(**no deadline — legacy values never disappear**), `CREATE INDEX CONCURRENTLY`, then
FK `NOT VALID` under `SET lock_timeout` + retry (the ADD briefly takes SHARE ROW
EXCLUSIVE on child *and* listings) → `VALIDATE` (non-blocking, IO-heavy — schedule off
the drain peaks; images is 8M rows). Pair caches: `listing_id_a/b` canonicalized in
**new-id order**, CHECK/UNIQUE added `NOT VALID`, validated after backfill + a
catch-up sweep for rows written during the window; audit every **positional** a/b
consumer (`clip_dedup` join, FE left/right renders) because mixed sreality/non-sreality
pairs flip sides under the new ordering. Class C: add columns, populate for *new*
rows only. Class D: nothing.

**R3 — dual-write + parity bake.**
Writer census (from the audit, more complete than v1's): scraper upsert +
`write_detail_batch` + `ingest_scraped_listing`, realtime-worker lanes, dedup engine +
`image_similarity` ON CONFLICT writers + `dedup_audit`, merge/unmerge/split writers,
broker resolver, freshness + fetch-failure trackers, image drain, condition-scoring
writers, enrichment writers, both notification producers, `_resolve_input`,
building-runs creator, golden-set freezer. Nightly parity check: per-table counts of
`listing_id IS NULL` (must go to zero) + join-equality spot checks.

**R4 — cutover, one PR per surface.**
Read models (public views, browse_list projection, health matviews) → API routes/
schemas (dual-id responses during transition) → frontend queries + **canonical
natural-key route with permanent legacy resolver** → extension lookup response + link
→ outbox link builder → `image_key()` argument → dedup `ListingKey` → unmerge/split
replay reads `listing_id`. Final flip, in one migration window with `lock_timeout` +
retry: ingest `ON CONFLICT` moves to `(source, source_id_native)`; new non-sreality
rows stop drawing the synthetic sequence (write NULL `sreality_id` — the CHECK from
Phase 0 already permits it); drop the now-replaced legacy child FKs; retain a plain
unique index on `sreality_id`; swap the PK to `id`; convert `id` to `GENERATED ALWAYS
AS IDENTITY` (DROP DEFAULT → ADD IDENTITY → `setval` past `max(id)` — the owned
sequence does *not* inherit the old one's position).

**R5 — optional cleanup, destructive gate (deferrable indefinitely).**
Drop legacy child columns; drop `synthetic_listing_id_seq`. **Existing `sreality_id`
values are never touched** — frozen artifacts, benchmark stores, and every URL ever
sent keep resolving, permanently.

**Testing:** ~70 test files reference `sreality_id` — budget a mechanical sweep per
surface PR. The sign CHECK and FK repoints specifically need live-schema tests
(`_FakeConn` cannot catch CHECK/UNIQUE/FK violations); the SQL-correctness CI gate
(PREPARE against replayed schema) covers query-shape drift.

## Decision framework

**For doing R1–R4** (my recommendation, as a deliberate track, not an emergency):
- The census *grew* from 15 FK tables to ~32 carrier tables just by looking harder,
  and the two days between v1 and v2 added three more id-carrying tables (308–310 —
  clean only because they could key on `images.id`). Every month of feature velocity
  compounds new couplings onto the smart key; the cost curve only rises.
- Public-release feasibility is an active roadmap question. Today the perimeter is
  private; the moment any API/URL surface goes external, negative ids and the
  ambiguous id space become a frozen *public* contract. This is cheapest to fix while
  single-operator.
- The corrected plan has **no destructive step, no breakage window, and no deadline
  pressure** — legacy values are permanent, so every phase can pause indefinitely.
- Post-refactor, ascending `id` is a true cross-portal chronology, keyset-friendly;
  URLs become self-describing; the estimation join collapses; the sign folklore dies.

**Against (the honest steelman — the audit's independent second opinion):** after
Phase 0 + the CHECK, **zero live correctness defects remain**; the residual is a
naming/maintainability tax on a single-operator, single-writer system, and the true
cost is 19 FK columns + ~13 loose carriers + ~146 files + a multi-week PR train. If
the platform were certain to stay private and single-operator, deferring R1–R4
indefinitely would be defensible.

**Either way, Phase 0 ships.** It is small, closes an already-reported data bug, and
is a prerequisite-quality improvement for both futures.

## Adjacent findings outside this refactor's scope (own small track)

The properties-side audit (the operator asked about both numbering systems) confirmed
`properties.id` is used cleanly everywhere, but found real identity-lifecycle gaps:

1. **No `merged_away` guard on any property-anchored write endpoint** (pipeline cards,
   collections, notes, tags): a stale `property_id` — e.g. cached by the Chrome
   extension, or from the 5-min-stale browse_list — writes onto the retired row and
   silently never appears on the survivor. Fix: one shared resolve-to-survivor helper
   (follow `merged_into`) at API write entry. HIGH.
2. **`properties.asset_id` + `asset_membership_events` are missing from
   `OPERATOR_STATE_TABLES` and have no merge reconciler** — an operator's same-building
   assertion is silently lost when a member property merges. The registry test
   compares against a hardcoded set, so it can't catch such omissions — make it a
   live-schema diff. MEDIUM.
3. **`/listing?property={id}` dead-ends for merged-away properties** (reads
   `properties_public`, which filters `status='active'`) instead of following
   `merged_into`. MEDIUM.
4. `properties_public`'s repr join has no `l.property_id = p.id` guard (defense in
   depth once #1 above and bug #1 land). LOW.

## Incidental: stale docs

CLAUDE.md still says "seven portals"; nine are live (realitymix, maxima missing).
`property_identity.py`'s docstring says "~9 FK child tables" (it's 19 columns/15
tables). Both are one-line fixes for a hygiene PR.

## Open questions for operator sign-off

1. **Ship Phase 0 now** as its own PR(s)? (Recommended regardless of anything else.)
2. **Ratify the trust order** (sreality > bezrealitky > idnes > mmreality > remax >
   maxima > ceskereality > realitymix > bazos) as deliberate policy — or change it —
   before it's centralized into `source_trust_rank()`.
3. **Commit to R1–R4 as a roadmap track?** If yes, sequencing vs. the public-release
   exit-gate work is the operator's call; the phases have no internal deadline.
4. **Canonical URL scheme**: natural-key `/listing/{source}/{native_id}` with a
   permanent legacy resolver (recommended), or surrogate `/l/{id}` with the same
   resolver?
5. Properties-side track (merged-away guard, asset reconciler, registry live-diff
   test): green-light as an independent small track?
6. `properties.id` → `GENERATED ALWAYS AS IDENTITY` polish: yes/no (low priority).
