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

Only idnes feeds the slice ledger today, so ceskereality and mmreality report
`no slice ledger` rather than a misleading 0% — an absent instrument is a
different statement from a failed walk. Wiring ceskereality's (already
kraj-sliced) walk to `record_index_slice` is what would let the gate evaluate it.

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
