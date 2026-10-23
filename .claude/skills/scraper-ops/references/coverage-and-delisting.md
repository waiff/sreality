# Coverage and delisting: the (formerly parked) flag, the slice ledger, and the retired gate

Loaded on demand from `scraper-ops`. Everything here is about one question —
**may this portal delete listings?** — and the four layers that now answer it
instead of a boolean somebody typed once.

## The flag is a claim about US, not about the portal

> **2026-09-07:** delisting is now presence-verified (rule #3): a finished walk
> nominates its unseen rows for a page check and the drain's fetch decides. The
> flag below no longer gates anything. The history stays because it is why the
> design changed.
>
> **2026-09-08:** what "finished" means changed too. The nomination gate is
> **structural** — `walk_reached_end`, "the walk reached the portal's end" — and
> the count comparison (`walk_coverage`) is a LOGGED WARNING, never a gate. See
> "The structural gate" below; the arithmetic in this file still governs the
> descent/resample triggers and the ledger's `outcome`, which did not change.
>
> **2026-10-19 (Health wave 4):** the coverage gate is retired — it had nothing
> left to evaluate once every scraper portal's flag was true (newest evaluation
> 2026-09-13) — and with it the ceskereality and mmreality slice writes only it
> read; idnes keeps its ledger for its own stalest-first order. Health measures
> coverage instead: the `coverage` gauge and the `scraping` and `delisting`
> alarms (`references/pipeline-verification.md`).

`portals.supports_complete_walk` used to gate the absence sweep (architectural rule #3; the
sweep, `mark_inactive`, was deleted in 2026-10):
a portal that could not prove it saw the whole catalogue never delisted from index
absence. It was set true for idnes when the walk *could* in principle be
complete — and then stayed true for months while the walk was reaching 13% of
the biggest category.

That is the failure mode to remember: **the flag does not decay.** Nothing
re-checks it, so it goes on asserting whatever was true the day it was typed.
Two portals were parked on it for exactly that reason (history: the flag gates nothing
since 2026-09-07):

| portal | parked in | why |
| --- | --- | --- |
| ceskereality | migration 449 | walk rebuilt onto the 14-kraj partition; ~29,400 rows would become delist-eligible in one pass |
| idnes | migration 453 | 64% of 109,908 active rows unseen >7d while the flag still authorised delisting |

**A portal cannot prove it saw everything if we have not.**

## Why the idnes walk was not merely slow

Two independent defects, and only one of them is about speed.

**The silent throttle.** idnes soft-throttles our datacenter egress and says
nothing about it: pages arrive in ~2.3s each for exactly 20 requests, then one
request stalls for ~390 **seconds** and returns `200`. No 429, no 403, no
exception, no retry — so `penalize()` never fires and every rail we own stays
quiet. Twenty-four such stalls consumed 143 of one 160-minute run, with zero
errors recorded. Residential IP over 26 consecutive requests: no stall at all,
0.62 pages/s against 0.047. Hence `IdnesClient.USE_PROXY` — but with
`PROXY_REQUIRED = False`, because idnes only *degrades* without the proxy where
ceskereality and mmreality hard-403, and the realtime worker skipping a slow
portal would trade degraded data for none.

**The amnesia, which was worse.** A walk starts at the first category's first
page every run. When the budget expires the next run starts from the same place,
so a catalogue bigger than one budget does not get walked slowly — the same
*head* gets walked repeatedly while the tail is never reached. idnes: 11 of 14
runs killed by the clock, and the one that finished covered 2 of 10 categories.
The other 8 were not walked slowly. They were not walked. No budget increase
fixes that; it only moves where the restart happens.

## The 15-slice partition

Each idnes category is walked as the 14 `CZ_KRAJ_SLUGS` (shared in
`scraper/portal.py` — ceskereality publishes character-identical slugs) plus the
abroad bucket.

- **Abroad is `?s-l=STAT-XX`, a query parameter.** Every `/zahranici/` path
  spelling 404s. It is not optional: the kraj slices sum to 15,319 of the 27,372
  flats for sale, so a slice set built from the region nav alone would report
  **56% of the portal as 100% of it**.
- **Proven a row-level partition by ID enumeration**, never by matching counts —
  an overlap and a gap of equal size produce matching counts, which is how the
  first such proof on ceskereality was correctly refuted. 755 listings, 14
  slices, zero overlap, zero gap, zero surplus; `kraj_sum + abroad == national`
  on all 10 categories.
- **Slicing here is not a reach workaround.** idnes has no pagination cap (page
  1,052 serves the declared tail, 1,060 404s). It buys 15 declared totals to
  check instead of 1, and units small enough to finish and be remembered.
- **The empty slice is the trap.** A slice with nothing in it publishes no
  count, so `total` is `None` — byte-for-byte what a degraded page returns, and
  conflating them is how a broken fetch reads as "this region is empty". idnes
  states it out loud ("momentálně tu není žádný inzerát" →
  `IndexPage.empty_confirmed`), so a confirmed zero is a real measurement.
  ceskereality has no such string and must read the page twice instead.

## Descending when paging cannot reach the tail

**A pagination pass of an idnes slice is close to a RANDOM 75% SAMPLE of it —
not a slightly-lossy walk.** That is the single most important fact about this
portal's index, and it took two measurements to see properly. First, that pages
of one query overlap and the loss compounds with page count:

| slice | pages | collected / declared | |
| --- | --- | --- | --- |
| `stredocesky-kraj` | 67 | 1,675 / 1,675 | exact |
| `jihomoravsky-kraj` | 71 | 1,693 / 1,698 | 99.7% |
| `praha` | 154 | 2,948 / 3,839 | **76.8%** — 27% of page slots were repeats |

Then, that two full passes of the SAME url, back to back, barely agree:

| praha, same URL, twice | rows | pages |
| --- | --- | --- |
| pass 1 | 2,847 | 153 |
| pass 2 | 2,860 | 153 |
| **union** | **3,576** | — |

They overlap on only ~2,131 of 3,818 declared, and pass 2 held **729 rows pass 1
never showed**. Paging harder within one pass does not help — the pager genuinely
ends. Reading the slice AGAIN does, and that is a different remedy: the right
response to a sample is more samples.

Three things follow.

**The `new_on_page == 0` stop was actively harmful.** It existed to defend
against idnes clamping an out-of-range `?page` to the last page — but with
unstable ordering a legitimately mid-walk page can be entirely rows we already
hold, and it ended Prague at 594 of 3,839 on the first production run.
`next_offset is None` is the reliable terminator (the clamped page reports it);
`_MAX_SLICE_PAGES` is the loop backstop.

**A short slice descends, and the parent walk is KEPT.** This is the measurement
that decided the design, and neither half of it is optional:

| | collected / 3,840 | |
| --- | --- | --- |
| parent `praha` alone | 2,948 | 76.8% ✗ |
| its ten obvody alone | 3,777 | 98.4% ✗ |
| **the union of both** | **3,830** | **99.74% ✓** |

Verified end-to-end through the real code path: `praha` → 3,825 / 3,840 =
**99.61%, `exhausted`**, 308 pages, 289s.

**A slice still short after descending is RESAMPLED** (`_RESAMPLE_PASSES`, 2).
This is what makes praha reliable rather than a coin flip: it was landing at
99.35 / 99.61 / 99.74% across runs against a 99.5% bar, so its category passed
only sometimes — and the gate needed THREE CONSECUTIVE passes, which a coin flip
essentially never delivers. Bounded and self-limiting: only a short slice
resamples, and it stops the moment a pass adds `<= _RESAMPLE_MIN_GAIN` rows,
because a pass that adds nothing means the union has converged and the shortfall
is not sampling loss. A slice that can never converge costs **two extra fetches**,
not a budget.

### Two axes, tried in order

**Place** first — the site's own hierarchy, and on the Czech side very nearly a
partition. A kraj links its okresy, Prague its ten obvody, the abroad bucket one
`s-l` value per country; those 38 countries sum **exactly** to the abroad total
(12,054 = 12,054). `stredocesky-kraj`'s 12 children likewise sum exactly to
1,675.

**Price** second, for a place with no sub-places at all: **Spain is 8,613 flats
over 345 pages and advertises no regions**. Without a second axis that slice
could never finish, and one unfinished slice holds its whole category open
forever. The ladder opens at both ends and a band that is still too big splits
geometrically (prices are log-distributed; an arithmetic midpoint would leave
nearly everything on one side).

### Why the parent walk is what makes either axis safe

**Neither axis is a partition**, and both leak in the same direction:

- 60 of Prague's 3,840 listings are too vaguely addressed to file under any obvod
- 110 of Prague's 3,840 and 6 of Spain's 8,613 have no price at all, so they fall
  outside every band

The unfiltered walk of the same place is what holds those remainders. That is why
the parent's rows are merged with its children's rather than replaced by them.

The child list is **scraped, not declared** — which on ceskereality would be a
mistake, since its facet block is a top-10-by-popularity list rather than a
partition. It is safe here only because **the arithmetic checks it**: a missing
child leaves the union short and the slice stays `incomplete`, while a spurious
child can only add rows of the same category, which cannot push the union past
the declared total. The link list never has to be trusted.

Descent runs **only on `incomplete`** — we paged to the pager's own end and came
up short, which is the shortfall a finer query can fix. An `error` is a transport
failure and a `degraded` page carries no total to measure a union against;
descending on either would just multiply failed requests and relabel a fetch
problem as a coverage one.

## The ledger (`portal_index_slices`, migration 454)

One latest-wins row per `(source, category_main, category_type, slice_key)`:
`walked_at`, `outcome`, `declared_total`, `collected`, `pages`.

- Only `exhausted` is positive. `deadline`, `error`, `degraded` and `ceiling`
  are **missing evidence**: any one of them held its whole category open for the
  retired gate — 14 good slices and one hole is not 93% coverage.
- **`outcome` stayed NUMERIC on 2026-09-08**: the gate counted
  `outcome='exhausted'` streaks, so re-pointing it at the structural verdict would
  have silently re-baselined it. Since the gate's retirement (2026-10-19) nothing
  reads the column — idnes orders by `walked_at` alone — so it is evidence for a
  human. The structural verdict travels beside it, in code (a field on the slice
  result) and in the `SLICE`/`CATEGORY` log lines, not in the ledger. Consequence
  to expect: a slice can read `outcome='degraded'` (one row short) and
  `stop=pager_end` (reached idnes's last page) at the same time. Both are true;
  only the second decides nomination. **Never read `exhausted` as "this
  nominated".**
- **Both the category order and the slice order are least-recently-walked
  first**, and an absent row sorts to *infinity*, not zero. Treating unknown as
  fresh would sort exactly the never-walked slices last, which is the starvation
  the table exists to end.
- Category ordering matters as much as slice ordering: the runner walks
  categories in sequence, so one that eats the whole budget starves the rest
  however its own slices are sorted. That is what left 8 of 10 never walked.
- Both helpers are best-effort. An unwritten slice looks stale next run, and an
  unreadable ledger means "walk everything" — the failure direction is always
  *more* walking.

```sql
select category_main, category_type, slice_key, outcome, collected, walked_at
  from portal_index_slices where source = 'idnes' order by walked_at;
```

**mmreality wrote one row per category until 2026-10-19**, when its slice write
went with the gate (its ten rows stay, unread: Health wave 9 left them to the
scraper track). Its ten per-(sale type, property type) indexes page to their
tail with no second axis (the largest is ~300 pages of 12), so each category was
one slice, `slice_key='national'`, `exhausted` only when the shared arithmetic
accepted the collected count against the page's own `metadata.count`; the walk's
verdict now shows in its `RECONCILE-INDEX` line. That evidence is what un-parked
it: the portal was parked in 2026-05 as "a single mixed index with no result
total", and both halves were wrong (the bare feed was prodej-only and did
declare a count — see `docs/architecture.md` § mmreality).

## The gate (`coverage_gate.yml`, migration 455) — retired 2026-10-19

`scripts/coverage_gate.py` (cron `15 3,9,15,21`, three hours after each walk cycle
started) re-earned `supports_complete_walk` from the ledger on two questions.
**Covered**: did every slice of every *declared* category finish inside 30 h
(`FRESHNESS_HOURS`)? The declared count was the denominator, because a
never-walked category has no ledger rows, and counting only what the ledger held
would let a portal pass by walking a subset perfectly — precisely idnes's failure;
and it counted CANONICAL categories (`portal_factory.canonical_category_count`:
ceskereality's `rodinne-domy` and `chaty-chalupy` both canonicalise to `dum`).
**Stable**: had that held for three consecutive evaluations with the
delist-candidate count within 15% between them? A walk that reaches every slice
but enumerates a different population each time is *sampling* the portal, not
covering it. Every evaluation, holds included, went to `portal_coverage_gate`.

Health wave 4 deleted it — the script, the workflow and its tests,
`canonical_category_count` and `_config_for`, and the two slice writers only it
read (ceskereality `_record_slices`, mmreality `_record_slice` and `SLICE_KEY`) —
because since 2026-09-07 the flag gates nothing (a complete walk nominates and the
page decides) and, once every scraper portal's flag was back to true, it had
nothing left to evaluate (its newest evaluation is from 2026-09-13). What it
reported as posture is measured now: the `coverage` gauge holds each unit's active
ads against its declared total and counts the active ads no walk can nominate, and
the `scraping` alarm's walks arm times each unit's newest walk that reached the
end. Migration 643 (Health wave 9) dropped `portal_coverage_gate` (153 rows). It
did not drop `portals.supports_complete_walk` (no writer now; the Scrapers page's
portal posture still reads it) or the 150 ceskereality and mmreality rows of
`portal_index_slices` (unread, unjudged): the scraper track owns both.

### ceskereality's descent over the 99-page ceiling

A kraj past the 99-page ceiling is re-walked per subtype (and a subtype child
itself past it, per okres — `_DESCENT_AXES`: the okres axis is a declared map, the
subtype axis is declared where measured, else the kraj page's links; each level
checked against its parent's declared total) and comes back as several
`SliceResult`s sharing one kraj; an okres grandchild keeps its parent kraj in
`kraj` and rides the okres in `okres`. Every descent logs `DESCENT cm=…
axis=subtype|okres declared=… slugs=…` (INFO); `grep 'DESCENT.*axis=okres'` is how
to see the okres level fire. A residue or deadline marker declares only its
unwalked part. Until 2026-10-19 the walk also wrote ONE LEDGER ROW PER KRAJ, never
per subtype, for the gate alone: a `kraj/subtype-*` row from an old shape would
never have been re-walked and would have held the gate shut forever.

## Why walks still stop short, and what that costs (2026-09-05)

Every "walk that did not finish" on sreality and idnes in the last three days was
the `--max-seconds` budget expiring cleanly — 20.1-20.4 min and 150.3-151.0 min
respectively, never a crash. The budget is a few minutes smaller than the work,
and the work is set by how the walk is SLICED, not by listing count.

**sreality.** Twenty category pairs; any pair over `SPLIT_THRESHOLD` (10,000) is
walked as 77 okres queries because the national list cannot page that deep. Five
pairs are over the line, and they cost ~425 of a full walk's ~465 pages; the other
fifteen cost ~39 combined. A full walk needs ~24 min. The category ORDER rotates by
run hour (`_rotated_categories`, `scraper/main.py`), so nothing starves at that
level — every pair completed >=27 times in 7 days. **The fairness hole is inside
the split**: `DISTRICT_IDS` is walked in fixed order with no memory, so a pair cut
at district 29 restarts at district 1 next run. Politeness is not the constraint
(limiter at 14% of 2 req/s, never a 429); ~41% of per-page time is the DB phase
(`index_summary` + `touch_listings`). The `*/15` cron is throttled by GitHub to
~7 runs/day, so each 20-min run was the only walk for ~2 h. Budget raised to 1800 s.

**idnes.** ~4,360 pages per full cycle. Per-page latency through the residential
proxy swings 1.4-4.0 s with the time of day; break-even at 9000 s was ~2.06 s/page,
so half the runs fit (night) and half hit the deadline (afternoon). The ledger
rotation makes that harmless for coverage — all 150 slices stay fresh — but each
truncated run reports one category incomplete. Budget raised to 13000 s (3.0 s/page).

**The worker's probe does NOT keep sreality fresh.** `PROBE_PAGES = 1` and sreality's
API ignores sort, so it re-touches the same ~10,000 rows every pass. The walks are
the only coverage mechanism.

**A lost lock fight that discards a finished walk.** The last step of a category
walk (`portal_runner.reconcile_sightings`) bumps `last_seen_at` for every sighted row
we already hold, whatever its index price (`touch_listings_by_id` on all nine
portals). Twice it has died AFTER every
page was fetched, and each time the category was recorded as collected=0 and its
sweep skipped: a `DeadlockDetected` against a concurrent writer (sreality
komercni/prodej, 2026-09-05 10:38, 3 of 46 runs), and a `QueryCanceled` — "statement
timeout … while locking tuple" — when idnes dum/prodej and ceskereality
komercni/prodej both sat the full 2-minute `statement_timeout` behind the hourly
MF-yield recompute (`recompute_mf_yields.yml`, cron `25 * * * *`; migration 257's
`recompute_mf_gross_yields()` is one `update listings` in one transaction), which
held row locks for 6½ minutes on its own deadlock retry (2026-09-05 21:22–21:30,
from the Postgres lock-wait log). Both touch functions now retry a chunk up to three
times on either error — safe because the walk connection is autocommit and both
statements are idempotent; a lock-wait cancel pauses longer (5 s × attempt) than a
deadlock (0.5 s × attempt) because the holder is usually committing by then. The
touch statements are indexed lookups over ≤250 ids and never take two minutes on
their own, so a cancel there is a lock wait, not slowness. The root cause — one
multi-minute transaction over a hot table — left with that job (MF is a read-time
SQL measure since migration 565); the retry stays for any other long writer.

**Not budget stops:** ceskereality's and bazos's "short" categories all sit at
99.0-99.5% — the declared count drifting a few rows during the walk, on slices
where one row is half a percent. A threshold artefact, not a coverage failure —
and since 2026-09-08 not a nomination failure either: those walks reached the
portal's end, so they nominate and log a `COVERAGE` warning.

## The structural gate (2026-09-08)

The 5th element of every portal's `walk_category` used to be arithmetic and is
now structural. **`reached_end` := every unit the category is defined over was
walked, AND each unit's page loop exited on a PORTAL terminator, AND no stop of
OURS fired anywhere in the category.** The vocabulary is shared so it means one
thing on all nine portals (`scraper/portal.py`): `StopReason`, the `PORTAL_ENDS`
/ `OUR_STOPS` frozensets, `stop_is_portal_end(reason)`, and the one conjunction
`walk_reached_end(portal_end=..., our_stop=...)`.

| verdict | reasons |
| --- | --- |
| PORTAL end (may nominate) | `pager_end`, `declared_total_reached`, `short_page`, `empty_confirmed`, `clamp_repeat` |
| OUR stop (nominates nothing) | `deadline`, `page_cap`, `limit`, `slice_subset`, `slice_unreached`, `error`, `pager_stalled`, `cap_wall`, `barren` |

**Why.** A numeric per-slice AND cannot pass on a live index: ceskereality's
20,964-row houses-for-sale category nominated nothing for days because one
87-row Karlovarský slice collected 86 (`0.9885 < 0.995`) — while the
category-level arithmetic would have passed at 0.99995.

**Two disciplines that keep it honest, mandatory on every portal.**
*Items-first*: a loop that breaks on `not items or next is None` must test
`not items` FIRST — a blocked or throttled HTTP 200 also has no pager, and
sharing one break with the real last page is exactly what the numeric gate was
quietly covering for. *The barren rule*: a zero-item page is `barren` (OURS)
until re-fetched once through the limiter and still empty AND at or past the
position the declared total implies (or no total was ever readable and an
earlier page of this unit carried items) — then `empty_confirmed`. A
confirmation that cannot be obtained is not a confirmation.

**A terminator has to be HARD TO FORGE, per portal.** The shared vocabulary says
what a stop means; each portal still has to prove its own terminator, and the
2026-09-08 live probe (per-portal last page + the page past it) is what settles
what is provable. Rules that came out of it, all count-free or position-only —
the count must never veto:

- **The suspect page may not supply its own corroboration.** `total` /
  `declared` is latched ONLY from a page that carried items (remax, maxima,
  realitymix, sreality's high-water `result_size`). A blank "no results" or
  shell page renders its own smaller (or zero) counter, and reading it put the
  page past the end and confirmed itself.
- **A count may only be latched upward within a walk.** `offset >= declared` is
  a portal end, so a total that steps DOWN below the offset already reached
  would end a walk on a full page of items (bezrealitky, sreality).
- **`next is None` is not one fact.** It means "the pager said last page" AND
  "no pager rendered at all". Portals that use it as evidence must read the
  site's own end marker instead: ceskereality's `<a class="pagination-arrow
  --disabled --next">` (`IndexPage.pager_end_marker`), maxima's rendered pager
  (`IndexPage.pager_present`). bazos publishes neither, so a FULL page claiming
  no next page is corroborated by one fetch of the offset it would have pointed
  at (gone or empty = its tail; ads we never saw = the pager broke).
  mmreality's real last page still emits `<link rel="next">` (it over-advertises
  by one page), so a missing link is WALKED PAST, not trusted — the items-less
  page past the end is its only terminator, and it also honours the portal's own
  "nejsou k dispozici žádné nemovitosti" copy.
- **An offset API needs a progress guard.** bezrealitky counts rows returned, so
  a resolver that ignores `offset` marched it to the declared total on one page:
  a page that adds no new id is `pager_stalled` (OURS).
- **`clamp_repeat` only where clamping is proven.** No probe has yet observed a
  portal clamping a past-the-end request; remax and mmreality now break on a
  repeated page as `pager_stalled` (OURS), and only maxima / realitymix keep it,
  corroborated by position.
- **A clamped PAGE SIZE is not a short page.** sreality's `short_page` is a
  portal end, so the client adopts the `pagination.limit` the API actually
  served and advances by what arrived.

**What to read in a log.** Nomination itself is unchanged:
`VERIFY cm=… ct=… subtype=… candidates=… queued=… deferred=… active=…`, once per
scope; since 2026-10-22 a walk whose every unit reached the end adds
`VERIFY orphans candidates=… queued=… deferred=… active=…` for the active ads in no
walked scope (a NULL label, an auction type no unit walks). Two lines changed shape:

- `COVERAGE cm=… ct=…: the walk reached the portal's end but collected N of M
  (coverage=…) -- the gap is nominated, the page decides (rule #3)` — a WARNING
  emitted when a walk nominates while `walk_coverage != "complete"`. This is the
  warning that replaced the veto. **Do not silence it**: on a portal that could
  serve a forged terminator (a missing pager, an edge-cached repeat) it is the
  first thing that shows the walk ended early.
- `VERIFY skipped cm=… ct=…: the walk did not reach the portal's end (our stop:
  deadline / page cap / limit / slice never reached / error / barren page);
  collected=… result_size=… coverage=…` — the old wording said "walk looks
  incomplete", which now misdescribes a structural skip.

Portals also log their own stop per unit (`SLICE … stop=…`,
`WALK … stop=… reached_end=…`, `SPLIT summary … our_stops=…`); the stop reason
is **not** in `scrape_runs.by_category` yet.

**What lands in `scrape_runs.by_category`** (JSONB, no migration): each category
entry carries `walk_reached_end` (bool — did it nominate?) beside the existing
`sreality_result_size` (the declared total, spelled once as
`portal_runner.DECLARED_TOTAL_KEY`) and `collected`, and since Health wave 4
`total_is_count` (the adapter's "total" is the count of what it saw: remax, maxima)
and `declared_is_upper_bound` (ceskereality's national count, wider than its kraj
slices). A category that reached the end with `collected` short of the declared
total is the new normal case the old gate refused; since Health wave 9 its
`walk_coverage` verdict is logged (`COVERAGE`), not stored (older entries carry it
and `active_db`). The `scraping` alarm's missing arm and
the `coverage` gauge's residual arm read these rows (an entry whose
`total_is_count` is true or absent is Not measured there, never 0%); they replaced
the `walk_coverage` check.

The other half of that record is a rail too. **The `scraping` alarm's walks arm**
(Health wave 4; it replaced 2026-10-13's `walk_end_streak`) times, per unit, the
hours since the START of the newest finished walk in which it reached the end,
against its portal's own rhythm G, the median start-to-start gap of its finished
walks over 14 d: Watch above 3 G, Problem above 6 G — the operator's
three/six-walk ruling of 10-13 in time, about 43-44 h on the ~7 h crawlers and
26.5 h on sreality — or when a unit walked in the last 30 d has had no end-reached
walk in 14 d; a portal with no finished walk in 14 d is scored on the slowest
portal's G and reads Problem. Every portal of `claims_common.SOURCES` also gets
a `<portal>/walks` cell on every pass: Problem while no walk in 30 d recorded a
unit (no walk at all, or none finished), else OK with the hours since its
newest finished walk started, its units judged by their own cells. So a portal
cannot drop out (it keeps its inflow, fetch-error and unnominated cells), and
that cell's incident recovers on a measured OK. The walks arm exists because
ceskereality byt/prodej missed the end for 89 walks (2026-09-16 -> 10-13): nothing
was nominated, 13,373 active rows piled up against 9,771 on the portal, each walk
logged `VERIFY skipped`, and the old `walk_coverage` check read ~4.6% because it
averaged the gap over the whole portal. When it rings, the unit's `VERIFY skipped`
line names the stop and the portal's SLICE / DESCENT lines name the slice. Blind
spots, known: a walk that stops between two scopes of one label reads as reached
when every scope it walked reached the end (a scope it never started leaves no
entry), and a dispatched `--max-pages` / `--limit` / bazos region run finishes as
an index walk, which shortens G and never reaches the end (`scrape_runs` records
no run arguments).

## The four layers, in order (as rebuilt 2026-09-07, regated 2026-09-08)

1. **Coverage** — the sliced walk reaches everything (or records that it didn't).
   A walk that reached the portal's end nominates; the count comparison is a
   logged warning, never a gate.
2. **The ledger** — coverage accumulates across runs instead of restarting.
3. **Nomination, not deletion** — a finished walk queues the rows it did not
   see for a page check; the drain fetches each page and only a positive gone
   signal, confirmed by a second one at least `GONE_DWELL` (12 h) later with
   no sighting between, flips it (*Hysteresis* below). A wrong nomination
   costs one fetch, never a live listing; a wrong verdict costs one more.
4. **The throttle** — `delist_flip_cap` bounds how many checks one walk may
   queue (oldest-unseen first, the rest deferred and recorded), so a broken
   walk cannot flood the drain and a real backlog drains in a few walks. Mind
   its FLOOR: the cap only applies once a scope holds `min_rows` (2,000) active
   rows, so a small scope (maxima's ~220-row agendas, sreality
   `pozemek/drazba`, bazos subtype scopes) is unthrottled — one walk can
   nominate 100% of it, and presence checks are also exempt from the drain's
   gone-rate breaker. The "~10% per walk" bound is a bound on the BIG scopes
   only; a small scope's protection is the page check itself. A
   walk that saw nothing nominates nothing; rows already in the queue or
   checked in the last 24 h (`delist_policy.RECHECK_THROTTLE`) are not
   re-nominated, which is why the effective dwell is 24 h (*Hysteresis*
   below); given-up rows are re-armed 50
   per walk. The drain reserves 20% of each claim for checks so they can never
   starve behind refresh inflow, and Health reads the ingest queue through a
   view that excludes them (migration 483).

The gate re-earned `supports_complete_walk` from the ledger as a posture signal
until Health wave 4 retired it; nothing read the flag to decide a deletion after
2026-09-07.
The retired pieces — the staleness rail, the national cross-check, the latching
refusal — all existed to make absence safe; presence does not need them.

## Hysteresis: one verdict records, the second flips (2026-10-09)

**The finding.** Property 892034 (sreality 1169301580) showed inactive while live.
The 22:44 UTC walk's komerční/prodej slice reached sreality's end with 5,009 of
our 5,087 active rows and nominated the 78 it did not see; the worker's drain
fetched the ad at 23:02, sreality answered 404, the drain flipped it. In the same
minutes the same worker completed 1,732 ingest fetches, so the transport was fine:
854 of 855 nominated pages read gone because sreality itself had them down. At
08:17 the ad was back on both the API and the index. Over the ledger's week
(2026-10-02..09) 35% of sreality's 18,105 gone verdicts were active again already,
and a live re-probe of ads still inactive put 62% of the 23:00 batch and 20% of a
32-hour-old batch back on the portal. Night batches are the worst: sreality takes
an ad down — index AND detail — for hours around its nightly expiry/renewal and
brings it back under the same id. Other portals: idnes 5.9%, bezrealitky 7.4%,
remax 10.7%, mmreality 15.4%, bazos / ceskereality / realitymix under 2%.

**The rule.** A gone verdict is recorded, never acted on alone. The drain's gone
branch reads `db.gone_evidence(source, native_id)`: (row exists, earliest gone
verdict newer than `last_seen_at`). No such verdict, or one younger than
`delist_policy.GONE_DWELL` (12 h): the queue row completes with outcome `gone`,
the listing stays active, the next complete walk re-nominates it — the walk
cadence is the retry timer, so there is no column, no timer and no re-enqueue.
An unrefuted verdict the dwell old: `db.mark_listing_inactive`, outcome `flipped`.
The EARLIEST verdict is the anchor so consecutive provisional verdicts cannot
reset the clock; a sighting (touch or detail write bumps `last_seen_at`) refutes
every verdict before it.

**The effective dwell is 24 h, not 12.** A walk re-nominates no ad with a
completion in the last 24 h (`delist_policy.RECHECK_THROTTLE`, read by
`db.presence_candidates`), so a walk-nominated ad's second verdict comes no
earlier than 24 h after its first, at the next end-reached walk of its unit after
that: a true removal closes about 24 h plus one walk after the first verdict (p90
27.8-35.2 h per portal, measured 2026-10-18), not on the fourth walk (~16 h) as
first written here. A VERIFY row queued another way (the daily location refetch,
sreality's freshness check) skips that throttle, so its second verdict can close
an ad 12 h after the first. 24 h becomes the one rule in its own scraper PR
(#1858, ruled 2026-10-18); `effective dwell = max(GONE_DWELL, RECHECK_THROTTLE)`
is what the `delisting` check's limits are built on.

**What to read in the logs.** `DETAIL id=… gone (first verdict recorded, not
flipped; a second one 12h+ later flips)` is the provisional branch; `DETAIL id=…
gone (is_active=false)` the flip; `gone (already inactive)` a confirmed verdict
on a row something else closed. `DRAIN progress` and `RUN done` carry `pending=`
beside `gone=`; `scrape_runs.listings_inactive` (GitHub drains only — the
worker writes no `scrape_runs` row) counts every verdict that is not pending —
flipped, already inactive, no row — so it is not a flip count; the `delisting`
check's outflow arm counts `listings.inactive_at` stamps instead.

**What was deleted.** `sreality_client._is_not_found_body`: the live listing
page carries "tato stránka neexistuje" inside its JavaScript bundle, so the scan
would read ANY HTML answer (a consent wall, an edge error) as a removal; the API
answers a real removal with a JSON 404 (62 of 62 re-probed removals did), and an
HTML body is now a parse error, retried. `freshness._record_gone` no longer flips;
it nominates at `QUEUE_PRIORITY_VERIFY` so the drain's one decider rules.

**Health.** The `delisting` alarm (wave 4) judges the rule per portal: its
precision arm is the share of a day of `flipped` ledger rows from 2 × the portal's
p90 walk gap + 1 h old whose ad is active again (a Wilson lower bound; Watch above
10%, Problem above 15%, n ≥ 50, else Not measured) — the false-flip rate the rule is meant to hold near the blink
tail beyond the dwell — and its stuck arm counts the active ads whose first
unrefuted verdict is older than the effective dwell + 2 × the portal's p90 walk
gap + 1 h; outflow, the verify backlog and the flip cap's deferrals are its other
arms (`references/pipeline-verification.md`). It replaced `false_delist_share` (a
mixed 7-day window). At ship sreality's 10-18 flip cohort reads ~17.5% active
again (Wilson lower bound ~15.5%), so its precision cell may open Problem on
10-20/21: the blink tail #1858 addresses, told, not suppressed.
