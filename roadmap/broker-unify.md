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

- **W2 — failures fail fast and say so** (app-wide): one client deadline
  (`REQUEST_DEADLINE_MS` 130 s, above the server's 120 s budget, under Railway's
  ~300 s close; a timeout is never retried), transient-only retry predicate replacing
  the blanket `retry: 1`, API classifies QueryCanceled/LockNotAvailable/Deadlock as
  503 `db_busy` + Retry-After and answers everything else 500 with a `ref` id (raw
  exception text no longer reaches the browser), the 3 raw `fetch()` bypasses folded
  into `request()` (uploads + blob mode), Brokers gets the shared ErrorBanner + retry
  + honest busy state + placeholder rows captioned by their OWN place label, the
  extension gets the same deadline. 8 of 12 per-query `retry: false` patches deleted
  (audited; BrowseExperience's paid-LLM guard, Shell's documented one, auth's and
  LocationTypeahead's stay). The dedicated short-lock-budget API DB role is
  deliberately deferred (operator decision 2026-09-30) — propose after the sprint.

- **W3 — broker maintenance on the rule-#20 shape** (migration 579): one
  `recompute_brokers()` (single MATERIALIZED base CTE — ONE corpus walk replaces the
  9-window loop's ~27; shared verbatim by the full sweep, the incremental and
  api/broker_review), `run_incremental_pass` as THE incremental driver
  (drain-until-empty + hourly-if-stale matview republish off the registry stamp — no
  new setting, no pg_cron entry; concurrent refresh measured 85 s), a worker
  `broker_maintenance` lane on the maintenance cadence with `broker_resolution.yml`
  as the throttled backstop of the same driver, the property drain mirroring flips
  into `dirty_broker_listings` (delist/revive reaches broker counts in minutes — the
  gap the judge caught), and the sweep reduced to a thin reconcile that publishes
  nothing. Dropped `dirty_broker_listings.sreality_id` (pre-approved; 576-row
  ephemeral-queue snapshot taken before apply). Leaderboard freshness: ~24 h → ≤1 h.

- **W4 — one count book** (migration 580): matview rebuilt with exact GROUPING SETS
  cells — (m,t)/(m,'*')/('*',t)/('*','*') per geo — plus a national `cz` level, so
  every fast-path shape reads a precomputed exact cell and nothing ever sums
  count(DISTINCT) across overlapping cells. The leaderboard's four summing arms
  collapse to ONE exact-cell arm; nested chips count once in SQL (admin_boundaries
  parent suppression — operator ruling); `brokers_public` serves cz_* from the
  ('cz',0,'*','*') cell and the 4 `brokers.cz_*` columns are dropped (one book);
  region_shares/outreach read cells (outreach stops mislabeling listing counts as
  property counts); `broker_identities.agency_name` captures idnes's firm label at
  attribution, ending the daily raw_json TOAST pass (mean 200 s); the domestic
  predicate exists in exactly one place (the matview); leaderboard envelopes carry
  the registry stamp as data_freshness. Measured before merge: default top-100
  shifts for 2 brokers (±4 properties), "Vše" top-100 for 35 (≤4 each) — the fixes
  are semantic (nested chips, one book), not a re-ranking earthquake. The `==17`
  double-count test retired for a count-once pin + a disjoint-additivity pin.

## Next

**Cut, deliberately.** W5 (one lease primitive across property/notification/broker):
unsafe cross-host cutover while a sweep is mid-run, off the north star; the lock-loss
class it targets disappears with W3's <8 min statements. The listing-grain
`broker_listing_facts` rewrite: best data model on paper, but unmeasured query risk on
public read surfaces and a weakened daily backstop — its best parts (W0 stop-gap,
freshness display, drift probe, the flips-never-enqueue-brokers finding) are grafted in.
