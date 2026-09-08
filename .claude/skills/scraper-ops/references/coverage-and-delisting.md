# Coverage and delisting: the parked flag, the slice ledger, and the gate

Loaded on demand from `scraper-ops`. Everything here is about one question —
**may this portal delete listings?** — and the four layers that now answer it
instead of a boolean somebody typed once.

## The flag is a claim about US, not about the portal

> **2026-09-07:** delisting is now presence-verified (rule #3): a finished walk
> nominates its unseen rows for a page check and the drain's fetch decides. The
> flag below no longer gates anything; the ledger and the gate remain the
> coverage evidence and the posture signal. The history stays because it is
> why the design changed.
>
> **2026-09-08:** what "finished" means changed too. The nomination gate is
> **structural** — `walk_reached_end`, "the walk reached the portal's end" — and
> the count comparison (`walk_coverage`) is a LOGGED ALARM, never a gate. See
> "The structural gate" below; the arithmetic in this file still governs the
> descent/resample triggers and the ledger's `outcome`, which did not change.

`portals.supports_complete_walk` used to gate `mark_inactive` (architectural rule #3):
a portal that could not prove it saw the whole catalogue never delisted from index
absence. It was set true for idnes when the walk *could* in principle be
complete — and then stayed true for months while the walk was reaching 13% of
the biggest category.

That is the failure mode to remember: **the flag does not decay.** Nothing
re-checks it, so it goes on asserting whatever was true the day it was typed.
Two portals are parked on it today for exactly that reason:

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
only sometimes — and the gate needs THREE CONSECUTIVE passes, which a coin flip
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
  are **missing evidence**, and any one of them holds its whole category open
  FOR THE GATE — 14 good slices and one hole is not 93% coverage for
  `supports_complete_walk` purposes.
- **`outcome` stayed NUMERIC on 2026-09-08 and must stay that way**:
  `scripts/coverage_gate.py` counts `outcome='exhausted'` streaks, so re-pointing
  it at the structural verdict would silently re-baseline the gate. The
  structural verdict travels beside it, in code (a field on the slice result) and
  in the `SLICE`/`CATEGORY` log lines, not in the ledger. Consequence to expect:
  a slice can read `outcome='degraded'` (one row short) and `stop=pager_end`
  (reached idnes's last page) at the same time. Both are true. The first decides
  the posture flag; the second decides nomination. **Never read `exhausted` as
  "this nominated".**
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

**mmreality writes one row per category.** Its ten per-(sale type, property type)
indexes page to their tail with no second axis (the largest is ~300 pages of 12), so
each category is one slice, `slice_key='national'`, `exhausted` only when the shared
arithmetic accepts the collected count against the page's own `metadata.count`. A
deadline stop records `deadline`, a page cap `ceiling`, a short or unmeasurable walk
`degraded`. This is what un-parks it: the portal was parked in 2026-05 as "a single
mixed index with no result total", and both halves were wrong (the bare feed was
prodej-only and did declare a count — see `docs/architecture.md` § mmreality).

## The gate (`coverage_gate.yml`, migration 455)

`scripts/coverage_gate.py`, cron `15 3,9,15,21` — three hours after each walk
cycle starts (idnes `15 */6`, ceskereality `25 */6`), so it never reads a
half-written ledger. Reading mid-walk would score a hold: harmless, but it would
reset a streak that had done nothing wrong.

**Only `kind='scraper'` rows are evaluated.** The `portals` registry also holds the
on-demand URL-parser rows (`idnes_reality`, kind='parser': no categories, no walk,
no ledger). One sat `supports_complete_walk=false` and was scored every cycle,
writing a "no slice ledger" hold four times a day for a portal that never walks.
A parser row is not a parked portal.

Two questions, both from data:

**The denominator is CANONICAL categories, not config entries.** ceskereality
declares both `rodinne-domy` and `chaty-chalupy` and both canonicalise to `dum`,
so its 12 config entries can only ever write 10 slice-ledger rows. A gate
demanding 12 could never be satisfied, and was not — it reported "8/12 categories
fully covered" every cycle for a week with no path out. `portal_factory.
canonical_category_count` resolves it through the framework's own
`category_labels` seam rather than restating the mapping, and falls back to the
raw count when it cannot, which is the strict direction: raw >= canonical, so a
fallback can only hold a gate shut, never open one.

1. **Covered** — did every slice of every *declared* category finish inside
   `FRESHNESS_HOURS` (30)? The **declared** count is the denominator, not the
   observed one: a never-walked category has no ledger rows at all, so counting
   only what the ledger holds would let a portal pass by walking a subset
   perfectly — precisely idnes's failure.
2. **Stable** — has that held for `REQUIRED_CONSECUTIVE` (3) evaluations with
   the delist-candidate count within `CANDIDATE_DRIFT_TOLERANCE` (15%) between
   them? One run is luck, two is a coincidence. And a walk that reaches every
   slice but enumerates a different population each time is *sampling* the
   portal, not covering it — invisible in a coverage percentage, which is why
   the candidate count is checked and not just the slice tally.

Both pass → `supports_complete_walk` returns to true. Either fails → it stays
down. Every evaluation appends to `portal_coverage_gate`, holds included: while a
portal is parked the holds are the interesting rows, and a verdict that lives
only in an expiring Actions log is a verdict nobody receives.

**Why this is safe unattended.** Since 2026-09-07 the gate decides nothing that
can delete: it re-earns a posture flag from ledger evidence, and the flag feeds
Health. Closures come from page checks nominated by complete walks, one fetch
each, throttled per walk by `delist_flip_cap`.

**ceskereality now feeds the ledger too.** It was parked in migration 449 with no
way back: the gate un-parks on ledger evidence and its walk wrote none, so it
reported `no slice ledger` every cycle, forever. Its walk was already kraj-sliced,
so wiring it was one call — but with one non-obvious rule.

**ONE LEDGER ROW PER KRAJ, not per subtype.** A kraj past the 99-page ceiling is
re-walked per subtype and comes back as several `SliceResult`s sharing one kraj.
Recording those individually would poison the ledger the first time a kraj stopped
needing the descent: the `kraj/subtype-*` rows from the old shape would linger,
never be re-walked, and age forever — and the gate reads "every slice of this
category exhausted inside the window", so one permanently stale row holds the
portal parked for good. Collapsing to the kraj keeps the key set stable. A kraj
counts as exhausted only when every one of its parts did; a kraj the deadline
never reached is not written at all, so it keeps its old timestamp and sorts first
next run.

mmreality still reports `no slice ledger` — an absent instrument is a different
statement from a failed walk, and reading them alike makes an uninstrumented
portal look broken.

```bash
gh workflow run coverage_gate.yml -f dry_run=true      # decide, write nothing
gh workflow run coverage_gate.yml -f source=idnes
```

```sql
select evaluated_at, source, verdict, covered, categories_ok, categories,
       slices_ok, slices_total, candidates, consecutive, note
  from portal_coverage_gate order by evaluated_at desc limit 20;
```

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
walk bumps `last_seen_at` for every unchanged row (`touch_listings` for sreality,
`touch_listings_by_id` for the other eight portals). Twice it has died AFTER every
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
multi-minute transaction over a hot table — is the recompute's to fix (commit in
batches); the retry only stops it costing a category.

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
`VERIFY cm=… ct=… subtype=… candidates=… queued=… deferred=… active=…`. Two
lines changed shape:

- `COVERAGE cm=… ct=…: the walk reached the portal's end but collected N of M
  (coverage=…) -- the gap is nominated, the page decides (rule #3)` — a WARNING
  emitted when a walk nominates while `walk_coverage != "complete"`. This is the
  alarm that replaced the veto. **Do not silence it**: on a portal that could
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
entry now carries `walk_reached_end` (bool — did it nominate?) and
`walk_coverage` (`complete` / `incomplete` / `unknown`) beside the existing
`sreality_result_size`, `collected` and `active_db`. A category with
`walk_reached_end: true, walk_coverage: "incomplete"` is the new normal case the
old gate refused. `scripts/verify_pipeline.py`'s `walk_coverage` check reads the
same rows and is now the standing rail on the count.

## The four layers, in order (as rebuilt 2026-09-07, regated 2026-09-08)

1. **Coverage** — the sliced walk reaches everything (or records that it didn't).
   A walk that reached the portal's end nominates; the count comparison is a
   logged alarm, never a gate.
2. **The ledger** — coverage accumulates across runs instead of restarting.
3. **Nomination, not deletion** — a finished walk queues the rows it did not
   see for a page check; the drain fetches each page and only a positive gone
   signal flips it. A wrong nomination costs one fetch, never a live listing.
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
   checked within a day are not re-nominated; given-up rows are re-armed 50
   per walk. The drain reserves 20% of each claim for checks so they can never
   starve behind refresh inflow, and Health reads the ingest queue through a
   view that excludes them (migration 483).

The gate still runs and re-earns `supports_complete_walk` from the ledger, but
as a posture signal for Health: nothing reads it to decide a deletion any more.
The retired pieces — the staleness rail, the national cross-check, the latching
refusal — all existed to make absence safe; presence does not need them.
