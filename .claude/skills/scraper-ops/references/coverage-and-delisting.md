# Coverage and delisting: the parked flag, the slice ledger, and the gate

Loaded on demand from `scraper-ops`. Everything here is about one question —
**may this portal delete listings?** — and the four layers that now answer it
instead of a boolean somebody typed once.

## The flag is a claim about US, not about the portal

`portals.supports_complete_walk` gates `mark_inactive` (architectural rule #3):
a portal that cannot prove it saw the whole catalogue never delists from index
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
  are **missing evidence**, and any one of them holds its whole category open —
  14 good slices and one hole is not 93% coverage for delisting purposes.
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

## The gate (`coverage_gate.yml`, migration 455)

`scripts/coverage_gate.py`, cron `15 3,9,15,21` — three hours after each walk
cycle starts (idnes `15 */6`, ceskereality `25 */6`), so it never reads a
half-written ledger. Reading mid-walk would score a hold: harmless, but it would
reset a streak that had done nothing wrong.

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

**Why this is safe unattended.** Not because the gate is certain to be right —
because a wrong verdict cannot execute. Un-parking only makes a sweep *eligible*;
the flip cap (migrations 451/452) still refuses any sweep over 10% of a category,
latches, and records the refusal in `delist_flip_refusals`. idnes's backlog is
~37% of its rows, so the single failure this gate could plausibly cause is the
exact one the layer beneath it is built to catch. **Right-or-caught, not right.**

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

## The four layers, in order

1. **Coverage** — the sliced walk reaches everything (or records that it didn't).
2. **The ledger** — coverage accumulates across runs instead of restarting.
3. **The gate** — the flag is re-earned from that evidence, on a schedule.
4. **The flip cap** — and if all three are wrong, no sweep over 10% of a
   category executes anyway; it latches and alarms.

Each layer assumes the one above it can fail. That is the whole design.
