# Per-portal listing order fidelity

> **Status: APPROVED (2026-08-04), implementation in progress.** Written after a 6-agent research
> pass across the schema, the sreality client/parser, the shared portal framework, the other 8
> portals' parsers, the frontend/Browse read path, and prior art (ROADMAP, `docs/design/`, git
> history), then a follow-up verification pass that reshaped Phase 4. Every claim below is
> file:line-verified by that investigation, not inferred. Operator approved Phases 1–4
> (2026-08-04); Phase 5 still open. See "Open decisions" at the bottom for exact scope.

## Problem statement

The operator's ask: filter Browse to ACTIVE listings from one portal, sort "newest first," and
get an exact mirror of that portal's own newest-first listing order. Today that doesn't happen.
The working hypothesis going in was that our index-walk → detail-drain pipeline processes
listings out of discovery order (FIFO/LIFO confusion). **That hypothesis is correct, but it's
only one of four independent, stacked causes** — fixing the pipeline alone would not deliver the
ask. The other three are: the portal doesn't always have a knowable order to mirror in the first
place; the timestamp Browse sorts on is stamped at the wrong point in the pipeline entirely; and
Browse's portal filter, at the grain it operates on today, doesn't even guarantee the *displayed
fields* come from the filtered portal's own listing, independent of sort order.

## Root cause 1 — the portal doesn't always have a mirror-able order

Only **1 of 9 portals** (bezrealitky, `order: TIMEORDER_DESC` hard-coded in its GraphQL query,
`scraper/bezrealitky_client.py:38`) explicitly requests and gets a verified newest-first index.
Everyone else is worse than "unordered":

| Portal | Native order | Native date field | Captured? |
|---|---|---|---|
| bezrealitky | **Requested newest-first** (`TIMEORDER_DESC`) | `timeActivated` | Wired, but anon API returns NULL today (`scraper/bezrealitky_parser.py:212-215`) |
| sreality | **API ignores every sort param** (live-HAR-confirmed, `docs/design/realtime-scrapers.md:37-41`); page 1 is promotion-polluted | `edited` (day-granular, ~40% populated, last-*edit* not first-publish) | Yes → `published_at` |
| ceskereality | Default index order is **explicitly NOT newest-first** (`scraper/ceskereality_main.py:83-87`); a `/nejnovejsi/` sort-slug exists but is only used by the probe, never the main ingestion walk | "Datum vložení" (real publish date) | Yes → `published_at` |
| bazos | *Assumed* newest-first, never enforced by a sort param | last-*bump*/TOP-renewal date, not first-publish | Yes → `published_at` |
| idnes, realitymix, remax | *Assumed* newest-first, never enforced by a sort param | **None exists** — live-fetched and grepped for meta/JSON-LD/date keywords, genuinely absent at the source | No (nothing to capture) |
| maxima | No claim made (catalogue small enough it doesn't matter) | **None exists** | No |
| mmreality | No claim, no delta-probe mechanism at all (`supports_complete_walk=false`) | **None exists** | No |

Two things fall out of this table that reframe the whole problem:

1. **"Exact mirror" has a hard ceiling per portal.** For sreality, the public API structurally
   cannot express newest-first order and page 1 is promotion-polluted — there is no portal order
   to mirror even in principle, short of depending on an undocumented internal API (see
   [Sreality: the hard case](#sreality-the-hard-case)). For idnes/realitymix/remax/maxima/mmreality,
   there is no portal-declared date signal at all, live-verified by fetching real detail pages —
   this is not a scraping gap, the data doesn't exist at the source.
2. **`bazos`/`idnes`/`realitymix`/`remax`'s "default order is newest-first" is an unverified,
   unenforced assumption**, baked into the probe/delta-detection logic ("a page-capped walk *is*
   the delta probe" — e.g. `scraper/idnes_main.py:120-122`). If any of these sites ever changes
   its default sort (a routine site redesign), the probe silently under-discovers new listings —
   a **completeness** risk (rule #3), not just a display-order one. This is a pre-existing
   fragility, independent of this doc's fix, worth hardening (see [Phase 5](#phase-5-close-the-unverified-sort-assumption)).

## Root cause 2 — the timestamp Browse sorts on is stamped at the wrong point

`listings.first_seen_at` — what "Newest first" actually orders by — is a plain
`timestamptz not null default now()` column (`migrations/001_initial.sql:20`). Per rule #19, the
index-walk **never writes `listings`**; it only enqueues into `listing_detail_queue`
(`scraper/db.py:2413-2470`). So `first_seen_at` is stamped at **detail-drain write time**, and
between "the index-walk saw this id" and "the write transaction ran," five independent
reorderings happen, each confirmed by file:line:

1. **Priority bucketing at enqueue.** New listings get the *lowest* claim priority — behind
   price-changed and failure-retry rows (`QUEUE_PRIORITY_NEW=0 < CHANGED=1 < FAILURE=2`,
   `scraper/db.py:2105-2107`; claim query `ORDER BY priority DESC, enqueued_at`,
   `scraper/db.py:2473-2503`).
2. **Transaction-granular `enqueued_at`.** `enqueue_detail` batches up to 1000 rows per INSERT
   (`scraper/db.py:2109`) on an autocommit connection where Postgres `now()` is fixed for the
   whole statement — so up to 1000 listings can share one identical `enqueued_at` with no
   secondary tiebreak (`scraper/db.py:339-359, 2441-2469`).
3. **Completion-order fetch.** The detail-drain fetches a claimed batch on a 4-8 worker
   `ThreadPoolExecutor` and buffers results via `as_completed()` — write order is HTTP-latency
   order, not claim order (`scraper/portal_runner.py:553-565`).
4. **Batch-constant `now()` at write.** `write_detail_batch` commits up to 100 rows in one
   transaction via one INSERT; `first_seen_at` falls through to the column default, so **every
   new listing in one flushed batch gets a literally identical `first_seen_at`**
   (`scraper/db.py:2137-2177`, `scraper/portal_runner.py:37`).
5. **Cross-process interleaving.** For 7/9 portals, the always-on `realtime_worker` drain lane
   (~every 30s) and the GitHub-Actions-scheduled drain both claim from the same queue
   concurrently. `FOR UPDATE SKIP LOCKED` prevents double-claiming a row but gives **zero
   guarantee about relative commit order** between the two processes
   (`scraper/realtime_worker.py:12-18`).

The one signal that *would* have preserved true discovery order — `listing_detail_queue.enqueued_at`
— is never copied anywhere and is **deleted the moment the row's detail-drain succeeds**
(`migrations/105_detail_queue_and_split_health.sql:5-9,24-32`). It exists only transiently.

For sreality specifically, a sixth reordering stacks on top: rule #21's sanctioned district-split
walks large categories as **77 fully sequential, independently-completed per-district passes**
(`scraper/main.py:1040-1094`) — a listing posted on the portal seconds ago in district 50 is
discovered only after districts 1–49's *entire* historical inventory has been walked in this run
— plus the 20 category pairs themselves are walked in an **hourly-rotating order**
(`scraper/main.py:136-149`), so even the cross-category order isn't fixed run-to-run.

**One existing doc claim is simply false and should be corrected regardless of what we build:**
`docs/design/realtime-scrapers.md:15-16` self-describes the pipeline as "already an idempotent,
resumable, **newest-first** drain over a Postgres queue." It is not, by the five mechanisms
above. This is a validated-assumption failure worth fixing in the same PR as anything else here.

## Root cause 3 — Browse's portal filter doesn't isolate the filtered portal's own data

Independent of sort order entirely: Browse's "Newest first" and its "Portal" filter both operate
on `browse_list` (migration 276), a **property-grain** read model — one row per real-world
property (rule #15), not per listing.

- **The sort key is cross-portal even at property grain.** `properties.first_seen_at` (what
  `browse_list` sorts on) is `min(children.first_seen_at)` across *every* portal a property has
  ever been seen on (`child_agg` CTE, `scripts/recompute_property_stats.py:126-137`) — not the
  filtered portal's own first-seen time.
- **The displayed fields aren't reliably from the filtered portal either.** The "Portal" filter
  constrains `properties.source`, which is the trust-ranked *representative* listing's source
  (`repr` CTE: active-first, then `source_trust_rank()`, `scripts/recompute_property_stats.py:207-227`)
  — so `price_czk`/`disposition`/`category` genuinely come from the filtered portal's row. But
  `area_m2`, `district`, `locality`, `street`, `building_type`, `condition`, `ownership`,
  `energy_rating`, etc. are picked by **separate CTEs that rank trust *before* activity**
  (`golden`/`best_geo`/`best_street`, `scripts/recompute_property_stats.py:149-241`). **Confirmed
  failure scenario:** an active bazos listing with an inactive sreality sibling — `repr` correctly
  picks bazos (only active child), but `golden` picks sreality's fields regardless, because
  sreality outranks bazos on trust and the `golden` CTE doesn't check activity first. A card shown
  under "portal = bazos" can display area/location/condition lifted from a *delisted* sreality
  listing. This is a real correctness bug against "mirror what I'd see on that portal's page,"
  and it exists whether or not sort order is ever fixed.

This is the expected, working-as-designed behavior for `browse_list`'s actual purpose — the
deduped, trust-blended, market-wide view rule #15 exists for. It is simply the *wrong grain* for
"show me portal X's own page," and no amount of CASE-logic patching onto the golden-record CTEs
would fix that cleanly — trust-blending and single-source-fidelity are opposite goals by design.

## Root cause 4 — `listings.published_at` exists but isn't usable yet

A portal-declared timestamp column already exists (`listings.published_at`, migration 266,
captured for bazos/ceskereality/bezrealitky/sreality per the table above). It was built purely
for internal publish→ingest SLO instrumentation: it has no index, is explicitly excluded from the
content hash (correctly, per rule #2 — good, no snapshot noise), is **not projected into
`browse_projection`/`browse_list`**, and is **not in the frontend's `SORTABLE_FIELDS` list**
(`frontend/src/lib/queries.ts:137-143`). It also has real reliability caveats documented in its
own migration comment (day-granular; last-bump not first-publish for bazos; weak ~40%-populated
fallback for sreality; NULL for 5/9 portals) — it needs to be a *preferred* signal where available,
not the *only* signal.

## No prior art

Grepped ROADMAP.md, every `roadmap/*.md`, `docs/architecture.md`, all of `docs/design/`, and
`git log --all --grep` for order/chronology/newest/sort/discovery/FIFO/LIFO/mirror. This exact
framing has never been raised. Adjacent, non-overlapping prior work: the 2026-07-07 decision to
default Browse sort to `first_seen_at DESC` (`docs/design/browse-read-model.md:122-129`, "purely
a product/UX call" — never examined whether `first_seen_at` preserves discovery order); and a
still-open, never-implemented roadmap item (`roadmap/next.md:60-63`) noting that a HAR spike
proved sreality's Next.js BFF has an internal `sort:'-date'` capability, scoped only for
scrape-probe efficiency, never for ordering. No `discovery_rank`/`portal_position`-equivalent
field exists anywhere (zero hits across `scraper/`, `toolkit/`, `api/`, `migrations/`, `frontend/`).

## Proposed architecture

Four causes, four independent fixes. All but Phase 4 are shared-framework, portal-agnostic
changes (rule #21-compliant — no per-portal branching in shared code).

### Phase 1 — true discovery-order capture (schema + shared framework)

Add a **dedicated monotonic sequence**, assigned once at true enqueue time, immune to every
reordering in Root cause 2 because it's fixed *before* any of that batching/concurrency happens:

- New migration: `listing_detail_queue.discovery_seq bigint not null default nextval(...)` on a
  new dedicated sequence. Unlike `now()`, `nextval()` is called once *per row* even inside one
  multi-row INSERT, so it gives a true relative order even for the 1000-row enqueue chunks.
- New migration: `listings.discovery_seq bigint` (nullable; NULL for pre-existing rows). Carried
  from the claimed queue row through `claim_detail_batch` → `write_detail_batch`, written **only
  on first insert** (same `_PRESERVE_IF_NULL_COLUMNS`-style set-once semantics already used for
  `street`/`house_number`/`published_at`, `scraper/db.py:169-174` — never touched by `ON CONFLICT
  DO UPDATE`, exactly like `first_seen_at` today).
- Mirror the existing "keep the original `enqueued_at` on re-enqueue" rule
  (`scraper/db.py:2429-2431`) for `discovery_seq` too — a listing's sequence value is a
  first-discovery fact, never regenerated on retry.
- Excluded from `_HASH_FIELDS`, same as `published_at` — no snapshot noise (rule #2 intact).

This is a pure schema + shared-framework change. No per-portal code changes. It neutralizes
reorderings #2–#5 from Root cause 2 entirely (batch ties, completion-order fetch, cross-process
interleaving) because the value is fixed at enqueue, not write. Priority-bucketing (#1) doesn't
actually disturb it: `discovery_seq` values for the "new" bucket are still assigned in the true
page-walk sub-order — priority only affects *when* a row gets claimed/processed, not the sequence
value stamped at its original enqueue.

### Phase 2+3 — SHIPPED (backend): `listing_feed_public` + a per-portal-safe sort key

**Status: backend shipped in PR (migration 369, branch `feature/browse-portal-mirror`, stacked on
#945 since it depends on `discovery_seq`). Frontend wiring NOT shipped — see the follow-up spec
below.** These turned out to be one coherent piece, not two: `published_at` promotion (Phase 2)
only matters in the context of the new listing-grain view (Phase 3), so they shipped together.

**The read contract:** a new `listing_feed_public` view (migration 369) — listing-grain, never
touching `properties`/`browse_list`, so Root cause 3's golden-record leakage (trust-blended
`area_m2`/`district`/etc. from a *different* portal's sibling listing) cannot happen: every
column is unambiguously the filtered listing's own row. Identity is `id` (surrogate PK) +
`source_id_native` — not `sreality_id`, which is legacy/NULL post-Gate-2 for non-sreality rows.

**The sort key resolved a subtlety the original plan glossed over.** `published_at` and
`discovery_seq` live in different domains (a timestamp vs. a bigint sequence) and can't be
naively COALESCEd into one global ordering — a 3-month-old bazos `published_at` would then
outrank a listing discovered 2 minutes ago on a portal with no date signal. The fix only works
*because* this view is always queried scoped to one portal (Phase 3's whole premise): a plain

```sql
order by portal_date desc nulls last, discovery_seq desc nulls last, id desc
```

self-selects the right effective key per portal with **no per-portal branching in the reader** —
`portal_date` is a view-level `CASE WHEN source IN ('bazos','ceskereality') THEN published_at END`
(the only two sources where it's a reliable signal today; sreality's is deliberately excluded
despite being non-NULL — its ~40%-populated day-granular `published_at` would rank a
stale-dated row above a same-day discovery within sreality's own result set, the same
domain-mixing problem one level down). For every other source `portal_date` is NULL for the
whole filtered result set, so `discovery_seq` becomes the *functional* primary key — a pure
data-driven fallback, not a code branch. Adding bezrealitky once its `timeActivated` actually
populates (migration 266 — wired but NULL today) is a one-line `create or replace view`.

A covering index (`listings_feed_sort_idx`, same migration) mirrors `browse_list`'s proven
pattern — filter columns first (`source, is_active, category_main, category_type`), then the
same two-column sort expression, then `id` for the keyset tiebreak — chosen defensively (matching
this codebase's established answer to this exact class of problem) since this session couldn't
run a live EXPLAIN to confirm a plain indexed `listings` scan would hold the anon 3s budget without it.

#### Frontend — SHIPPED (migration 370 + Browse wiring)

Operator direction (2026-08-04), which overrode two of the open questions this section
originally raised: **the filter engine does not change and neither do the "mechanics" per
surface.** One portal selected means Browse shows that portal — rows, count and map together —
rather than a special mode with per-surface rules. That collapsed decisions 4 and 5 into one
answer: every cohort surface switches together, or none does.

**What the mode is.** `portals.length === 1` → the Cards, Table, Count and Map fetchers read
`listing_feed_public` instead of `browse_list` / `properties_map_mv`. 0 or ≥2 portals keeps the
deduped property view unchanged, which is precisely what dedup exists for. A `mirroring <portal>`
chip in the Browse header states when it is active — the count changes meaning, so it is said out
loud rather than left to be inferred.

**Root cause 3 was worse than this doc originally recorded.** It documented the golden-record
field leakage; it did not notice that the portal filter also drops rows outright. `properties.source`
is the *representative* child's portal, so a property whose repr is sreality is invisible under
`portal = idnes` even with a perfectly good active idnes listing. Measured live 2026-08-04:

| Portal | Properties with an active listing there | Hidden by today's filter |
|---|---|---|
| idnes | 109,034 | **23,429 (21%)** |
| ceskereality | 63,898 | 11,913 (19%) |
| realitymix | 47,250 | 9,132 (19%) |
| bazos | 29,741 | 2,939 (10%) |
| sreality | 99,272 | 1 |

The listing-grain feed has no representative to pick, so the mode fixes this as a side effect.

**Migration 370 made the view serviceable.** 369 shipped a bare projection; three gaps blocked the
swap. (a) Seven filter columns Browse dispatches don't exist on `listings` at all
(`place_search_text`, `tom_days`, `last_change_at`, `home_obec_pop` + the eight `near_*`, the four
`price_change_count*`, `total_price_change_pct`) — against 369's view each is a PostgREST 42703, a
hard 400, not a silent no-op. 370 derives the listing-grain ones and joins `properties` for the
genuinely property-grain ones (none is displayed, so no leakage path). (b) 369 had **no publication
gate**, so single-portal mode would have surfaced the 12,784 active-but-unpublished properties
Browse deliberately hides; 370 reproduces `browse_projection`'s gate verbatim. (c) The sort key —
below.

**The three-column ORDER BY became one column.** `portal_sort_key` = 12-digit UTC-epoch
`portal_date` ‖ 19-digit `discovery_seq`, NOT NULL, `COLLATE "C"`. Byte order is identical to
`portal_date desc nulls last, discovery_seq desc nulls last` (verified against a synthetic matrix
covering NULL date, NULL seq, bigint max and same-day ties: 0 positional differences for every
input at or after the epoch; pre-1970 dates deliberately clamp into the sorts-last bucket, and
there are 0 such rows). The point is the reader, not the database: keyset pagination anchors each
page on the previous page's sort value, and PostgREST can only express that as an `or=()`
disjunction — two nullable sort columns plus a tiebreak means a nested six-disjunct tree with four
NULL phases. One NOT NULL column keeps `applyKeyset`'s existing, proven single-column machinery.
`to_char` cannot be used here at all (both timestamp overloads are STABLE, so the expression index
is rejected); the epoch form is the immutable equivalent.

**`property_id` is not a legal tiebreaker at this grain** — the correctness trap in this work.
7,951 properties carry more than one active listing on a single portal (18,521 rows, live). A
keyset tiebreaker must impose a total order, and a React row key must be unique, so the mirror lane
anchors both on `listing_id`. `applyKeyset` / `nextCursorFrom` / `withKeysetColumns` take the
tiebreak as an argument so the two can never be mixed.

**Verified live, not just reviewed.** Keyset paging was simulated in SQL against the real view —
the exact predicate PostgREST emits, 10 pages deep, per portal — and compared row-for-row against
the straight `ORDER BY … LIMIT`: **0 mismatches** on bazos, idnes, ceskereality, realitymix and
sreality, including the pathological ties this file's own frontend spec worried about (237 of
bazos's top 240 rows share one key; idnes 213; realitymix 210). Card page: 11.6 ms, index scan on
`listings_portal_feed_idx`, no sort node. Map at the full 50k cap on the largest portal: 6.86s →
**1.52s** after adding `properties_gate_cover_idx` (the gate probe becomes index-only instead of
52k random heap reads). Worst-case exact count (idnes, no other filter) is 2.88s, which trips
`fetchBrowseCount`'s existing 2.5s budget and degrades to the planner estimate rendered as "~N" —
the designed fallback, not a regression.

**Day-one behaviour, and why it improves on its own.** `discovery_seq` is NULL for every row
written before migration 368 (a stated non-goal — no retrofit). For the two portals with a
trustworthy `portal_date` (bazos, ceskereality) that changes nothing: the date half of the key
dominates and the order is right immediately. For the other seven the legacy rows all share the
identical all-zeros key, so the mirror currently falls back to the `listing_id` tiebreak —
surrogate-PK order, a reasonable proxy for "newest in our archive" but not portal order. The
useful part is that this self-corrects in the right direction: any row WITH a `discovery_seq`
sorts above every zero-key row, so newly discovered listings float to the top from the first
drain onward, and the resolution of the ordering deepens as the sequence accumulates. Measured
~40 minutes after 368 was applied: 1,782 sreality rows already carried a sequence, plus
bezrealitky, realitymix, idnes, bazos, remax and ceskereality. maxima and mmreality were still at
zero — expected for maxima (tiny catalogue) and worth a look for mmreality, whose drain is
disabled via `realtime_drain_disabled_sources`.

**Deliberately not changed — the Stats tab.** It is a property-grain RPC
(`browse_stats_properties`); mirroring it needs a listing-grain twin, which is a separate piece of
work rather than something to half-do here. In single-portal mode Stats therefore still describes
the deduped property cohort while the list describes the portal's listings.

**The grain is stated in the UI, not inferred.** One portal and several portals produce rows that
count differently, and nothing on screen said so — the `mirroring {portal}` chip named the mode but
not its consequence. `RowGrainNotice` (below the cohort count) carries the two explanations: with
one portal, each row is one of that portal's listings, so a property posted twice there appears
twice; otherwise one row per property, with the merged record's provenance spelled out (price /
disposition from the representative child, every other field from the golden-record CTEs, so a card
can mix portals and match none of them exactly — 9.4% of multi-portal active properties disagree on
price outright, median spread 7.4%). Dismissal is per variant and per browser: the two say opposite
things, so dismissing the everyday one must not suppress the other's first appearance.

**Follow-up this surfaced:** the ≥2-portal case still filters on `properties.source`, so it keeps
the row-hiding bug above and the count can *drop* when a second portal is added. Fixing it properly
means a property-grain "has a child on portal X" predicate (a `sources` array or an EXISTS on
`browse_projection`) — worth doing, out of scope for this PR.

### Phase 4 — sreality: separate discovery from completeness, drop the district-split for discovery

Revised after a follow-up verification pass (2026-08-04) that overturned the original
recommendation here. The original text proposed either accepting sreality's fidelity ceiling or
restructuring the district-split into a round-robin walk — both wrong, because they treated
"discover new listings" and "verify nothing's been delisted" as the same job. They aren't, and
splitting them removes the scrambling entirely without touching rule #3's completeness guarantee.

**The 422 that forces the district-split is triggered by offset depth, not category size.**
`scraper/sreality_client.py:50-53`: *"The search endpoint refuses offsets past its deep-pagination
window with HTTP 422."* `SPLIT_THRESHOLD=10000` (`scraper/sreality_client.py:66-67`) is a
pre-walk heuristic — probe the total count, and if it's large enough that a full sequential walk
would *eventually* reach the deep-offset window, split by district *so the full walk can still
finish*. It is not itself a per-request limit. A **shallow** walk (offset 0, stopping after a
handful of pages) never approaches that window regardless of how large the category actually is —
confirmed: nothing in the client or `main.py` ties the 422 to `result_size` directly.

**This means the current district-split is only necessary for the completeness function — the
full walk that touches every currently-active listing for rule #3's ≥99.5% gate — and is not
needed at all for discovery.** Today sreality has no separate discovery mechanism: it's excluded
from the shared newest-first probe (`scraper/realtime_worker.py:161-164`'s `REALTIME_SOURCES`
omits sreality; comment at `:29-31`, *"sreality's v1 API ignores sort params, so a newest-first
probe is impossible for it"*), so every new listing is discovered only via the same district-split
full walk that also does completeness — which is exactly what scrambles order (Root cause 2).

**The other 8 portals already prove the fix pattern.** 7 of them use a generic page-capped probe
(`scraper/portal_runner.py:240-364`, `run_index_probe`) that's cheap and separate from their full
walk. ceskereality — the other portal with no reliable default sort — has a *bespoke* probe
(`scraper/ceskereality_main.py:301-371`, `probe_category`) that checks the DB **after every page**
(`:340-343`, via the existing `db.index_summary_native` helper) and stops on the first page that
comes back **entirely already-known** (`:369`, comment at `:310`: *"early stop on the first
all-known page"*). This is precisely the "known prefix" stop condition this doc's discovery/
completeness split needs — it already exists, battle-tested, one portal away.

**Proposed change:** give sreality its own `probe_category`, modeled directly on ceskereality's,
using the existing `db.index_summary` bulk-existence helper (`scraper/db.py:1546-1571`, already
used by sreality's own full walk, so no new DB plumbing) in a per-page early-stop loop —
**unsplit**, since the shallow offset never risks the 422 regardless of category size. Add
sreality to `REALTIME_SOURCES` so it runs on the same cadence as the other 7. Since the probe
stays cheap at any category size, there's no need to keep the hourly category-rotation trick for
it either — walk all 20 category pairs every cycle instead of rotating a subset through by hour
(the rotation was a fairness trade-off for the *expensive* full walk; it has no reason to carry
over to a shallow probe). The existing district-split full walk is **unchanged** — it keeps running
at its current cadence, doing `touch_listings`/`mark_inactive` completeness work (rule #3), and may
still enqueue ids the probe already found; the "keep the original `enqueued_at`/`discovery_seq` on
re-enqueue" rule (already existing behavior, extended in Phase 1) makes that a harmless no-op.

Net effect: brand-new sreality listings get discovered and enqueued via a fast, unsplit,
early-stopping pass whose relative order is a single coherent walk (not 77 sequential per-district
completions), giving `discovery_seq` (Phase 1) a materially better signal than today — closing the
gap flagged in `scraper/realtime_worker.py:29-31` using the same shared-framework mechanism 7
other portals already rely on, with no new bespoke infrastructure beyond one `probe_category`
function (rule #21-compliant: this is the *second* sanctioned per-portal hook, in the same spirit
as ceskereality's, not a departure from the pattern).

**Verified not needed elsewhere:** no portal besides sreality splits geographically for pagination-
cap reasons. ceskereality's region×facet split (`scraper/ceskereality_main.py:97-190`) is a
*different* mechanism for a *different* trigger (a hard ~240-result anon page-count wall, not a
deep-offset 422) — and it's irrelevant to ordering anyway, because ceskereality's probe already
requests a genuine newest-first sort from the portal (`/nejnovejsi/`) and doesn't go through the
region split at all. Every other portal's `split`/`max_pages` hits are the unrelated cadence split
(rule #19: index-walk vs. detail-drain phases), not geographic partitioning.

**Remaining honest limitation:** even with this fix, sreality's walk order is *our* best
reconstruction of "recently added," not a portal-guaranteed newest-first order — the public API
still can't be asked to sort, so there's no way to confirm the shallow walk's default order
actually clusters new listings near the front, only that it's no longer artificially scrambled by
district-then-district sequential completion. Recommend a quick empirical validation (compare probe
convergence against known recent listings) before fully trusting it, and treating the semi-public
BFF `sort:'-date'` lever (below) as a possible future upgrade if the default-order assumption
doesn't hold up — not a blocker for shipping this phase.

### Phase 5 — close the unverified-sort assumption

Independent hardening, adjacent but not required for the above: bazos/idnes/realitymix/remax
assume default index order is newest-first without ever requesting it via a sort param (unlike
bezrealitky, which does). Recommend adding an explicit sort param where the portal's search
supports one, converting a silent assumption into a verified guarantee — this is a **completeness**
risk (rule #3), not just a display one. Similarly, ceskereality's *main* ingestion walk still uses
its non-newest-first default order — only the separate delta-probe uses `/nejnovejsi/`; worth
checking whether that sort slug returns the *same* full result set (just reordered) before
switching the main walk to it, since rule #3's completeness gate depends on full-walk fidelity.

## Sreality: the semi-public sort lever (still not part of this program)

The existing `roadmap/next.md:60-63` "still open" item notes a HAR spike proved sreality's
internal Next.js BFF accepts `sort:'-date'` — an undocumented, semi-public API, not the scraper's
current public v1 JSON endpoint. This remains out of scope even after Phase 4: it carries real
risk (internal API, no completeness guarantee, could break silently on any frontend redeploy) that
doesn't belong in the authoritative pipeline. If Phase 4's empirical validation shows the public
API's default order genuinely doesn't cluster new listings near the front, this becomes the
natural fallback to revisit — but only as a targeted follow-up, not a prerequisite.

## Non-goals

- Changing scrape frequency/cadence for the *completeness* walk — explicitly out of scope per the
  operator's framing. (Phase 4's new discovery probe running frequently is not a frequency change
  to the scrape itself, the same way the other 8 portals' existing probes aren't — it's a cheap,
  additive, discovery-only pass alongside the unchanged full walk.)
- Depending on sreality's undocumented internal BFF API — Phase 4 reconstructs order from the
  existing public API via early-stop walking, not from an unsupported endpoint.
- Retrofitting `discovery_seq` onto historical rows — NULL for pre-existing listings is acceptable;
  it only needs to be right going forward, and NULLs sort last/first predictably.

## Open decisions for the operator

1. ~~Ship Phases 1–3 now~~ **Approved 2026-08-04** — build Phases 1–3.
2. ~~Direct indexed `listings` query vs. a second blue-green read model for Phase 3~~
   **Resolved: direct indexed view** (`listing_feed_public` + `listings_feed_sort_idx`,
   migration 369) — chosen without a live load test (none available this session); revisit if
   production EXPLAIN shows it can't hold the anon 3s budget.
3. ~~Phase 4~~ **Approved 2026-08-04, in revised form** — separate discovery from completeness,
   give sreality its own `probe_category` (ceskereality pattern), unsplit, added to
   `REALTIME_SOURCES`. No other portal needs this.
4. ~~Should single-portal mode swap the header count / Stats tab too?~~ **Resolved by operator
   direction 2026-08-04: every cohort surface switches together** — "it is always the filter that
   is applied… the same mechanics, the number just mirrors the portal's count." Count follows the
   rows. Stats is the one exception, for the mechanical reason that it is an RPC with no
   listing-grain twin (see the frontend section).
5. ~~Should the map stay property-grain?~~ **Resolved: no** — same direction. The map mirrors the
   portal like everything else; the original "near-duplicate pins" worry doesn't apply, because
   dedup collapses ACROSS portals and the mirror is scoped to one. The real constraint turned out
   to be latency, fixed by `properties_gate_cover_idx`.
6. **New:** the ≥2-portal case still filters on `properties.source` and so keeps the 10–21%
   row-hiding measured above. Fix with a property-grain "has a child on portal X" predicate?
4. Phase 5 (verified sort params + ceskereality main-walk sort slug) — bundle into this program or
   track separately as its own hardening PR? Still open.
