# Unified Browse read model

> **Status: DESIGN, NOT YET BUILT (2026-07-07).** Proposed follow-up to the
> P0 hotfix in PR #707 (`fix/browse-read-path`), which restored Browse but left
> the underlying structural debt in place. This doc is for operator review
> before any schema lands. Nothing here is applied. See ROADMAP.md for
> sequencing.

## Context

Browse has **five read surfaces that must agree**: the card list, the table,
the header/tab **count**, the **map** clusters, and the **Stats** aggregates.
Today four of them (cards, table, count, stats) read the live OLTP table
`properties` (448k active rows, rewritten every ~5 min by the detail drain +
property-maintenance, and `last_seen_at` bumped for *every* active row each
scrape cycle) through the `properties_public` view. Only the **map** reads a
purpose-built read model, `properties_map_mv` (migration 254): a compact
projection, physically **clustered by `(category_main, category_type, lat, lng)`**
and refreshed blue-green on the refresh_map_mv cadence (currently every 30 min),
precisely because the live table can't stay
cached (512 MB `shared_buffers` for a 41 GB DB) and a scattered scan is cold-slow.

The map is the surface that has *not* had reliability problems. The list, table,
and count are the ones that keep breaking — most recently the market-wide timeout
fixed in PR #707. That is not a coincidence: **serving an interactive,
low-latency, anon-3s-budget read workload off a high-churn OLTP table forces a
bespoke index per query shape.** The evidence:

- `properties` now carries **31 indexes totaling 928 MB against a 336 MB heap**
  (indexes are **2.75× the heap**). Migrations 247, 251, 253, and the P0c
  composites in 275 are all "add another index because a new (category × sort ×
  filter) shape timed out." This is a treadmill.
- Every new index on `last_seen_at` (like P0c's `properties_cat_last_seen_keyset_idx`)
  pays **write amplification on every scrape cycle**, because `touch_listings`
  bumps `last_seen_at` on every active row — a non-HOT update that maintains each
  such index continuously.
- Keyset pagination is defined over a table that **mutates its own sort key mid-scroll**
  (`last_seen_at`); keyset.ts carries a long comment explaining the correctness
  gymnastics this forces.

The P0 fix (InitPlan the gate, match the keyset nulls placement to the btree, add
the category+recency composites) was the right *emergency* response — it restored
every reported lane to <20 ms — but it added two more indexes to a table that
already has 31, and it does not fix the multi-category lane (byt+dum falls back to
a 393 ms bare-index scan) or the exact-count ceiling (`EXACT_COUNT_MAX = 5000`,
above which the UI shows "~N" because an exact scan of the live table is
unaffordable).

## Goal

**One compact, snapshot-based read model that serves all of cards / table / count
/ map / stats from a single projection, built for reading — so query shapes are
served by cheap indexes on a stable snapshot instead of by an ever-growing pile of
indexes on the churning OLTP table.** Detail pages and every write path keep
reading `properties` / `properties_public` live (unchanged).

Why this is the clean architecture and not more patchwork:

- It **ends the index treadmill on the hot table.** The recency/category/filter
  indexes move onto a matview that is *rebuilt wholesale* each refresh, so they
  carry **zero per-row write amplification** — the exact opposite of P0c's
  live-table composite.
- **Keyset pagination becomes correct-by-construction**: a matview is a *stable
  snapshot* between refreshes, so `last_seen_at` doesn't move under an
  infinite-scroll session. The two-phase-cursor gymnastics get simpler, not
  harder.
- **Exact counts become affordable market-wide** on a compact projection (drop or
  raise `EXACT_COUNT_MAX`), so the UI stops showing "~N" for broad cohorts.
- It puts the **publication-gate predicate and the column contract in ONE place**
  (see the P0a lockstep hazard below), instead of hand-duplicated across
  `properties_public` and `refresh_map_mv.py`.

## The decisive constraint: the map matview can't just be reused

The obvious "unify onto `properties_map_mv`" is **wrong**: the map matview is
filtered `WHERE lat IS NOT NULL AND lng IS NOT NULL`, which excludes **11,583
active properties (2.6%)** that have no coordinates. Those rows must still appear
in the card list and the count. So the list read model needs its own row set (all
active rows), and — separately — the map needs geo clustering that a
recency-sorted list can't share (a table has one physical order).

## Recommended shape: two matviews from one shared projection

```
                         browse_projection            (ONE place: column contract
                    (SQL: columns + gate WHERE)         + `NOT (SELECT gate())` predicate)
                          /              \
             browse_list_mv          properties_map_mv
   (ALL active rows, incl.        (lat/lng NOT NULL rows,
    lat-less; indexed by           clustered by geo — as today)
    category+recency+filters)
        |         |          \                     |
      cards     table     count(exact)            map
```

- **`browse_projection`** — a single SQL definition (a plain view or a set-returning
  function) that produces the Browse column set and applies
  `status='active' AND (NOT (SELECT publication_gate_enabled()) OR published_at IS
  NOT NULL)` **once**. Both matviews `SELECT ... FROM browse_projection`. This is
  the fix for today's **hand-duplicated gate predicate** (it lives in
  `properties_public`'s SQL *and* in `refresh_map_mv.py`'s Python string; PR #707
  had to remember to change both — exactly the kind of by-hand lockstep that rots).
- **`browse_list_mv`** — every active property (including the 11.5k lat-less ones),
  the ~30 columns cards/table/filters need (not the heavy `description` /
  broker-contact text, which the card/table lanes already fetch lazily). Indexed
  for the real Browse shapes: `(category_main, category_type, last_seen_at DESC, id
  DESC)`, `(…, first_seen_at DESC, id DESC)`, the district-id + price covering
  index, the MF-yield index — the *same* shapes as today's live-table indexes, but
  now on a compact churn-free snapshot. Cards, table, and **exact counts** read
  this.
- **`properties_map_mv`** — unchanged in spirit (geo-clustered), but re-pointed to
  `SELECT FROM browse_projection WHERE lat IS NOT NULL AND lng IS NOT NULL` so it
  shares the projection/gate.

Both matviews refresh from the same blue-green job (extend `refresh_map_mv.py` into
a `refresh_browse_mvs.py`, or run two swaps in one job) on the existing refresh
cadence. `properties_public` **stays** for detail pages, the watchdog/collection
matchers, and any write-adjacent read (those need live data, not a ~30-min snapshot).

### Open sub-decisions (for the review)

1. **Default sort.** `last_seen_at DESC` is the current default, but `last_seen_at`
   is bumped market-wide every scrape cycle, so it effectively means "whichever
   portal/category the scraper touched most recently" — not a meaningful "what's
   new" for the user, and it's the churn that made P0c necessary. **Recommend
   switching the default to `first_seen_at DESC`** ("newest listings", stable and
   meaningful). On a snapshot matview the churn argument disappears either way, so
   this is purely a product/UX call — but it also removes the last remaining
   write-amplification motivation. *Operator decision.*
2. **Staleness.** The list would go stale by up to the refresh interval (~30 min
   today) like the map. Acceptable for a
   browse workload (asking prices don't move by the second), and the realtime
   publication gate is orthogonal. If a fresher list is ever wanted, the refresh
   cadence is one number.
3. **One matview with two index sets vs two matviews.** A single `browse_list_mv`
   *could* also serve the map via a geo covering index, avoiding a second object —
   but the map's cold-robustness depends on *physical geo clustering* (the
   refresh_map_mv comment measures 2.4–3.6 s scattered vs 0.12 s clustered), and a
   table has one physical order. So two matviews (one clustered for recency, one for
   geo), one shared projection, is the recommendation. Revisit if `shared_buffers`
   ever grows enough that clustering stops mattering.

## Two read-path follow-ups surfaced by the P0 review

Both are pre-existing (not P0 regressions) and low-urgency, captured here so they
aren't lost:

1. **Keyset deep-pagination is O(depth), not flat.** PostgREST/supabase-js can
   only emit the cursor predicate in OR form (`col.lt.v,and(col.eq.v,id.lt.c)`),
   which the planner applies as a **Filter, not an index range bound** — so page N
   discards the N preceding rows before filling the window. Realistic consecutive
   scroll is fine (each cursor is the previous page's boundary → ~ms; measured page
   2 ~8 ms), but a cursor far from any boundary, or scrolling a giant single-category
   cohort to its tail, degrades and can time out. The unified read model doesn't fix
   this by itself; the real fix is emitting a **row-comparison cursor** `(col, id) <
   (v, c)` that Postgres converts to an index seek — which needs a mechanism
   PostgREST can express (a keyset RPC, or a computed composite sort key). Fold into
   the `browse_list_mv` reader work.
2. **Gate-ON companion index.** When `dedup_publication_gate_enabled` is flipped on,
   the residual filter is `published_at IS NOT NULL` (not in the recency composites),
   so a large unpublished-active backlog would scan deep. A no-op today (~290
   unpublished rows, ~18 ms) and watched by the PR-#706 health panel, but the
   read model should either **bake the gate into the matview WHERE** (rows are
   pre-filtered at refresh, so the reader never pays it — the natural outcome of
   `browse_projection`) or carry a `published_at`-aware index. This removes the
   concern entirely.

## The proper CI guardrail (what P0 deferred)

PR #707 shipped a **deterministic, offline** guardrail
(`tests/test_browse_read_path_guardrail.py`: the gate call must be wrapped in the
effective view definition; keyset nulls-placement is pinned). That catches the two
*specific* regressions but not the *general* one ("a Browse query shape lost its
serving index"). The general guard needs a populated DB, which the
`TEST_DATABASE_URL` SQL-correctness gate (open PR #665) provides but currently seeds
empty.

Proposal: seed a **few-thousand-row representative fixture** (spread across
categories / types / a few obce, with coordinates and without) into that gated
Postgres, `ANALYZE`, then assert **plan shape** for the canonical anon Browse
shapes: (a) no `Seq Scan` / large `Bitmap Heap Scan` on the default + preset +
bbox lanes; (b) no per-row `publication_gate_enabled` in a Filter (must be an
InitPlan); (c) each lane under a buffer budget. This turns "a Browse read path
regressed" into a red CI check instead of a production incident — the real fix for
the class of bug that has recurred across migrations 247/253/275.

## Rollout (independently shippable slices)

1. **`browse_projection` + `browse_list_mv`** (additive migration + refresh job)
   built and refreshed in parallel with the live path — no reader change yet.
   Verify row parity vs `properties_public` (incl. the 11.5k lat-less rows) and
   plan shape.
2. **Re-point the frontend** cards/table/count fetchers from `properties_public`
   to `browse_list_mv` (one change in `queries.ts`; the column contract is
   identical). Ship behind nothing — it's a read swap; roll back by reverting the
   `from()` target.
3. **Re-point `properties_map_mv`** onto `browse_projection` (share the gate).
4. **Retire the now-redundant live-table indexes** (the P0c composites + any
   category/recency index only Browse used) with a forward migration — reclaiming a
   chunk of the 928 MB and the write amplification. Keep only what non-Browse
   surfaces need.
5. **Seeded plan-shape CI guardrail** (depends on PR #665 landing).
6. *(Optional, product)* Switch the default sort to `first_seen_at DESC`.

## Risks / mitigations

- **A matview is stale by up to the refresh interval.** Mitigation: it already is,
  for the map; detail pages stay live; the realtime gate is separate. A brand-new
  listing appears within one refresh (~30 min today), same as the map today.
- **Two matviews double the refresh cost.** Both are compact projections; the map
  rebuild is already ~12 s. Run them in one job on the existing cadence; measure
  before/after.
- **Row-parity drift** between `browse_list_mv` and `properties_public`.
  Mitigation: both derive from the single `browse_projection`, and slice 1 ships a
  parity assertion (counts + a sampled row diff) before any reader moves.
- **Keyset over a refreshing snapshot**: a refresh mid-scroll swaps the snapshot,
  which is *strictly better* than today (the live table mutates `last_seen_at`
  mid-scroll); the `(sort, id)` cursor remains valid across a refresh because
  `first_seen_at` / `id` are immutable. If the default sort stays `last_seen_at`, a
  refresh can re-order the tail — another reason to prefer `first_seen_at`.

## Non-goals

- True spatial (GiST) serving of arbitrary-bbox *card lists* (as opposed to the
  map's clustered read) — the recency composites cover the reported large-bbox
  case; a tiny sparse bbox with a recency sort is a separate, rare shape.
- Changing the write model, `listings`/`properties` split, or the dedup/publication
  pipelines. This is purely a read-path concern.
