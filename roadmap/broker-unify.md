# Broker Unify sprint (2026-09-30)

Root cause and full plan: the 2026-09-30 Brokers-page outage investigation (9-agent
deep-dive; operator-approved the same day, all four decisions taken: full W0–W4,
destructive steps pre-approved dump-first, count-once semantics, API DB role deferred).

**North star.** Broker read models are built and published the way every other platform
read model is — recomputed continuously from the dirty-set queue (rule #20's shape),
published without ever blocking a reader, counted by one definition. No reader ever
waits on a writer, and every broker number is exact and at most one cadence old.

**The incident.** The daily full broker sweep refreshed `broker_region_type_stats` with
the plain (ACCESS EXCLUSIVE) REFRESH — every reader blocked for the whole rebuild
(352 s that day), cancelled at the 120 s statement_timeout, silently retried by the SPA
(no client deadline), an eternal "Načítám žebříček…". GH cron drift had moved the sweep
from 04:35 UTC into working hours, and its runtime had grown 51→174 min (I/O contention;
the 9-window rollup walks the full corpus index ~27×). Underneath: migration 508 lost
414's covering index (56 ms → 3–5 s fast arm), two false "CONCURRENTLY can't run in a
txn" comments, non-additive summed distinct counts (556 brokers inflated), a kraj+okres
double count enshrined by a test, and two disagreeing CZ-count books (19/100 top brokers).

## Done

- **W0 — stop the bleed** (PR #1656, merged 2026-09-30): the sweep's refresh is
  CONCURRENTLY, attempts=1, stamped with rows + duration. Ends the daily reader outage.
  Runtime proof owed from the next scheduled sweep's postgres logs.
- **W1 — one refresh chokepoint** (migration 578): `public.refresh_matview(name)` is the
  ONE publication path — refuses unregistered names, CONCURRENTLY with first-populate
  fallback, in-flight skip, stamps rows+duration in-DB. All five Python refresh sites
  converge on `scraper.db.refresh_matview`; the Python stamp wrapper is deleted;
  `rent_map_choropleth` stops blocking readers inside an API request. 508's lost
  covering index restored (rank_idx now covering). New rails:
  `test_matview_refresh_convention.py` (no bypass, no plain refresh after 578),
  `test_matview_indexes.py` (every matview CONCURRENTLY-refreshable + exact broker
  index set), contract test now tracks the live leaderboard definition dynamically.

## Next

- **W2 — failures fail fast and say so** (app-wide): one client deadline in
  `request()` (above the server's 120 s budget, under Railway's 300 s close),
  transient-only retry predicate, API classifies QueryCanceled/LockNotAvailable as 503
  `db_busy` + Retry-After, fold the 3 raw `fetch()` bypasses into `request()`, shared
  error state + retry affordance on Brokers, same deadline in the extension. The
  dedicated short-lock-budget API DB role is deliberately deferred (operator decision
  2026-09-30) — propose after the sprint.
- **W3 — broker maintenance on the rule-#20 shape**: one `recompute_brokers()` (single
  MATERIALIZED base CTE, one corpus walk replacing the 9-window loop, acceptance gate
  <8 min measured before merge), worker-lane drain of `dirty_broker_listings` (~2 min
  cadence), `broker_resolution.yml` stays as the throttled backstop, the daily sweep
  becomes a thin reconcile, matview refresh moves to its own frequent cadence (host
  chosen from W1's stamped `last_duration_ms`; worker lane if >~3 min — pg_cron is
  measured congested). Adds the daily drift probe (stored vs recomputed counts).
  Destructive (pre-approved, dump first): drop `dirty_broker_listings.sreality_id`.
- **W4 — one count book**: matview rebuilt with exact grouping-set cells incl. national
  `cz` level, nested chips count once (operator-confirmed), `brokers_public` reads the
  cz cell, region_shares/outreach read cells directly, firm display names captured at
  attribution (ends the daily idnes raw_json TOAST pass), retire the `==17`
  double-count test. Destructive (pre-approved, dump first): DROP+CREATE of the
  matview; drop the 4 `brokers.cz_*` columns.

**Cut, deliberately.** W5 (one lease primitive across property/notification/broker):
unsafe cross-host cutover while a sweep is mid-run, off the north star; the lock-loss
class it targets disappears with W3's <8 min statements. The listing-grain
`broker_listing_facts` rewrite: best data model on paper, but unmeasured query risk on
public read surfaces and a weakened daily backstop — its best parts (W0 stop-gap,
freshness display, drift probe, the flips-never-enqueue-brokers finding) are grafted in.
