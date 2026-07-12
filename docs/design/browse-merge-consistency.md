# Merge → Browse read-model consistency

> **Status: BUILT** (branch `fix/browse-merge-consistency`). Root-caused from a
> live bug report (2026-07-12): manual merge in Browse appeared to silently fail,
> then "fixed itself" after ~2 minutes. Layers 1–2 are implemented; three
> deliberate deviations from the original proposal are marked **[as-built]** in
> place, and Layer 3 was intentionally dropped (see its section for why). The
> diagnosis sections (Root cause / Audit / Findings) are unchanged from the
> investigation.

## Symptom, as reported

Operator selects 4 listings in Browse merge-mode, clicks **Merge**. The
merge-mode toolbar closes (the app's only success signal). Listings still show
as separate cards after a manual reload. A second merge attempt on what look
like the same still-separate cards does **nothing visible** at all. Reloading
again ~2 minutes later shows the listings correctly merged into one property.

## Root cause

Two subsystems, each individually correct, fell out of sync when the newer one
was layered on top of the older one without updating it:

- **Write side (original multi-portal-dedup design, rule #15/#20):**
  `toolkit/property_identity.py`'s `merge_properties` re-points every child
  listing onto the survivor and calls `recompute_one`/`recompute_mf_one`
  **inline, in the same transaction** (lines 172-173). `docs/architecture.md`
  states the intent explicitly (rule #20, line 905): *"Inline merge/unmerge
  still call `recompute_one` directly (they keep the survivor current
  **without waiting for the cron**)."* This part works exactly as designed —
  `properties` (and the live `properties_public` view) is 100% correct the
  instant the API call returns.
- **Read side (added 2026-07-07/08, PR #711/#714, migrations 276-278):**
  Browse's cards, table, header count, and Stats **all read `browse_list`**```
  frontend/src/lib/queries.ts:592,700,753,826 — every one of them `.from('browse_list')`
  ```
  an UNLOGGED table that is a **full, wholesale snapshot** of
  `browse_projection` (a live view over `properties`), rebuilt **blue-green
  every 5 minutes by `pg_cron`** (`migrations/277_browse_read_model_refresh.sql`,
  job `browse-list-rebuild`, `*/5 * * * *`). Nothing about this rebuild is
  event-driven — it is a pure timer. Nothing in the merge write path signals
  it. The map (`properties_map_mv`) has the identical structure on a 30-minute
  timer.

So: the "without waiting for the cron" guarantee the merge code was written to
provide is **silently negated** by a read layer that didn't exist when that
sentence was written and was never updated to honor it. This is not a bug in
either subsystem in isolation — it's an integration gap at the seam between
them, exactly the kind of thing that "evolves in layers" without anyone
noticing because each layer's own tests pass.

`docs/design/browse-read-model.md` (the design doc for the read model itself)
explicitly discusses and *accepts* this staleness — but only for **organic
scraped data**: *"Acceptable for a browse workload (asking prices don't move
by the second)"* (line 130-132). That reasoning is sound for price ticks and
new listings arriving in the background. It does not hold for an **operator
watching their own screen after clicking a button** — a fundamentally
different consistency requirement (read-your-own-writes for the actor vs.
eventual consistency for background data), and the read-model design doc never
considered operator-triggered identity changes as a distinct case.

### Why the "2 minutes" and the "second attempt did nothing"

- **~2 minutes** is just the expected value of a uniform wait for a 5-minute
  periodic tick (0–5 min, mean 2.5 min) — exactly what was observed.
- **Second attempt did nothing:** the operator re-selected the same
  visually-still-separate cards. Server-side, the retired property's
  `status` already flipped to `'merged_away'` inside the first merge's
  transaction. `merge_property_set` (`api/property_dedup.py:986-1044`) filters
  `WHERE status = 'active'` before merging (line 1007) — with one side already
  retired, `len(active) < 2` → raises `MergeError("fewer than two active
  properties in the selection")` → `HTTPException(409)`
  (`api/routes/dedup.py:274-275`) → the app-wide `MutationCache.onError` in
  `frontend/src/main.tsx:31-39` toasts it. So the second click **did** produce
  a response — a correct 409 rejecting a no-longer-valid selection — but the
  only feedback was an easy-to-miss toast, with no visual explanation on the
  stale card that it had already been merged away. This is a symptom of the
  same root cause, not a separate bug: fix the staleness and the operator
  never gets to reselect an already-merged card in the first place.

## Audit: how far this reaches (confirms scope)

A 4-agent parallel audit of every property-mutating endpoint and every
operator-state mutation type found the staleness gap is **confined to
identity-changing mutations on `properties`** — merge, unmerge, split, and (by
construction, same chokepoint) the Tier-2 auto-merge sweep. It does **not**
extend to collections, tags, notes, or deal-pipeline stage moves:

| Mutation | Chokepoint | Touches `properties`? | In `browse_projection`'s column list? | Affected |
|---|---|---|---|---|
| Merge (candidate / bulk / cluster / property-set) | `merge_properties` | Yes, inline recompute | Yes (most columns) | **Yes** |
| Unmerge | `unmerge_group` | Yes, inline recompute | Yes | **Yes** |
| Split to singletons | `split_property_to_singletons` | Yes, inline recompute | Yes | **Yes** |
| Asset link/unlink | `link_properties`/`unlink_property` (`toolkit/asset_identity.py`) | Yes, `asset_id` only, no recompute | Yes — `p.asset_id` is the last column in `browse_projection` (migration 276 line 86) | **Yes, latent** (not the reported bug — Browse doesn't currently render `asset_id` on cards — but the same gap exists the moment it does; see Rollout) |
| Dismiss (cluster/candidate), decision feedback, archive-reset | candidate-table writes only | No | — | No |
| Collections, tags, notes, pipeline-stage moves, watchdog/collection monitoring toggle | `api/curation.py`, `toolkit/pipeline_identity.py` | Different tables entirely (`collection_properties`, `property_tags`, `property_notes`, `property_pipeline`, `collections`) | **No** — none of these columns exist in `browse_projection`'s SELECT (migration 276 lines 57-86) | **No** — these are read live via dedicated `*_public` views/routes, never via `browse_list`, so they were never subject to the 5-min snapshot lag to begin with |
| Watchdog matching | reads live `properties_public` (migration 276's own comment, line 118: *"detail pages and the watchdog matcher stay on the live `properties_public`"*) | — | — | **No** — ruled out; a watchdog alert cannot fire on a stale merged-away duplicate |

This is a helpful result: the fix does not need to be a generic "any write
must refresh Browse" mechanism — it needs to close one specific, well-bounded
seam (identity-changing writes to `properties`), which happens to already
funnel through exactly three functions in one file.

## Compounding findings (same investigation, adjacent bugs)

Found while tracing the frontend mutation → cache-invalidation path. Each is
real, none is the primary cause, all are cheap to fix in the same pass:

1. **`browse-count` (the header/tab cohort total) is never invalidated by any
   mutation, anywhere in the app.** `queryKey: ['browse-count', filters]`
   (`BrowseExperience.tsx:370`) doesn't appear in any `invalidateQueries` call
   in the codebase.
2. **Merging from the `/dedup` review-queue page doesn't invalidate Browse's
   cache at all** — `Dedup.tsx`'s merge/dismiss/bulk mutations only invalidate
   `dedupKeys.all`, never `['cards','map','table','stats']`. A merge approved
   from the review queue leaves Browse showing stale data even *after* its
   next successful refetch would have shown fresh data, because Browse was
   never told to refetch.
3. **The `['cards','map','table','stats']` invalidation list is hand-typed in
   exactly two places** (`BrowseExperience.tsx:178` and `:194`, both in the
   same file) with no shared constant — the kind of duplication that drifts
   the moment one call site is updated and the other isn't.
4. **Success feedback (`pushToast('ok', …)`) is used at exactly one call site
   in the entire app** (`linkMut`, `BrowseExperience.tsx:193`) out of ~20
   sampled `useMutation` sites. Merge has none — the merge-mode toolbar
   silently closing is the only signal, which is why the operator couldn't
   tell whether the first click had worked.
5. **Optimistic `setQueryData` (the correct pattern for instant own-action
   feedback) is independently reimplemented four times** with no shared hook —
   `Pipeline.tsx`, `PipelineToggle.tsx`, `Watchdog.tsx`, `Settings.tsx` — and
   merge/link use neither this nor any other optimistic pattern (pure
   invalidate + wait for refetch).

None of these five are the reported bug's root cause (staleness would still
occur even with perfect invalidation, because the *server-side* snapshot is
what's stale). They are exactly the class of "app-wide unification" gap the
investigation was asked to surface, and they compound the root cause's user
experience into "felt completely broken" rather than "took a couple of
minutes to refresh."

## Proposed architecture

Three independent layers. Layer 1 is the actual fix (makes the data correct,
fast, everywhere, for every viewer). Layers 2–3 are the UX/consistency
follow-through that makes the fix *felt* immediately, not just eventually
true. Each is independently shippable and reviewable — do not bundle into one
mega-PR (see Rollout).

### Layer 1 — targeted synchronous patch of `browse_list` (the fix)

Industry-standard shape for this exact problem (a CQRS-style read model with a
periodic bulk rebuild, needing read-your-own-writes for a low-volume,
high-value operator action): keep the periodic full rebuild as the
**reconciling backstop** — cheap insurance, self-heals anything this patch
ever misses — and add a **targeted, O(1) patch riding the same transaction**
as the write, for the specific rows the write touched. This is the same
two-speed shape this codebase already uses for `properties` itself (dirty-set
incremental + daily full-sweep backstop, rule #20) — applied one layer further
downstream, at the one seam that doesn't have it yet.

**No migration needed.** `browse_list` and `browse_projection` already exist
with the exact shape required (migration 276); the API's DB connection already
owns/writes `properties`/`listings` directly with no RLS in the way (`api/dependencies.py`'s
`get_db_conn` → `scraper.db.connect` → `SUPABASE_DB_URL`, the same
full-privilege connection `merge_properties` already uses for its `UPDATE
properties` — pre-flight check: confirm this role has implicit owner
privileges on `browse_list`, e.g. `\dp browse_list` in psql; no new `GRANT` is
expected to be required since it's the same role that ran the migration that
created the table). This is pure application-layer SQL added to one file.

**[as-built] The helper lives in its own module, `toolkit/browse_read_model.py`
— not `property_identity.py`.** Both `property_identity` (merge/unmerge/split)
and `asset_identity` (link/unlink) need it; a shared single-purpose module
keeps `asset_identity` from importing the merge module (an unnatural
dependency) and gives future read-model helpers (e.g. a `properties_map_mv`
patch) an obvious home. `sync_browse_list(conn, property_ids)`:

```python
def sync_browse_list(conn: psycopg.Connection, property_ids: Iterable[int]) -> None:
    ids = list(dict.fromkeys(int(p) for p in property_ids))
    if not ids:
        return
    try:
        # Nested transaction == SAVEPOINT (callers are already in a txn), so a
        # failure here unwinds only the patch, never the merge/link it follows.
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM browse_list WHERE property_id = ANY(%s)", (ids,))
            cur.execute(
                "INSERT INTO browse_list "
                "SELECT * FROM browse_projection WHERE property_id = ANY(%s)",
                (ids,),
            )
    except psycopg.Error as exc:
        LOG.warning("browse_list sync failed for %s: %s — self-heals on the next rebuild", ids, exc)
```

**[as-built] Best-effort in a SAVEPOINT, NOT hard-atomic.** The original
proposal ran the patch bare in the caller's transaction so it "commits or rolls
back atomically." That's the wrong trade for a *cache*: `browse_list` is
disposable and self-heals every 5 min, but the merge is durable operator
intent. Under the strict-atomic form, a transient read-model problem (a
projection-column migration mid-rollout, an environment where `browse_list`
isn't provisioned) would abort the merge itself. The cache-aside principle says
the opposite: a cache-update failure must never fail the underlying write. So
the patch runs in a nested transaction (a SAVEPOINT, since the callers are
already inside `with conn.transaction()`), and on any `psycopg.Error` it rolls
back *only itself*, logs a warning, and lets the merge commit — the 5-min
rebuild reconciles. On success the patch still commits in the same top-level
transaction as the merge, so the normal path retains the "no stale window"
guarantee; only the failure path degrades gracefully instead of taking the
merge down with it.

The column contract is safe by construction: both `browse_list` (migration 276)
and each rebuild's `browse_list_next` (migration 277) are built `SELECT * FROM
browse_projection`, so `INSERT INTO browse_list SELECT * FROM browse_projection`
is column-compatible by the same definition — no hand-maintained column list.
(Live verification was not possible from the dev box — no `psql`/DB URL — so a
post-deploy check is in the verification plan; the SAVEPOINT covers the narrow
projection-migration window regardless.)

Called at the three recompute chokepoints — the whole change, so every current
and future caller (including the Tier-2 auto-merge sweep) gets it for free:

- `merge_properties`, after `recompute_mf_one(conn, survivor_id)`:
  `sync_browse_list(conn, [survivor_id, retired_id])`
- `split_property_to_singletons`, after the `recompute_mf_one` loop:
  `sync_browse_list(conn, [property_id, *new_ids])`
- `unmerge_group`, after the per-retired recompute loop:
  `sync_browse_list(conn, [survivor_id, *retired_ids])`

**[as-built] Also wired into `toolkit/asset_identity.py`** (`link_properties` →
the surviving asset's members; `unlink_property` → the cleared property + any
dissolved siblings). `asset_id` is a `browse_projection` column, so this keeps
the read-model contract uniform ("every mutation of a projection column patches
the read model"). **It is latent today** — no Browse surface renders `asset_id`
(it appears only in the link/unlink API *response* type, not on a card), so this
path is correctness-hardening, not a user-visible fix. Flagged explicitly so the
operator can drop the two asset call sites if they prefer strict minimalism; the
recommendation is to keep them, because a uniform invariant is less likely to
rot than "merge syncs, asset-link doesn't, for a reason you must remember."

**Concurrency safety.** The patch is plain DML against the table currently named
`browse_list`. The 5-minute rebuild's blue-green swap (`rebuild_browse_list()`,
`migrations/277_browse_read_model_refresh.sql:67-75`) does `DROP TABLE
browse_list; ALTER TABLE browse_list_next RENAME TO browse_list` under a brief
`ACCESS EXCLUSIVE` lock. A patch DML that arrives mid-swap queues behind that
lock for the ~100 ms window, then resolves against the freshly-renamed table —
never a half-dropped one, and never duplicating work (the fresh rebuild already
read the post-merge state from `browse_projection`). No deadlock risk (single
relation, one lock direction).

**Volume note for the future auto-merge sweep:** each patch is two indexed
statements (PK on `browse_list_pk`), not a scan. Merging 3+ properties into one
survivor re-patches the survivor's row once per retired id in the loop —
idempotent, slightly wasteful, not worth optimizing for operator-scale merges
(2-5 properties). If the Tier-2 sweep's volume ever makes it material, batch one
`sync_browse_list` call after the loop — a micro-optimization deferred until
there's a number to justify it.

**Concurrency safety (verify in review, not expected to be an issue):** the
patch is plain DML against the table currently named `browse_list`. The
5-minute rebuild's blue-green swap (`rebuild_browse_list()`,
`migrations/277_browse_read_model_refresh.sql:67-75`) does `DROP TABLE
browse_list; ALTER TABLE browse_list_next RENAME TO browse_list` under a brief
`ACCESS EXCLUSIVE` lock. A patch DML that arrives mid-swap simply queues
behind that lock for the ~100ms swap window, then resolves against the
freshly-renamed table — never against a half-dropped one, and never
duplicating work, since the fresh rebuild already read the post-merge state
from `browse_projection`. No deadlock risk (single relation, one lock
direction, no cross-waiting).

**Volume note for the future auto-merge sweep:** each patch is two tiny
indexed statements (PK lookup on `browse_list_pk`), not a scan — cheap even at
thousands of merges/hour. If `merge_property_set`/`merge_cluster` merge 3+
properties into one survivor, the survivor's row gets redundantly
DELETE+re-INSERTed once per retired property in the loop — correct (idempotent,
each pass uses the latest state) but slightly wasteful. Not worth optimizing
away for typical operator-scale merges (2-5 properties); if the Tier-2 sweep's
measured volume ever makes this material, batch one `_sync_browse_list` call
at the end of the loop instead of per-pair — a pure micro-optimization,
deferred until there's a number to justify it.

### Layer 2 — one shared invalidation contract (closes findings #1-3)

New file `frontend/src/lib/browseInvalidation.ts`:

```ts
import type { QueryClient } from '@tanstack/react-query';

/** Every Browse read surface that must refresh after a property-identity-
 * changing mutation (merge / unmerge / link). Single source of truth — do not
 * hand-type this list at a new call site; import and call
 * invalidateBrowseQueries instead. */
export const BROWSE_QUERY_KEYS = ['cards', 'map', 'table', 'stats', 'browse-count'] as const;

export function invalidateBrowseQueries(queryClient: QueryClient): void {
  for (const key of BROWSE_QUERY_KEYS) {
    queryClient.invalidateQueries({ queryKey: [key] });
  }
}
```

Replace both hand-rolled loops in `BrowseExperience.tsx` with calls to
`invalidateBrowseQueries(queryClient)` — this alone fixes finding #1
(`browse-count` was simply missing from the list). Add the same call to
`Dedup.tsx`'s merge/bulk-merge mutations (alongside their existing
`dedupKeys.all` invalidation, not instead of it) — fixes finding #2.

**[as-built]** Both done. `BrowseExperience.tsx`'s `mergeMut` and `linkMut` now
call `invalidateBrowseQueries`; `mergeMut` also emits a success toast (`Merged N
listings into one property.`), closing finding #4 for the merge path (the
toolbar closing was the only prior signal). `Dedup.tsx` routes its three merge
mutations (`mergeMut`/`mergeSetMut`/`bulkMut`) through a combined
`invalidateAfterMerge` (dedup keys **+** browse keys); `dismissMut` stays
dedup-only since a dismiss changes no `properties` row. A vitest
(`browseInvalidation.test.ts`) pins the key set (including `browse-count`) so
finding #1 can't silently regress.

### Layer 3 — read-your-own-writes optimistic UI — **[as-built] deliberately NOT implemented**

The original proposal added optimistic `setQueryData` removal of the merged
cards. On reflection during build it was dropped as gold-plating that would add
real fragility for a benefit Layers 1+2 already deliver:

- **The user-visible bug is already fully fixed without it.** Layer 1 makes the
  server (and `browse_list`) correct the instant the merge returns; Layer 2's
  `invalidateBrowseQueries` refetches against that now-correct snapshot, so the
  merged cards drop out on the refetch — sub-second, not the 2 minutes reported.
- **The "second click did nothing" path is structurally gone regardless.**
  `mergeMut.onSuccess` calls `exitMergeMode()`, which clears the selection and
  closes the toolbar; the operator cannot re-click Merge on a stale card without
  re-entering merge-mode and re-selecting, by which point the sub-second refetch
  has already removed it. Optimistic removal isn't needed to prevent the
  double-merge 409.
- **It would introduce coupling and asymmetry.** Optimistic removal hard-codes
  the `InfiniteData<InfiniteListPage<CardRow>>` cache shape into the mutation
  (two surfaces: cards *and* table), and the sibling `linkMut` doesn't do it — so
  merge-mode would carry two different feedback patterns for no principled
  reason. Matching `linkMut` (toast + invalidate + exit) keeps merge-mode
  internally consistent and the cache shape owned solely by `useInfiniteList`.

Net: the shipped merge path is `pushToast('ok', …)` → `invalidateBrowseQueries`
→ `exitMergeMode`, mirroring `linkMut` exactly. If a future measurement shows the
sub-second refetch flash is actually bothersome, optimistic removal can be added
then as a *shared* `useInfiniteList` cache helper (so cards and table stay
symmetric) — not bespoke per-mutation cache surgery. Findings #4 (success
feedback) is closed for merge; #5 (four duplicated optimistic-update
reimplementations) stays an explicit separate-PR follow-up (see non-goals).

## Explicit non-goals (do not scope-creep the fix into these)

- **`properties_map_mv`'s 30-minute lag stays as-is.** It is a true
  `MATERIALIZED VIEW`, not a table — Postgres does not support row-level DML
  against a matview, so the Layer-1 patch technique doesn't apply directly.
  The reported bug is about the card list, not the map; the existing 30-minute
  map lag is a long-accepted trade-off predating this investigation
  (`migrations/277_browse_read_model_refresh.sql:31` comment). If the operator
  later wants the map to reflect merges within seconds too, the clean path is
  converting it to the same UNLOGGED-table-with-patch shape as `browse_list`
  (unifying both under one mechanism) — a materially bigger, separate change;
  do not fold it into this fix.
- **Organic listing staleness (price changes, new listings, delisting) stays
  eventually-consistent at ~5 minutes.** `docs/design/browse-read-model.md`'s
  "asking prices don't move by the second" reasoning is sound for that class
  of write and is not being revisited here. Only operator-initiated
  identity-changing writes get the synchronous patch.
- **Do not unify the four pre-existing bespoke optimistic-`setQueryData`
  implementations** (`Pipeline.tsx`, `PipelineToggle.tsx`, `Watchdog.tsx`,
  `Settings.tsx`) into a shared hook as part of this fix. It's a real,
  independently-identified piece of technical debt (see finding #5) and a
  legitimate follow-up — but it's a pure refactor touching four unrelated
  features, and CLAUDE.md's "one PR = one purpose" rule applies. This fix adds
  *no* optimistic-update site (Layer 3 was dropped), so it neither worsens nor
  is the right occasion to consolidate that debt — track it separately.
- **Collections/tags/notes/pipeline-stage/monitoring mutations need no
  change.** The audit confirmed these were never part of the `browse_list`
  contract and are read live — they were never subject to this staleness
  class to begin with.

## Rollout — **[as-built]** one PR (`fix/browse-merge-consistency`)

Layers 1 and 2 are one coherent purpose ("a merge reaches Browse immediately")
and ship together:

1. **Layer 1 — `toolkit/browse_read_model.py` + five call sites**
   (merge/unmerge/split in `property_identity.py`; link/unlink in
   `asset_identity.py`). No migration.
2. **Layer 2 — `frontend/src/lib/browseInvalidation.ts`** + the two
   `BrowseExperience.tsx` call sites + `Dedup.tsx`'s three merge mutations, plus
   the merge success toast.

Deferred, each its own future PR, only on operator request:

3. Shared `useInfiniteList` optimistic-removal helper (if the sub-second refetch
   flash ever proves bothersome) — consolidating the five call sites including
   the four pre-existing bespoke ones (finding #5).
4. Sub-30-min map freshness for merges: convert `properties_map_mv` to the same
   UNLOGGED-table-with-patch shape as `browse_list`.

## Testing / verification — **[as-built]**

- **Hermetic unit + wiring tests** (the repo's `_FakeConn` convention — the
  spatial/recompute SQL is verified out-of-band, control flow + emitted
  statements here): `tests/test_browse_read_model.py` exercises the
  DELETE→INSERT shape, id dedup/order, the empty no-op, and the
  **must-swallow-DB-error** behaviour, plus a guardrail asserting all five
  mutation functions' source calls `sync_browse_list` (so the wiring can't be
  silently dropped). `tests/test_property_identity.py` adds three assertions that
  merge/split/unmerge emit the patch with the right ids, **after** the inline
  recompute. `frontend/.../browseInvalidation.test.ts` pins the key set. **All
  40 backend + 3 frontend tests green locally; changed FE files typecheck clean**
  (the only `tsc` errors are a pre-existing `@dnd-kit` install gap in the dev
  box's cross-branch `node_modules`, absent in CI).
- **Not added: a live-DB integration test.** The repo has no seeded
  `TEST_DATABASE_URL` Postgres in CI today (the SQL-correctness gate seeds
  empty), and standing up `browse_list` + `browse_projection` + the gate
  function + `properties`/`listings` fixtures for one assertion is
  disproportionate and would be the only such test in the suite. The column
  contract is guaranteed by construction (both objects are `SELECT * FROM
  browse_projection`); the SAVEPOINT makes a schema-window mismatch non-fatal.
  If the seeded-DB gate (open PR #665) lands, fold in a plan-and-row assertion
  then.
- **Post-deploy live check** (couldn't run from the dev box — no `psql`/DB URL):
  after a real Browse merge returns 200, before the next `*/5` tick,
  `psql "$SUPABASE_DB_URL" -c "SELECT property_id FROM browse_list WHERE
  property_id IN (<survivor>, <retired>)"` should show the survivor present and
  the retired id absent.
- **Manual UI verification** (per CLAUDE.md, golden path in a browser): in Browse
  merge-mode select 2+ listings, click Merge, confirm (a) a success toast, (b)
  the merged card(s) disappear **without a manual reload**, (c) the header count
  decrements.

## Data-quality framing

This isn't purely a UX polish item. For up to 5 minutes after every merge —
manual or Tier-2 automatic — Browse actively **contradicts a dedup decision
that was just made**: it shows what looks like two separate active listings
for a property the system just declared to be one. That's a direct, visible
regression in the exact metric the entire dedup program (rules #15-16,
`docs/design/multi-portal-dedup.md`, `clip-visual-embeddings.md`) exists to
improve — perceived duplicate rate. Because the fix lives at the shared
`merge_properties`/`unmerge_group`/`split_property_to_singletons` chokepoint,
it closes this window for **every** merge path, including the much
higher-volume Tier-2 automatic sweep, not just the rare manual click — a
standing data-quality improvement market-wide, beyond the one reported bug.
