"""Broker identity resolver — the decoupled job behind broker intelligence (phase 1).

Mirrors scripts.recompute_property_stats: a pure-SQL, set-based, idempotent driver
that runs OFF the scrape hot path. Three modes:

  * --incremental (cron */10): drain the dirty_broker_listings queue (new +
    content-changed listings, enqueued at write time by the detail writers —
    db.write_detail_batch for sreality, db.ingest_scraped_listing for idnes —
    rule #20), re-attribute exactly those, then recompute only the affected
    brokers' rollups + firm memberships. O(changes); never touches the leaderboard
    matview. There is deliberately NO full-table straggler scan here: broker_
    identity_id IS NULL is a permanent state for the ~110k listings that carry no
    broker block (index-only stubs, FSBO, other portals), so scanning for it every
    run cost a full raw_json detoast pass for ~7 genuine stragglers and timed out.
    Anything the queue misses is reconciled by the daily full sweep below.
  * full (default, daily reconcile): re-attribute EVERY broker-bearing listing
    (batched by id), recompute all rollups, rebuild memberships + firm counts, run
    the auto-merge step, REFRESH the leaderboard matview, clear the queue.
    The self-healing backstop. Attribution resumes from `broker_sweep_cursor` and
    rotates, so a run truncated by --max-seconds advances through the corpus instead
    of re-walking the same head; only a walk that covered EVERY id in one run clears
    the whole queue, and closing a rotation LAP (cumulative coverage across however
    many runs it took) stamps `broker_resolution_last_complete` — the stamp
    verify_pipeline's broker_resolution_freshness check ages.
  * --backfill: alias for full (the one-shot first population from existing
    raw_json). Run in Actions after merge — local has no psycopg, and a raw_json
    scan over the pooler times out, so it is keyset-batched here.

Attribution is the only source-specific step, and it is CONFIG, not code: one row
per portal in toolkit/broker_sources.py (JSON block path, id/name/email/phone keys,
three quirks) generates all four statement shapes. Everything downstream (firms,
singletons, rollups, grouping, merges) is source-agnostic, so onboarding a portal
is that row plus nothing here (rule #21). scraper/db.py derives its dirty-queue
allowlist from the same registry, so an onboarding can no longer half-land.

Ranking is CZ-scoped (migration 396): `brokers.cz_*` counts only listings that
resolved to a Czech obec, so the two idnes syndication feeds advertising ~26k
Spanish/Croatian properties stay fully attributed but stop heading the leaderboard.
Nothing is filtered out of the corpus — `_DOMESTIC` is a rollup predicate.

Identity keystone (toolkit.broker_resolver, rewritten 2026-08-20): merges are
NAME-GATED and portal-agnostic — two identities unify when their name keys match
and either they share a DISCRIMINATING contact (one whose carriers corpus-wide all
carry that one name) or they share a firm whose name appears at no other firm.
Within-portal duplicates merge like any other pair. `broker_auto_merge_enabled`
(app_settings, absent = on) is the kill switch for the whole step.

Required env: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable, Set as AbstractSet
from typing import Any, TypeVar

from scraper import db
from toolkit import broker_resolver as R
from toolkit.broker_sources import BROKER_SOURCE_NAMES, attribution_statements

LOG = logging.getLogger("resolve_brokers")

_T = TypeVar("_T")

_FREE_KEY = "broker_free_email_domains"
_FRANCHISE_KEY = "broker_franchise_domains"
# The kill switch for the whole auto-merge step. ABSENT MEANS ON: the engine is the
# designed behaviour, and a row that has to exist for it to run is a row someone
# deletes. Only an explicit `false` stops it (the old per-source allowlist
# `broker_auto_merge_sources` is gone — the engine is portal-agnostic).
_AUTO_MERGE_ENABLED_KEY = "broker_auto_merge_enabled"

# Sources whose listings carry a broker block, and the SQL that attributes them —
# both derived from the ONE registry (toolkit.broker_sources), which scraper.db
# also reads for the dirty-queue allowlist. Onboarding a portal is a config row
# there; nothing in this file changes.
_BROKER_SOURCES = BROKER_SOURCE_NAMES
_ATTRIBUTION_SQL = attribution_statements()

# Domestic = the listing resolved to a Czech obec. The admin hierarchy is derived
# from `geom` by a BEFORE trigger, so a foreign pin lands outside every CZ boundary
# and all three of region/okres/obec stay NULL (verified: 0 of the 38,197
# obec-less attributed listings carry a region_id). Matches the split
# docs/design/media-integrity-architecture.md §Q4 already committed the platform
# to, and the guard broker_region_type_stats applies at refresh time — one
# definition of "foreign", not three.
_DOMESTIC = "l.obec_id IS NOT NULL"

# --- Firm resolution (global; %(free)s / %(franchise)s are text[] params). ---

_FIRMS_UPSERT = """
INSERT INTO firms (canonical_domain, is_franchise, first_seen_at, last_seen_at)
SELECT DISTINCT bi.email_domain, (bi.email_domain = ANY(%(franchise)s)), now(), now()
FROM broker_identities bi
WHERE bi.email_domain IS NOT NULL AND NOT (bi.email_domain = ANY(%(free)s))
ON CONFLICT (canonical_domain) DO UPDATE SET
  is_franchise = EXCLUDED.is_franchise, last_seen_at = now()
"""

_FIRM_IDENTITIES_UPSERT = """
INSERT INTO firm_identities (source, source_firm_native, firm_id, email_domain, first_seen_at, last_seen_at)
SELECT DISTINCT bi.source, bi.email_domain, f.id, bi.email_domain, now(), now()
FROM broker_identities bi
JOIN firms f ON f.canonical_domain = bi.email_domain
WHERE bi.email_domain IS NOT NULL AND NOT (bi.email_domain = ANY(%(free)s))
ON CONFLICT (source, source_firm_native) DO UPDATE SET
  firm_id = EXCLUDED.firm_id, last_seen_at = now()
"""

_LINK_IDENTITY_FIRM = """
UPDATE broker_identities bi SET firm_identity_id = fi.id
FROM firm_identities fi
WHERE fi.source = bi.source AND fi.source_firm_native = bi.email_domain
  AND bi.email_domain IS NOT NULL AND bi.firm_identity_id IS DISTINCT FROM fi.id
"""

# Deterministic LOCK ORDER, not just a deterministic result. The plain
# `UPDATE listings ... FROM broker_identities JOIN firm_identities` form let the
# planner lock listings rows in whatever order the join emitted them (identity
# order, i.e. effectively broker-clustered) while the detail drain's batch upsert
# locks the same rows in fetch-completion order — two writers walking one table in
# unrelated orders, which is the textbook deadlock recipe (and what the CI census
# saw twice in 30 days). The MATERIALIZED CTE acquires every `listings` row lock in
# ascending id order (PG applies ORDER BY before the locking clause and puts
# LockRows above the Sort; the outer UPDATE then only re-touches rows this
# transaction already holds, so it can neither block nor lock out of order).
#
# PARTIAL and one-sided, deliberately: the drain still locks in fetch-completion
# order, so a cycle between the two writers remains POSSIBLE — one ordered side
# cannot break a cycle, both would have to share the order, and they key on
# different columns (drain: source_id_native; here: the surrogate listings.id).
# What covers the residual risk is recovery, not prevention: DeadlockDetected is an
# OperationalError, so db.run_resilient retries the victim on BOTH sides (this
# step, and portal_runner's "drain.write"). Ordering this side just makes the
# collision rarer and the retry cheaper.
_LINK_LISTINGS_FIRM = """
WITH targets AS MATERIALIZED (
  SELECT l.id AS listing_id, fi.firm_id
  FROM listings l
  JOIN broker_identities bi ON bi.id = l.broker_identity_id
  JOIN firm_identities fi ON fi.id = bi.firm_identity_id
  WHERE l.broker_firm_id IS DISTINCT FROM fi.firm_id {extra}
  ORDER BY l.id
  FOR UPDATE OF l
)
UPDATE listings l SET broker_firm_id = t.firm_id
FROM targets t WHERE l.id = t.listing_id
"""

# Robust set-based singleton attach: RETURNING carries the seed identity id, so new
# brokers link back without depending on RETURNING row order.
_SINGLETON_ATTACH = """
WITH ins AS (
  INSERT INTO brokers (seed_identity_id, display_name, primary_email, first_seen_at, last_seen_at)
  SELECT bi.id, bi.display_name, bi.email, bi.first_seen_at, bi.last_seen_at
  FROM broker_identities bi WHERE bi.broker_id IS NULL
  RETURNING id, seed_identity_id
)
UPDATE broker_identities bi SET broker_id = ins.id
FROM ins WHERE ins.seed_identity_id = bi.id
"""

_IDENTITY_ROLLUP = """
UPDATE broker_identities bi SET
  listing_count = c.n, active_listing_count = c.live, attrs_computed_at = now()
FROM (
  SELECT broker_identity_id AS id, count(*) AS n,
         count(*) FILTER (WHERE is_active AND last_seen_at > now() - interval '7 days') AS live
  FROM listings WHERE broker_identity_id IS NOT NULL {extra}
  GROUP BY broker_identity_id
) c
WHERE c.id = bi.id
"""

_BROKER_ROLLUP = """
WITH ident AS (
  SELECT broker_id, source, display_name, email, last_seen_at
  FROM broker_identities WHERE broker_id IS NOT NULL {bscope}
),
ident_latest AS (
  SELECT DISTINCT ON (broker_id) broker_id, display_name, email
  FROM ident ORDER BY broker_id, last_seen_at DESC NULLS LAST
),
ident_agg AS (
  SELECT broker_id, count(*) AS sc, count(DISTINCT source) AS dsc FROM ident GROUP BY broker_id
),
lst AS (
  SELECT bi.broker_id,
    count(*) AS lc,
    count(DISTINCT coalesce(l.property_id, -l.id)) AS pc,
    count(*) FILTER (WHERE l.is_active AND l.last_seen_at > now() - interval '7 days') AS alc,
    count(DISTINCT coalesce(l.property_id, -l.id))
      FILTER (WHERE l.is_active AND l.last_seen_at > now() - interval '7 days') AS apc,
    count(*) FILTER (WHERE {domestic}) AS cz_lc,
    count(DISTINCT coalesce(l.property_id, -l.id)) FILTER (WHERE {domestic}) AS cz_pc,
    count(*) FILTER (WHERE {domestic}
      AND l.is_active AND l.last_seen_at > now() - interval '7 days') AS cz_alc,
    count(DISTINCT coalesce(l.property_id, -l.id)) FILTER (WHERE {domestic}
      AND l.is_active AND l.last_seen_at > now() - interval '7 days') AS cz_apc,
    min(l.first_seen_at) AS fseen, max(l.last_seen_at) AS lseen
  FROM listings l JOIN broker_identities bi ON bi.id = l.broker_identity_id
  WHERE bi.broker_id IS NOT NULL {bscope}
  GROUP BY bi.broker_id
),
pfirm AS (
  SELECT DISTINCT ON (bi.broker_id) bi.broker_id, l.broker_firm_id AS firm_id
  FROM listings l JOIN broker_identities bi ON bi.id = l.broker_identity_id
  WHERE bi.broker_id IS NOT NULL AND l.broker_firm_id IS NOT NULL {bscope}
  ORDER BY bi.broker_id, l.last_seen_at DESC NULLS LAST
),
pphone AS (
  SELECT DISTINCT ON (bi.broker_id) bi.broker_id, ct.value
  FROM broker_identity_contacts ct JOIN broker_identities bi ON bi.id = ct.broker_identity_id
  WHERE ct.kind = 'phone' AND bi.broker_id IS NOT NULL {bscope}
  ORDER BY bi.broker_id, ct.last_seen_at DESC NULLS LAST
)
UPDATE brokers b SET
  display_name = il.display_name,
  primary_email = il.email,
  primary_phone = pp.value,
  primary_firm_id = pf.firm_id,
  source_count = ia.sc,
  distinct_source_count = ia.dsc,
  listing_count = coalesce(ls.lc, 0),
  property_count = coalesce(ls.pc, 0),
  active_listing_count = coalesce(ls.alc, 0),
  active_property_count = coalesce(ls.apc, 0),
  cz_listing_count = coalesce(ls.cz_lc, 0),
  cz_property_count = coalesce(ls.cz_pc, 0),
  cz_active_listing_count = coalesce(ls.cz_alc, 0),
  cz_active_property_count = coalesce(ls.cz_apc, 0),
  first_seen_at = coalesce(ls.fseen, b.first_seen_at),
  last_seen_at = coalesce(ls.lseen, b.last_seen_at),
  stats_computed_at = now()
FROM ident_latest il
JOIN ident_agg ia USING (broker_id)
LEFT JOIN lst ls ON ls.broker_id = il.broker_id
LEFT JOIN pfirm pf ON pf.broker_id = il.broker_id
LEFT JOIN pphone pp ON pp.broker_id = il.broker_id
WHERE b.id = il.broker_id AND b.status = 'active'
"""

# Bound once, here, so every caller — including api.broker_review's per-broker
# recompute after a manual merge/unmerge — writes the cz_* columns from the SAME
# predicate without threading it through .format(). {bscope} stays open.
_BROKER_ROLLUP = _BROKER_ROLLUP.replace("{domestic}", _DOMESTIC)

_MEMBERSHIP_RECOMPUTE = """
WITH agg AS (
  SELECT bi.broker_id, l.broker_firm_id AS firm_id,
         min(l.first_seen_at) AS fseen, max(l.last_seen_at) AS lseen, count(*) AS lc
  FROM listings l JOIN broker_identities bi ON bi.id = l.broker_identity_id
  WHERE bi.broker_id IS NOT NULL AND l.broker_firm_id IS NOT NULL {bscope}
  GROUP BY bi.broker_id, l.broker_firm_id
),
up AS (
  INSERT INTO broker_firm_memberships (broker_id, firm_id, first_seen_at, last_seen_at, listing_count)
  SELECT broker_id, firm_id, fseen, lseen, lc FROM agg
  ON CONFLICT (broker_id, firm_id) DO UPDATE SET
    first_seen_at = least(broker_firm_memberships.first_seen_at, EXCLUDED.first_seen_at),
    last_seen_at = greatest(broker_firm_memberships.last_seen_at, EXCLUDED.last_seen_at),
    listing_count = EXCLUDED.listing_count
  RETURNING 1
)
DELETE FROM broker_firm_memberships m
WHERE {mscope} NOT EXISTS (SELECT 1 FROM agg a WHERE a.broker_id = m.broker_id AND a.firm_id = m.firm_id)
"""

_FIRM_ROLLUP = """
WITH mc AS (
  SELECT firm_id, count(DISTINCT broker_id) AS bc FROM broker_firm_memberships GROUP BY firm_id
),
lc AS (
  SELECT broker_firm_id AS firm_id, count(*) AS n,
         count(*) FILTER (WHERE is_active AND last_seen_at > now() - interval '7 days') AS live
  FROM listings WHERE broker_firm_id IS NOT NULL GROUP BY broker_firm_id
)
UPDATE firms f SET
  broker_count = coalesce(mc.bc, 0),
  listing_count = coalesce(lc.n, 0),
  active_listing_count = coalesce(lc.live, 0),
  stats_computed_at = now()
FROM (SELECT id FROM firms) ff
LEFT JOIN mc ON mc.firm_id = ff.id
LEFT JOIN lc ON lc.firm_id = ff.id
WHERE f.id = ff.id
"""

# Friendly firm names from idnes agency labels (sreality's raw_json.user has no
# agency field). Only the DOMINANT label of a non-franchise domain — single-firm
# domains carry one clean 100%-modal name (prexima.cz -> "PREXIMA nemovitosti
# s.r.o."); franchise/aggregator domains (re-max.cz: 95 offices, century21.cz)
# have no dominant label, so they stay NULL and the UI falls back to the domain
# rather than mislabel the brand as one office.
_FIRM_DISPLAY_NAMES = """
WITH agency AS (
  SELECT bi.email_domain AS domain,
         l.raw_json->'broker'->>'agency_name' AS name,
         count(*) AS n
  FROM listings l
  JOIN broker_identities bi ON bi.id = l.broker_identity_id
  WHERE l.source = 'idnes' AND bi.email_domain IS NOT NULL
    AND coalesce(l.raw_json->'broker'->>'agency_name', '') <> ''
  GROUP BY bi.email_domain, l.raw_json->'broker'->>'agency_name'
),
ranked AS (
  SELECT domain, name, n,
         row_number() OVER (PARTITION BY domain ORDER BY n DESC, name) AS rk,
         sum(n) OVER (PARTITION BY domain) AS total
  FROM agency
)
UPDATE firms f SET display_name = r.name
FROM ranked r
WHERE r.rk = 1 AND r.n::numeric / r.total >= 0.60
  AND f.canonical_domain = r.domain
  AND NOT f.is_franchise
  AND f.display_name IS DISTINCT FROM r.name
"""

# The auto-merge engine's two inputs, both deliberately UNFILTERED. Its maps are
# corpus-wide statements — which names a contact belongs to, how many firms a name
# appears at — so pre-filtering the rows silently changes the verdict instead of
# just shrinking the work. The old frequency/personal/multi-source CTEs are gone
# with the guard they served (a duplicated broker's own e-mail read as shared).
#
# firm_id: the firm behind the IDENTITY's own e-mail domain — deliberately not
# coalesced with brokers.primary_firm_id. That column is not curated: _BROKER_ROLLUP
# derives it DISTINCT ON (broker_id) ORDER BY last_seen_at, i.e. the firm of the
# broker's most recent listing. Fed to path B it made the rarity test self-weakening
# (merge two identities of one name at two firms and next sweep both report one
# firm, so the name's spread drops to 1 and a third, unrelated record of that name
# gets a free B-edge) and non-deterministic day to day. is_franchise is NOT read:
# path B's franchise safety lives in the contradiction veto (two same-named agents
# at two offices carry disagreeing personal contacts), not in a flag exclusion.
# primary_firm_id rides along for ONE purpose: decide_merges' double-card check,
# which must compare exactly what _CANDIDATE_BROKERS groups on. mergeable=false for
# an identity whose broker is already merged away — it still counts as evidence
# about a contact's owner, but never gets an edge (its broker is not a merge target).
_MERGE_IDENTITIES_SQL = """
SELECT bi.id, bi.source, bi.display_name,
       fi.firm_id AS firm_id,
       (b.status IS DISTINCT FROM 'merged_away') AS mergeable,
       b.primary_firm_id
FROM broker_identities bi
LEFT JOIN brokers b ON b.id = bi.broker_id
LEFT JOIN firm_identities fi ON fi.id = bi.firm_identity_id
"""

_MERGE_CONTACTS_SQL = """
SELECT broker_identity_id, kind, value FROM broker_identity_contacts
"""

# The operator's standing NO (migration 401). One indexed read per sweep: the merge
# step already runs ~8.4 min against a 20-min lock-stale window, so the rail is a
# single set load, never a per-candidate lookup.
_SUPPRESSED_PAIRS_SQL = """
SELECT identity_lo, identity_hi FROM broker_merge_suppressions WHERE lifted_at IS NULL
"""

# Claim/delete by listing_id: it is this queue's PRIMARY KEY since the R2 Phase D
# swap, and the only column guaranteed non-NULL post-Gate-2 — a NULL sreality_id
# would make the claim return NULL and the DELETE match nothing, silently stopping
# broker attribution for new rows while leaking undeletable queue rows.
_CLAIM_DIRTY = "SELECT listing_id FROM dirty_broker_listings WHERE marked_at <= %(cutoff)s ORDER BY marked_at LIMIT %(limit)s"
_DELETE_DIRTY = "DELETE FROM dirty_broker_listings WHERE listing_id = ANY(%(ids)s) AND marked_at <= %(cutoff)s"
_CLEAR_DIRTY = "DELETE FROM dirty_broker_listings WHERE marked_at <= %(cutoff)s"

# The budget-exhausted variants (recompute_property_stats._CLEAR_DIRTY_SWEPT_SQL's
# analogue). A sweep that breaks out on --max-seconds re-attributed only the ids in
# the window it actually walked, so the GLOBAL clear above erased the queue's signal
# for every id it never reached — and, before the rotation cursor, it never reached
# the same tail again on any subsequent day. Scope the delete to the swept window;
# it is contiguous in id order but WRAPS when the rotation crossed the end of the
# corpus, hence the second form.
_CLEAR_DIRTY_SWEPT_SQL = (
    "DELETE FROM dirty_broker_listings WHERE marked_at <= %(cutoff)s "
    "AND listing_id >= %(lo)s AND listing_id <= %(hi)s"
)
_CLEAR_DIRTY_SWEPT_WRAPPED_SQL = (
    "DELETE FROM dirty_broker_listings WHERE marked_at <= %(cutoff)s "
    "AND (listing_id >= %(lo)s OR listing_id <= %(hi)s)"
)

# Full-sweep rotation cursor: the id attribution last stopped at, plus the open
# LAP's accounting. The sweep walks broker-bearing ids ASCENDING and breaks on
# --max-seconds, so without a cursor it restarted at the minimum id every day and
# the tail above the break — the NEWEST listings — was never attributed by any
# sweep, ever, while the job still exited 0.
_SWEEP_CURSOR_KEY = "broker_sweep_cursor"
# Stamped when the ROTATION closes a lap — cumulative ids swept since the lap
# opened reach the corpus size — NOT when one run walks everything. Truncation is
# the designed steady state here (the 2026-08-10 sweep attributed 480,000 of
# 535,007 ids inside its 3000s budget), so a single-run condition would essentially
# never hold: the stamp would never land and the freshness check would park forever
# on the amber a missing stamp gives it, while the one light day that did fit would
# then age past the fail threshold with the rotation working exactly as designed.
# Read by verify_pipeline's `broker_resolution_freshness` — rename in both places
# or not at all.
_SWEEP_COMPLETE_KEY = "broker_resolution_last_complete"

_READ_SETTING_SQL = "SELECT value FROM app_settings WHERE key = %s"

# lap_started_at falls back to now() so the very first sweep opens a lap: the check
# ages the sweep axis from it until a lap actually closes, which is what makes a
# rotation that never closes one reachable as `fail` instead of a permanent warn.
_WRITE_SWEEP_CURSOR_SQL = """
INSERT INTO app_settings (key, value, updated_by)
VALUES (%(key)s,
        jsonb_build_object('last_id', %(last_id)s::bigint,
                           'lap_swept', %(lap_swept)s::int,
                           'lap_started_at',
                           coalesce(%(lap_started_at)s::timestamptz, now()),
                           'updated_at', now()),
        'resolve_brokers')
ON CONFLICT (key) DO UPDATE
  SET value = excluded.value, updated_at = now(), updated_by = excluded.updated_by
"""

_STAMP_SWEEP_COMPLETE_SQL = """
INSERT INTO app_settings (key, value, updated_by)
VALUES (%(key)s,
        jsonb_build_object('completed_at', now(),
                           'listings_swept', %(swept)s::int,
                           'elapsed_s', %(elapsed_s)s::numeric),
        'resolve_brokers')
ON CONFLICT (key) DO UPDATE
  SET value = excluded.value, updated_at = now(), updated_by = excluded.updated_by
"""

# A proposal whose brokers were merged by another route (the operator resolving an
# overlapping pair, or the auto-merge step) can never be acted on: merge_brokers
# raises "fewer than two of the given brokers are active" and the row occupies the
# queue forever, because the generators only ever touch keys they re-propose.
_RETIRE_DEAD_CANDIDATES_SQL = """
UPDATE broker_merge_candidates c
   SET status = 'merged', resolved_at = now(), resolved_by = %(by)s
 WHERE c.status = 'proposed'
   AND (SELECT count(*) FROM brokers b
         WHERE b.id = ANY(c.broker_ids) AND b.status = 'active') < 2
"""

# Who retired a dead card: the auto-merge step's own hygiene pass vs the sweep's
# end-of-run backstop. Same statement, distinguishable in the ledger.
_SWEEP_RETIRE_ACTOR = "resolve_brokers"
_AUTO_MERGE_RETIRE_ACTOR = "auto:sweep"

# broker_merge_events.reason when a component carries no recorded evidence path —
# only reachable from a caller that passes no group_reasons (the column is free
# text, and inventing 'contact_name' there would claim evidence that never existed).
_MERGE_REASON_FALLBACK = "auto_merge"


def _settings(conn: Any) -> tuple[list[str], list[str], bool]:
    """Free/franchise domain lists plus the auto-merge kill switch (absent = ON)."""
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM app_settings WHERE key = ANY(%s)",
                    ([_FREE_KEY, _FRANCHISE_KEY, _AUTO_MERGE_ENABLED_KEY],))
        rows = {k: v for k, v in cur.fetchall()}
    free = [str(d).lower() for d in (rows.get(_FREE_KEY) or [])]
    franchise = [str(d).lower() for d in (rows.get(_FRANCHISE_KEY) or [])]
    raw = rows.get(_AUTO_MERGE_ENABLED_KEY, True)
    enabled = not (raw is False or (isinstance(raw, str) and raw.strip().lower() == "false"))
    return free, franchise, enabled


# Pooler-safe mutual exclusion (migration 192). A session pg_advisory_lock is
# unreliable through the transaction-mode pooler, so we claim a single lock row
# instead; the holder heartbeats during a long run and a stale heartbeat lets a
# later run take over after a SIGKILL.
# 10 -> 20 minutes. The TTL must exceed the longest gap between two heartbeats,
# and the beat is the first statement of each RETRIED attempt (see _resilient_step),
# so the real bound is ONE attempt plus run_resilient's backoff. The worst measured
# single attempt is the auto-merge step at 8.4 min (2026-08-09 full sweep,
# 06:24:33 -> 06:32:56) — 84% of the old 10-min window, i.e. one slow day from a
# live holder being declared stale and its lock stolen mid-sweep. 20 min leaves
# ~2.4x margin over that attempt and still sits far under the 110-min job timeout,
# so a genuinely dead holder is still cleared within one incremental tick or two.
_LOCK_STALE_MIN = 20
_LOCK_POLL_SECONDS = 10
# > the stale window, so the full sweep always outwaits a dead holder rather than
# aborting RED with no work done. Raised with _LOCK_STALE_MIN — the two are one
# decision. The wait is anchored at process start and the attribution budget
# (--max-seconds) is anchored there too, so a long wait eats attribution time
# rather than extending the job.
_LOCK_WAIT_MAX_SECONDS = 1260

# Minimum share of the full sweep's wall-clock budget reserved for the firm-link
# loop, which runs AFTER attribution has usually spent all of --max-seconds. Sized
# from the measured phase (43s on 2026-08-10, 186s on 08-09, both over the full
# ~107-chunk corpus) with ~1.6x headroom for corpus growth. Counts on top of
# --max-seconds, so resolve_brokers_full.yml's timeout-minutes covers both.
_FIRM_LINK_MIN_SECONDS = 300


def _try_acquire_lock(conn: Any, holder: str, mode: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE broker_resolution_lock SET holder=%(h)s, mode=%(m)s, acquired_at=now(), "
            "heartbeat_at=now() WHERE id=1 AND (holder IS NULL OR "
            "heartbeat_at < now() - make_interval(mins => %(stale)s))",
            {"h": holder, "m": mode, "stale": _LOCK_STALE_MIN})
        return cur.rowcount == 1


def _acquire_lock_blocking(conn: Any, holder: str, mode: str, deadline_s: float) -> bool:
    while not _try_acquire_lock(conn, holder, mode):
        if time.monotonic() > deadline_s:
            return False
        time.sleep(_LOCK_POLL_SECONDS)
    return True


def _heartbeat_lock(conn: Any, holder: str) -> None:
    """Refresh our own lock row, pushing the staleness window out one TTL.

    Raises when the holder-guarded CAS misses ZERO rows: that means our heartbeat
    went stale and another run took the lock over, so continuing would resolve
    concurrently against a holder that thinks it is alone. Fail loud instead —
    RuntimeError is not an OperationalError, so db.run_resilient re-raises it
    immediately rather than replaying into the race, and _release_lock's own CAS
    guard stops us clearing the new holder's lock on the way out. Mirrors
    recompute_property_stats._renew_lease.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE broker_resolution_lock SET heartbeat_at=now() WHERE id=1 AND holder=%(h)s",
                    {"h": holder})
        if cur.rowcount != 1:
            raise RuntimeError(
                "broker resolution lock lost mid-run (heartbeat went stale and another "
                "run re-claimed it) — aborting rather than resolving concurrently")


def _release_lock_cas(conn: Any, holder: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE broker_resolution_lock SET holder=NULL WHERE id=1 AND holder=%(h)s",
                    {"h": holder})


def _release_lock(conn: Any, holder: str,
                  reconnect: Callable[[], Any] | None = None) -> None:
    """Best-effort release, with ONE reconnect fallback. The caller runs this from a
    `finally:`, so a raise here replaces the real failure with a crash-during-cleanup
    (2026-08-10 23:10: an SSL drop mid-rollup surfaced as `the connection is closed`
    raised by THIS function's cursor open, burying the original error). Correctness
    never depended on the release landing — the CAS guard plus the lock's staleness
    TTL are what make takeover safe.

    The fallback exists because the handle the caller holds can be a DEAD socket even
    when the process still has a healthy path to the DB: the drivers only hand their
    live connection back on a normal return, so a phase that reconnected and then died
    on something non-transient leaves main() releasing on the connection run_resilient
    already closed. One fresh connection, one holder-guarded CAS, close it again — the
    CAS makes the release safe from any connection."""
    exc: BaseException | None = None
    try:
        _release_lock_cas(conn, holder)
        return
    except Exception as e:  # noqa: BLE001 - best-effort; the staleness TTL is the real guarantee
        exc = e
    if reconnect is not None:
        fresh: Any = None
        try:
            fresh = reconnect()
            _release_lock_cas(fresh, holder)
            return
        except Exception as e:  # noqa: BLE001 - still best-effort
            exc = e
        finally:
            if fresh is not None:
                try:
                    fresh.close()
                except Exception:  # noqa: BLE001
                    pass
    # exc_info=exc, not True: we are outside the except block here, so sys.exc_info()
    # would be empty and the traceback lost — which is the whole complaint.
    LOG.warning("RESOLVE: lock release failed (holder=%s) — self-heals via the %d-min "
                "staleness TTL", holder, _LOCK_STALE_MIN, exc_info=exc)


def _attribute(conn: Any, sel: str, params: dict[str, Any]) -> None:
    """Run every registered source's attribution over one listings selector.

    Source selection is a literal inside each statement, so a single-portal chunk
    still issues all of them; the ones that match nothing are bounded by the same
    {sel} id list and cost ~nothing. That is what lets the sweep be ONE ascending
    id rotation over every portal rather than a cursor per source.

    Statement timeout lifted per chunk, exactly as every other full-corpus phase of
    this sweep does: the pooler's 2-min guardrail is an app-query bound, wrong for a
    serialized once-daily reconcile, and one chunk crossing it aborted the whole run
    (2026-08-23, QueryCanceled after both attempts). The chunk loop's --max-seconds
    check still bounds the phase between chunks; the job timeout is the backstop."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 0")
        for sql in _ATTRIBUTION_SQL:
            cur.execute(sql.format(sel=sel), params)


def _resolve_firms(conn: Any, free: list[str], franchise: list[str]) -> None:
    """Upsert firms + firm identities and link identities to firms (once per run)."""
    with conn.cursor() as cur:
        cur.execute(_FIRMS_UPSERT, {"free": free, "franchise": franchise})
        cur.execute(_FIRM_IDENTITIES_UPSERT, {"free": free})
        cur.execute(_LINK_IDENTITY_FIRM)


def _link_listings_firm(conn: Any, extra: str = "", params: dict[str, Any] | None = None) -> None:
    """Point listings at their identity's resolved firm. Bounded by `extra` (an id
    scope) so the full sweep batches it — one global UPDATE over every linked
    listing exceeds the pooler statement timeout once a second source lands."""
    with conn.cursor() as cur:
        cur.execute(_LINK_LISTINGS_FIRM.format(extra=extra), params or {})


def _attach_singletons(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute(_SINGLETON_ATTACH)
        return cur.rowcount or 0


def _suppressed_pairs(conn: Any) -> set[tuple[int, int]]:
    """The identity pairs the operator has rejected and not since overridden."""
    with conn.cursor() as cur:
        cur.execute(_SUPPRESSED_PAIRS_SQL)
        return {(int(lo), int(hi)) for lo, hi in cur.fetchall()}


def _retire_dead_candidates(conn: Any, actor: str) -> int:
    """Close proposals that can no longer be acted on (fewer than 2 active brokers)."""
    with conn.cursor() as cur:
        cur.execute(_RETIRE_DEAD_CANDIDATES_SQL, {"by": actor})
        return cur.rowcount or 0


def _shared_contacts(contacts: list[R.Contact], pairs: list[tuple[int, int]],
                     ) -> dict[tuple[int, int], set[str]]:
    """pair -> the 'kind:value' strings both identities carry (the card's evidence).

    Built only for the pairs actually being written: an all-pairs index over a
    corpus-wide contact table is quadratic in the carriers of every role inbox.
    """
    wanted = {i for pair in pairs for i in pair}
    if not wanted:
        return {}
    by_identity: dict[int, set[str]] = {}
    for c in contacts:
        if c.identity_id in wanted:
            by_identity.setdefault(c.identity_id, set()).add(f"{c.kind}:{c.value}")
    out: dict[tuple[int, int], set[str]] = {}
    for a, b in pairs:
        shared = by_identity.get(a, set()) & by_identity.get(b, set())
        if shared:
            out[(a, b) if a < b else (b, a)] = shared
    return out


def _auto_merge(conn: Any, run_id: int) -> tuple[int, int, int]:
    """Form name-gated broker groups and apply reversible merges (portal-agnostic).

    Loads the whole identity + contact corpus (both maps the rule consults are
    corpus-wide, so neither read may be pre-filtered), decides in
    toolkit.broker_resolver, applies at BROKER grain and persists everything the
    rule could not prove for operator review (_queue_review_pairs). The second
    returned count is the pairs DECIDED, which is what
    broker_resolution_runs.queued_for_review has always meant. The third is what the
    apply-time rails refused this run — suppressed edges, whole components the
    standing-NO backstop dropped, and components too large to fuse.
    """
    # One full-table read of each input, once per daily sweep: lift the pooler's
    # app-query statement timeout, which is the wrong guardrail for it.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 0")
        cur.execute(_MERGE_IDENTITIES_SQL)
        identity_rows = cur.fetchall()
        cur.execute(_MERGE_CONTACTS_SQL)
        contact_rows = cur.fetchall()
    if not identity_rows:
        return 0, 0, 0

    identities: dict[int, R.Identity] = {
        int(iid): R.Identity(int(iid), source, name, _as_int(firm_id), bool(mergeable),
                             _as_int(primary_firm_id))
        for iid, source, name, firm_id, mergeable, primary_firm_id
        in identity_rows
    }
    contacts = [R.Contact(int(iid), kind, value) for iid, kind, value in contact_rows
                if int(iid) in identities]

    blocked = _suppressed_pairs(conn)
    decision = R.decide_merges(list(identities.values()), contacts,
                               suppressed_pairs=blocked)
    auto, dropped, oversized = _apply_merges(conn, decision.auto_merge_groups,
                                             suppressed_pairs=blocked,
                                             group_bridges=decision.group_bridges,
                                             group_reasons=decision.group_reasons)
    # An auto-merged group leaves its old review card sitting at 'proposed' with
    # only one surviving broker behind it — the UI renders it thin and the merge
    # button can only ever answer 409. Retire those the moment the merges land,
    # BEFORE this run proposes anything new.
    retired = _retire_dead_candidates(conn, _AUTO_MERGE_RETIRE_ACTOR)
    # AFTER the merges: they re-point broker_identities.broker_id, so a pair read
    # before them could propose a broker that no longer survives.
    shared = _shared_contacts(contacts, decision.review_pairs + decision.dismiss_pairs)
    proposed = _queue_review_pairs(conn, decision.review_pairs, identities,
                                   shared, run_id)
    dismissed = _dismiss_name_conflicts(conn, decision.dismiss_pairs, identities,
                                        shared, run_id)
    reasons = [decision.group_reasons.get(tuple(g), "") for g in decision.auto_merge_groups]
    LOG.info("RESOLVE auto-merge groups=%d (contact=%d, firm=%d, mixed=%d) identities=%d "
             "queued=%d suppressed=%d",
             len(decision.auto_merge_groups),
             sum(1 for r in reasons if r == R.REASON_CONTACT_NAME),
             sum(1 for r in reasons if r == R.REASON_NAME_FIRM),
             sum(1 for r in reasons if "+" in r),
             sum(len(g) for g in decision.auto_merge_groups),
             len(decision.review_pairs),
             # the same total the run row records; the next line breaks it down
             len(decision.suppressed) + dropped + oversized)
    LOG.info("RESOLVE merge review pairs decided=%d proposed=%d name_conflict_dismissed=%d "
             "suppressed_edges=%d suppressed_components=%d oversized_components=%d "
             "active_suppressions=%d stale_cards_retired=%d",
             len(decision.review_pairs), proposed, dismissed, len(decision.suppressed),
             dropped, oversized, len(blocked), retired)
    return auto, len(decision.review_pairs), len(decision.suppressed) + dropped + oversized


def _broker_of(conn: Any, identity_ids: list[int]) -> dict[int, int]:
    """identity id -> its current brokers.id; identities with none are absent."""
    if not identity_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT id, broker_id FROM broker_identities WHERE id = ANY(%s)",
                    (identity_ids,))
        return {int(i): int(b) for i, b in cur.fetchall() if b is not None}


def _identities_of(conn: Any, broker_ids: list[int]) -> dict[int, list[int]]:
    """brokers.id -> EVERY identity it currently holds, not just the merging ones."""
    if not broker_ids:
        return {}
    out: dict[int, list[int]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, broker_id FROM broker_identities WHERE broker_id = ANY(%s) "
            "ORDER BY id", (broker_ids,))
        for iid, bid in cur.fetchall():
            out.setdefault(int(bid), []).append(int(iid))
    return out


def _broker_components(groups: list[list[int]],
                       broker_of: dict[int, int]) -> list[list[int]]:
    """Union-find the identity components in BROKER space, ascending survivor first.

    decide_merges' components are disjoint over IDENTITIES, not over brokers: an
    already-merged broker holds several identities, and contacts are never deleted,
    so an edge between two of its own identities can lapse (a contact that stops
    discriminating, a display_name change) and leave them in two components. Left as-is
    that broker would be retired into two survivors in one run — `losers` is keyed
    on the retired id, so the ledger logged both while the brokers UPDATE kept
    whichever came last. Merging at broker grain makes the collision unrepresentable
    instead of detecting it after the fact.

    This deliberately widens what ONE run can fuse: decide_merges caps a group at
    MAX_AUTO_MERGE_COMPONENT identities, and two capped groups chained through a
    shared broker now apply as one merge instead of colliding. The fusing edge is a
    merge already recorded (that broker holds both identities), not new evidence, so
    the union is implied rather than invented. Two groups chained this way are also
    the one place where DIFFERENTLY-named groups meet — name_key transitivity makes
    that unreachable inside decide_merges, not here — so every multi-group component
    is logged for the operator, and _apply_merges refuses any component wider than
    the same cap rather than letting the chain run unbounded."""
    parent: dict[int, int] = {}

    def find(b: int) -> int:
        root = b
        while parent[root] != root:
            root = parent[root]
        while parent[b] != root:
            parent[b], b = root, parent[b]
        return root

    for group in groups:
        brokers_in = sorted({broker_of[i] for i in group if i in broker_of})
        for b in brokers_in:
            parent.setdefault(b, b)
        for b in brokers_in[1:]:
            ra, rb = find(brokers_in[0]), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    comps: dict[int, list[int]] = {}
    for b in parent:
        comps.setdefault(find(b), []).append(b)
    spanned: dict[int, int] = {}
    for group in groups:
        roots = {find(broker_of[i]) for i in group if i in broker_of}
        for root in roots:
            spanned[root] = spanned.get(root, 0) + 1
    for root, members in sorted(comps.items()):
        if len(members) > 1 and spanned.get(root, 0) > 1:
            LOG.warning(
                "RESOLVE merge component chains %d auto-merge groups: %d brokers "
                "onto survivor %d (decide_merges caps ONE group at %d identities; "
                "the chaining broker holds identities in more than one)",
                spanned[root], len(members), min(members),
                R.MAX_AUTO_MERGE_COMPONENT)
    return sorted((sorted(c) for c in comps.values() if len(c) > 1))


def _bridge_pair_rows(conn: Any, pairs: list[tuple[int, int]],
                      identities: dict[int, R.Identity],
                      shared_contacts: dict[tuple[int, int], set[str]],
                      run_id: int) -> dict[str, tuple[int, int, str]]:
    """group_key -> (lo_broker, hi_broker, evidence) for identity pairs sharing a contact.

    Shared by the review and the auto-dismiss writers so both describe a pair the
    same way and collide on the same key: a pair the operator is already looking at
    is the SAME row the dismiss pass retires, not a second one beside it. The
    evidence's `bridges` key is the card's contact list — the SPA reads that name.
    """
    broker_of = _broker_of(conn, sorted({i for pair in pairs for i in pair}))
    rows: dict[str, tuple[int, int, str]] = {}
    for a, b in pairs:
        left, right = broker_of.get(a), broker_of.get(b)
        if left is None or right is None or left == right:
            continue
        shared = sorted(shared_contacts.get((a, b) if a < b else (b, a), ()))
        if not shared:
            continue
        lo, hi = (left, right) if left < right else (right, left)
        ia, ib = identities.get(a), identities.get(b)
        rows[f"contactbridge:{lo}:{hi}"] = (lo, hi, json.dumps({
            "identity_ids": [a, b],
            "sources": [ia.source if ia else None, ib.source if ib else None],
            "names": [ia.name if ia else None, ib.name if ib else None],
            "bridges": shared,
            "run_id": run_id,
        }, ensure_ascii=False))
    return rows


def _dismiss_name_conflicts(conn: Any, pairs: list[tuple[int, int]],
                            identities: dict[int, R.Identity],
                            shared_contacts: dict[tuple[int, int], set[str]],
                            run_id: int) -> int:
    """Retire pairs whose names conflict outright — without suppressing them.

    The name-gated engine never proposes a cross-name pair, so it hands this
    nothing; the writer stays wired as the retirement path for the cards the old
    corroboration guard queued before the name gate existed (PR #1096).

    Deliberately NOT routed through the operator dismiss path: that writes
    broker_merge_suppressions, a standing NO meant to record a HUMAN judgement. A
    machine verdict off a name comparison should not be able to permanently
    foreclose a merge, so this only stamps the candidate row. The row itself is what
    stops re-proposal (the upserts' status='proposed' guard), and every row is
    recoverable in bulk by its resolved_by stamp if the name rules are ever wrong.
    """
    if not pairs:
        return 0
    rows = _bridge_pair_rows(conn, pairs, identities, shared_contacts, run_id)
    if not rows:
        return 0
    keys = sorted(rows)
    with conn.cursor() as cur:
        cur.execute(_DISMISS_PAIR_UPSERT_SQL, {
            "gk": keys,
            "lo": [rows[k][0] for k in keys],
            "hi": [rows[k][1] for k in keys],
            "ev": [rows[k][2] for k in keys],
            "by": _AUTO_DISMISS_ACTOR,
        })
    return len(keys)


def _queue_review_pairs(conn: Any, pairs: list[tuple[int, int]],
                        identities: dict[int, R.Identity],
                        shared_contacts: dict[tuple[int, int], set[str]],
                        run_id: int) -> int:
    """Persist decide_merges' review pairs as operator-reviewable merge candidates.

    They were computed and dropped on the floor every sweep (9,377/day at the
    2026-08-12 review), so the only output of the conservative half of the engine
    never reached the operator. Keyed on the BROKER pair, not the identity pair: the
    operator merges brokers, so two identity pairs resolving to the same two brokers
    are one proposal, and the key stays stable across sweeps.

    Only pairs that share an ACTUAL contact are proposed — a one-click merge of two
    agents connected only through a shared switchboard several hops away is exactly
    what the cap exists to stop. That filter is not a size rail, though: it thins a
    component chained from SEVERAL values, and drops nothing at all from the pool it
    was written for, where one switchboard is on every member and therefore on every
    transitive pair (464 records -> 107,416 rows, all of them shared-contact pairs).
    The size rail is decide_merges itself, which hands an over-cap component its real
    EDGES instead of its closure; this writer sees n-1 rows, not n(n-1)/2. The
    evidence carries the shared contact so the operator judges the same fact the
    engine did."""
    if not pairs:
        return 0
    rows = _bridge_pair_rows(conn, pairs, identities, shared_contacts, run_id)
    if not rows:
        return 0
    keys = sorted(rows)
    with conn.cursor() as cur:
        cur.execute(_REVIEW_PAIR_UPSERT_SQL, {
            "gk": keys,
            "lo": [rows[k][0] for k in keys],
            "hi": [rows[k][1] for k in keys],
            "ev": [rows[k][2] for k in keys],
        })
    return len(keys)


def _blocked_component(identities_in: set[int], owner: dict[int, int],
                       by_identity: dict[int, set[int]]) -> tuple[int, int] | None:
    """The first active suppression this component would newly co-locate, if any."""
    for iid in sorted(identities_in):
        for other in sorted(by_identity.get(iid, ())):
            if other in identities_in and owner[iid] != owner[other]:
                return (iid, other) if iid < other else (other, iid)
    return None


def _component_reason(spanning: AbstractSet[int], groups: list[list[int]],
                      group_reasons: dict[tuple[int, ...], str] | None) -> str:
    """broker_merge_events.reason for a component: the union of its groups' paths.

    A component usually spans exactly one auto-merge group and takes its reason
    verbatim ('contact_name' | 'name_firm' | 'contact_name+name_firm'). When
    _broker_components chains several groups through a shared broker, the honest
    answer is every path that contributed, so the parts are unioned rather than
    one being picked."""
    parts: set[str] = set()
    for gi in spanning:
        for part in ((group_reasons or {}).get(tuple(groups[gi])) or "").split("+"):
            if part:
                parts.add(part)
    return "+".join(p for p in R.REASON_ORDER if p in parts) or _MERGE_REASON_FALLBACK


def _apply_merges(conn: Any, groups: list[list[int]], *,
                  suppressed_pairs: AbstractSet[tuple[int, int]] | None = None,
                  group_bridges: dict[tuple[int, ...], tuple[str, str]] | None = None,
                  group_reasons: dict[tuple[int, ...], str] | None = None,
                  ) -> tuple[int, int]:
    """Unify each BROKER component onto one survivor broker, set-based.

    Two queries fetch every group member's current broker and then every identity
    those brokers hold, the plan is built in Python (cheap — the groups are
    union-find output), and the whole thing applies in three array-driven
    statements. Per-group transactions were ~4 pooler round-trips each and overran
    the job timeout once a second source produced thousands of merges.

    The merge unit is the broker, not the identity component, and the loser's WHOLE
    identity set moves — the same invariant api/broker_review.py::merge_brokers
    enforces. Retiring a broker while leaving it some identities would freeze their
    rollups (_BROKER_ROLLUP only touches status='active'), hide them from the
    dossier, and let the next sweep elect the merged_away broker as a survivor.
    Idempotent — a component already on one broker is skipped, so a re-run after a
    partial apply converges. Reversible via broker_merge_events, whose `reason`
    records WHICH evidence path formed the group (_component_reason).

    `suppressed_pairs` is the apply-time BACKSTOP for the rail decide_merges cannot
    fully enforce: it removes the suppressed EDGE, but _broker_components then fuses
    components through any broker holding an identity in both, so A and B can still
    land on one broker via C without their edge existing. Any component that would
    newly co-locate an active suppressed pair is dropped WHOLE this run and logged —
    over-suppression of a chained component is the accepted trade (the operator can
    still merge explicitly, which lifts the suppression). The set passed in is a
    snapshot taken at the top of a merge step that runs ~8.4 min, so it is UNIONED
    with a fresh read inside this write transaction: an operator NO landing mid-sweep
    must not be applied over.

    The component cap applies HERE too, at broker grain: decide_merges bounds one
    group at R.MAX_AUTO_MERGE_COMPONENT identities, but _broker_components chains
    groups through a shared broker and nothing in the pure layer bounds that chain —
    the layer that re-introduces the fusion is the layer that has to bound it.
    A wider component is skipped whole and counted, not merged and warned about.

    Returns (brokers retired, components dropped by the backstop, components skipped
    for exceeding the cap)."""
    if not groups:
        return 0, 0, 0
    broker_of = _broker_of(conn, sorted({iid for g in groups for iid in g}))
    components = _broker_components(groups, broker_of)
    if not components:
        return 0, 0, 0
    held = _identities_of(conn, sorted({b for c in components for b in c}))
    owner = {iid: bid for bid, iids in held.items() for iid in iids}
    # broker -> the auto-merge groups that touch it, built ONCE. The per-component
    # rescan this replaces was O(components x groups x group size) with a set()
    # rebuilt per identity — 0.02s -> 7.02s at 5,000 groups.
    groups_by_broker: dict[int, set[int]] = {}
    for gi, group in enumerate(groups):
        for iid in group:
            bid = broker_of.get(iid)
            if bid is not None:
                groups_by_broker.setdefault(bid, set()).add(gi)

    gids: list[str] = []
    survivors: list[int] = []
    retired: list[int] = []
    idents: list[int] = []
    reasons: list[str] = []
    kinds: list[str | None] = []
    values: list[str | None] = []
    losers: dict[int, int] = {}
    dropped = 0
    oversized = 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 0")
        cur.execute(_SUPPRESSED_PAIRS_SQL)
        active = {(int(lo), int(hi)) for lo, hi in cur.fetchall()}
        active |= set(suppressed_pairs or ())
        by_identity: dict[int, set[int]] = {}
        for lo, hi in active:
            if lo in owner and hi in owner:
                by_identity.setdefault(lo, set()).add(hi)
                by_identity.setdefault(hi, set()).add(lo)

        for component in components:
            if len(component) > R.MAX_AUTO_MERGE_COMPONENT:
                oversized += 1
                LOG.warning(
                    "RESOLVE merge component SKIPPED as oversized: %d brokers exceeds "
                    "the cap of %d; chained through brokers holding identities in "
                    "several groups, so no single group vouches for it: %s",
                    len(component), R.MAX_AUTO_MERGE_COMPONENT, component)
                continue
            in_component = set(component)
            identities_in = {iid for b in component for iid in held.get(b, ())}
            blocked = (_blocked_component(identities_in, owner, by_identity)
                       if by_identity else None)
            if blocked is not None:
                dropped += 1
                LOG.warning(
                    "RESOLVE merge component DROPPED by suppression: identities %d/%d "
                    "are an active broker_merge_suppressions pair; brokers %s not merged",
                    blocked[0], blocked[1], component)
                continue
            spanning = {gi for b in in_component for gi in groups_by_broker.get(b, ())}
            # Only an unambiguous single-group component carries a stamp, and only
            # the identities of THAT group are stamped: the loser broker's other
            # identities were carried along by the broker-grain merge, not by this
            # contact, and stamping them would invent evidence.
            bridge = ((group_bridges or {}).get(tuple(groups[next(iter(spanning))]))
                      if len(spanning) == 1 else None)
            bridged = set(groups[next(iter(spanning))]) if bridge else set()
            reason = _component_reason(spanning, groups, group_reasons)
            survivor, *rest = component
            gid = str(uuid.uuid4())
            for loser in rest:
                losers[loser] = survivor
                for iid in held.get(loser, ()):
                    gids.append(gid)
                    survivors.append(survivor)
                    retired.append(loser)
                    idents.append(iid)
                    reasons.append(reason)
                    stamped = bridge if iid in bridged else None
                    kinds.append(stamped[0] if stamped else None)
                    values.append(stamped[1] if stamped else None)
        if not losers:
            return 0, dropped, oversized

        cur.execute(
            "INSERT INTO broker_merge_events (merge_group_id, survivor_broker_id, "
            "retired_broker_id, identity_id, prev_broker_id, reason, source, "
            "bridge_kind, bridge_value) "
            "SELECT g, s, r, i, r, n, 'auto', k, v "
            "FROM unnest(%(g)s::uuid[], %(s)s::bigint[], %(r)s::bigint[], %(i)s::bigint[], "
            "%(n)s::text[], %(k)s::text[], %(v)s::text[]) AS d(g, s, r, i, n, k, v)",
            {"g": gids, "s": survivors, "r": retired, "i": idents,
             "n": reasons, "k": kinds, "v": values},
        )
        cur.execute(
            "UPDATE broker_identities bi SET broker_id = d.s "
            "FROM unnest(%(i)s::bigint[], %(s)s::bigint[]) AS d(i, s) WHERE bi.id = d.i",
            {"i": idents, "s": survivors},
        )
        cur.execute(
            "UPDATE brokers b SET status = 'merged_away', merged_into = d.s, merged_at = now() "
            "FROM unnest(%(l)s::bigint[], %(s)s::bigint[]) AS d(l, s) WHERE b.id = d.l",
            {"l": list(losers), "s": list(losers.values())},
        )
    return len(losers), dropped, oversized


def _affected(conn: Any, listing_ids: list[int]) -> list[int]:
    if not listing_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT bi.broker_id FROM listings l "
            "JOIN broker_identities bi ON bi.id = l.broker_identity_id "
            "WHERE l.id = ANY(%s) AND bi.broker_id IS NOT NULL",
            (listing_ids,),
        )
        return [int(r[0]) for r in cur.fetchall()]


def _max_id(conn: Any, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT coalesce(max(id), 0) FROM {table}")
        return int(cur.fetchone()[0])


def _refresh_matview(conn: Any) -> None:
    # Non-concurrent REFRESH inside a txn so SET LOCAL can lift the statement
    # timeout — the matview aggregates the whole linked-listings corpus and
    # CONCURRENTLY cannot run in a txn (so it can't get the raised timeout). A
    # brief lock on a matview only the Brokers page reads, once per daily sweep,
    # is the right tradeoff for reliability.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 0")
        cur.execute("REFRESH MATERIALIZED VIEW broker_region_type_stats")


_CANDIDATE_BROKERS = """
SELECT b.id, b.display_name, b.primary_firm_id, f.canonical_domain, f.display_name
FROM brokers b JOIN firms f ON f.id = b.primary_firm_id
WHERE b.status = 'active' AND b.display_name IS NOT NULL
"""

_CANDIDATE_UPSERT = """
INSERT INTO broker_merge_candidates (group_key, broker_ids, reason, evidence)
VALUES (%(gk)s, %(ids)s, 'name_firm', %(ev)s)
ON CONFLICT (group_key) DO UPDATE SET
  broker_ids = EXCLUDED.broker_ids, evidence = EXCLUDED.evidence
  WHERE broker_merge_candidates.status = 'proposed'
"""

# Same ON CONFLICT gate as _CANDIDATE_UPSERT (a resolved group is never revived),
# but set-based: a sweep decides ~9.4k review pairs and one execute per pair is
# ~9.4k pooler round trips inside a run that already spends its whole budget.
# A pair is exactly two brokers, so parallel lo/hi arrays build broker_ids inline
# — unnest() flattens a bigint[][] and could not carry per-row arrays. Evidence
# rides as text[] then casts: there is no text[] -> jsonb[] cast to lean on.
_REVIEW_PAIR_UPSERT_SQL = """
INSERT INTO broker_merge_candidates (group_key, broker_ids, reason, evidence)
SELECT d.gk, ARRAY[d.lo, d.hi], 'contact_bridge_review', d.ev::jsonb
FROM unnest(%(gk)s::text[], %(lo)s::bigint[], %(hi)s::bigint[], %(ev)s::text[])
     AS d(gk, lo, hi, ev)
ON CONFLICT (group_key) DO UPDATE SET
  broker_ids = EXCLUDED.broker_ids, evidence = EXCLUDED.evidence
  WHERE broker_merge_candidates.status = 'proposed'
"""


# Stamped into resolved_by so an auto-dismissal is always distinguishable from an
# operator's, and so the whole cohort can be reopened with one UPDATE.
_AUTO_DISMISS_ACTOR = "auto:name_conflict"

# Same key + same status='proposed' guard as the review upsert, so this both stops
# proposing a name-conflicting pair AND retires the ones already sitting in the
# queue. A row an operator already resolved is never touched.
_DISMISS_PAIR_UPSERT_SQL = """
INSERT INTO broker_merge_candidates (
  group_key, broker_ids, reason, evidence, status, resolved_at, resolved_by)
SELECT d.gk, ARRAY[d.lo, d.hi], 'contact_bridge_review', d.ev::jsonb,
       'dismissed', now(), %(by)s
FROM unnest(%(gk)s::text[], %(lo)s::bigint[], %(hi)s::bigint[], %(ev)s::text[])
     AS d(gk, lo, hi, ev)
ON CONFLICT (group_key) DO UPDATE SET
  broker_ids = EXCLUDED.broker_ids, evidence = EXCLUDED.evidence,
  status = 'dismissed', resolved_at = now(), resolved_by = EXCLUDED.resolved_by
  WHERE broker_merge_candidates.status = 'proposed'
"""


def _generate_merge_candidates(conn: Any) -> int:
    """Propose Phase-5 review groups: active brokers that share a normalized name AND
    a firm but are separate ids (the corporate/role-inbox case the auto-merge guard
    deliberately leaves apart). Idempotent — group_key keeps regeneration from
    reviving a merged/dismissed group; a merged group's losers go inactive and the
    group shrinks below 2, so it never re-proposes."""
    from collections import defaultdict
    from psycopg.types.json import Jsonb
    groups: dict[tuple[str, int], list[int]] = defaultdict(list)
    meta: dict[tuple[str, int], tuple[str, str | None, str | None]] = {}
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_BROKERS)
        for bid, name, firm_id, domain, firm_name in cur.fetchall():
            nk = R.name_key(name)
            if not nk:
                continue
            key = (nk, int(firm_id))
            groups[key].append(int(bid))
            meta[key] = (name, domain, firm_name)
    proposed = 0
    with conn.cursor() as cur:
        for (nk, firm_id), ids in groups.items():
            if len(ids) < 2:
                continue
            name, domain, firm_name = meta[(nk, firm_id)]
            cur.execute(_CANDIDATE_UPSERT, {
                "gk": f"namefirm:{firm_id}:{nk}",
                "ids": sorted(ids),
                "ev": Jsonb({"name": name, "firm_domain": domain,
                             "firm_name": firm_name, "broker_count": len(ids)}),
            })
            proposed += 1
    return proposed


def _broker_bearing_ids(conn: Any, page_size: int) -> list[int]:
    """Every broker-bearing listing's SURROGATE id, ascending, in bounded keyset pages.

    Keyset — never one unbounded `ORDER BY` scan: the corpus crossed the pooler
    statement timeout (2 min) once four portals were attributed, so the single
    SELECT that loaded all ids timed out (#639 era). Each page is `... AND
    id > :last ... LIMIT :n`, so no statement is unbounded.

    Paging moved from sreality_id to id with the R2 cutover (the callers now scope
    attribution on `l.id = ANY(...)`). The gap-skipping property keyset existed for
    is unchanged — and `id` is in fact denser than sreality_id, whose sparse
    [-287k, 4.29B] span is what made a numeric-range loop untenable."""
    ids: list[int] = []
    last: int | None = None
    srcs = list(_BROKER_SOURCES)
    with conn.cursor() as cur:
        while True:
            if last is None:
                cur.execute(
                    "SELECT id FROM listings WHERE source = ANY(%(srcs)s) "
                    "ORDER BY id LIMIT %(lim)s",
                    {"srcs": srcs, "lim": page_size})
            else:
                cur.execute(
                    "SELECT id FROM listings WHERE source = ANY(%(srcs)s) "
                    "AND id > %(last)s ORDER BY id LIMIT %(lim)s",
                    {"srcs": srcs, "last": last, "lim": page_size})
            page = [int(r[0]) for r in cur.fetchall()]
            if not page:
                break
            ids.extend(page)
            last = page[-1]
            if len(page) < page_size:
                break
    return ids


def _sweep_state(conn: Any) -> tuple[int | None, int, str | None]:
    """(resume id, ids swept so far in the open lap, when that lap opened).

    Every field degrades to its first-run default on a missing row, a NULL or a
    hand-edited value — the daily sweep resumes from the corpus floor rather than
    crashing."""
    with conn.cursor() as cur:
        cur.execute(_READ_SETTING_SQL, (_SWEEP_CURSOR_KEY,))
        row = cur.fetchone()
    value = row[0] if row else None
    if not isinstance(value, dict):
        return None, 0, None
    lap_started = value.get("lap_started_at")
    return (_as_int(value.get("last_id")), _as_int(value.get("lap_swept")) or 0,
            lap_started if isinstance(lap_started, str) else None)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_sweep_progress(conn: Any, *, last_id: int, lap_swept: int,
                           lap_started_at: str | None, lap_complete: bool,
                           elapsed_s: float) -> None:
    """Advance the rotation cursor and, when the lap closed, stamp completion.

    Written right after attribution rather than in _finalize: the 17-25 min tail
    (auto-merge, rollups, matview, candidates) fails often enough that
    leaving the cursor behind it threw away a whole run's rotation advance and
    re-walked the same window the next day — the exact starvation the cursor
    exists to remove. Both statements are idempotent upserts that depend on
    nothing the tail does."""
    with conn.cursor() as cur:
        cur.execute(_WRITE_SWEEP_CURSOR_SQL, {
            "key": _SWEEP_CURSOR_KEY, "last_id": last_id,
            "lap_swept": 0 if lap_complete else lap_swept,
            "lap_started_at": None if lap_complete else lap_started_at,
        })
        if lap_complete:
            cur.execute(_STAMP_SWEEP_COMPLETE_SQL, {
                "key": _SWEEP_COMPLETE_KEY, "swept": lap_swept,
                "elapsed_s": round(elapsed_s, 1),
            })


def _rotate_from_cursor(ids: list[int], last_id: int | None) -> list[int]:
    """`ids` re-ordered to resume just past the previous sweep's stopping point.

    A rotation, NOT a filter: the tail the last sweep never reached is walked first
    and the head it already covered follows, so a budget-truncated sweep advances
    through the corpus day by day and wraps — every id stays reachable, which a bare
    `id > cursor` resume would not guarantee (the head would starve instead)."""
    if last_id is None or not ids:
        return ids
    start = bisect.bisect_right(ids, last_id)
    if start >= len(ids):
        return ids
    return ids[start:] + ids[:start]


def _resilient_step(
    conn: Any, reconnect: Callable[[], Any], holder: str = "",
) -> tuple[Callable[..., Any], Callable[[], Any]]:
    """Return (`step`, `live`) — `step(op, label, attempts=None)` runs op through
    db.run_resilient and REBINDS the live connection internally (run_resilient may
    hand back a FRESH one after a pooler drop), `live()` reads it back out for the
    caller.

    Factored once per driver instead of repeating the `res, conn = db.run_resilient(...)`
    rebinding at each of a dozen phases: this sweep holds ONE connection for 60-90
    minutes, so a forgotten rebind is a latent 'connection is closed' at the next
    phase — the exact failure this wave exists to remove. Every op passed in must be
    idempotent; it is re-run from the top on retry.

    `holder` (when set) makes the lock heartbeat the FIRST statement of EVERY attempt,
    inside the retried op. It used to be a step of its own before each phase, which
    meant a retried phase re-ran from the top with NO intervening beat: one replay of
    the 8.4-min merge phase left the lock unrefreshed for ~17 min and a `*/10`
    incremental could steal it mid-sweep. Beating inside also re-asserts the lock on
    the FRESH connection after a reconnect, and covers the phases that had no beat at
    all (the rollup/matview/candidates tail, and the whole incremental driver).
    The bound on the renewal gap is now ONE attempt + backoff, which is what
    _LOCK_STALE_MIN is sized against.

    `attempts` overrides db.run_resilient's default of 4. Pass it for phases that lift
    the statement timeout or otherwise run for minutes: four attempts of a multi-minute
    phase is how a recoverable blip turns into a timeout-minutes SIGKILL, i.e. the
    silent `cancelled` this wave exists to eliminate."""
    state = {"conn": conn}

    def step(op: Callable[[Any], _T], label: str, attempts: int | None = None) -> _T:
        def _with_beat(c: Any) -> _T:
            if holder:
                _heartbeat_lock(c, holder)
            return op(c)

        budget = {} if attempts is None else {"attempts": attempts}
        result, state["conn"] = db.run_resilient(
            state["conn"], _with_beat, reconnect=reconnect, label=label, **budget)
        return result

    return step, lambda: state["conn"]


def _run_full(conn: Any, free: list[str], franchise: list[str], auto_merge: bool,
              batch_size: int, deadline: float | None, holder: str = "", *,
              reconnect: Callable[[], Any]) -> tuple[dict[str, int], Any]:
    """Run the daily reconcile. Returns (stats, live_conn) — the connection may be a
    FRESH one if a phase rode out a pooler drop, so main() must rebind before
    releasing the lock on it."""
    step, live = _resilient_step(conn, reconnect, holder)

    # NOT wrapped: the run-row INSERT is the one non-idempotent statement here (a
    # replay would open a second broker_resolution_runs row) and it is the first
    # thing the sweep does, so a drop here costs nothing but the retry-free red.
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        cutoff = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO broker_resolution_runs (mode) VALUES ('full') RETURNING id"
        )
        run_id = int(cur.fetchone()[0])
    t0 = time.monotonic()

    # Chunk the ACTUAL listing ids of every broker-bearing source (sreality_id is the
    # sparse PK — a numeric-range loop would walk huge empty gaps). Keyset-paginated
    # in bounded pages so no single statement is unbounded; attribution then runs per
    # id-chunk, source-filtered inside. attempts=2 on the chunk phases: they inherit
    # the pooler's ~2-min statement timeout, which run_resilient classifies transient,
    # so the default 4 burns ~8 min on a chunk that is simply poisoned.
    all_ids = step(lambda c: _broker_bearing_ids(c, batch_size), "resolve.ids")
    # Resume where the last sweep stopped instead of at the corpus floor. Attribution
    # regularly spends the whole budget (the 08-09 and 08-10 sweeps both logged it),
    # so an ascending-from-zero walk re-attributed the same head every day and never
    # reached the tail — the newest listings — at all.
    last_id, lap_swept, lap_started_at = step(_sweep_state, "resolve.sweep_cursor")
    walk = _rotate_from_cursor(all_ids, last_id)
    first_swept = walk[0] if walk else None
    last_swept: int | None = None
    swept = 0
    complete = True
    for i in range(0, len(walk), batch_size):
        chunk = walk[i:i + batch_size]
        step(lambda c, ids=chunk: _attribute(c, "l.id = ANY(%(ids)s)", {"ids": ids}),
             "resolve.attribute", attempts=2)
        last_swept = chunk[-1]
        swept += len(chunk)
        # `i + batch_size < len(walk)`: a deadline crossed while finishing the LAST
        # chunk is a complete walk, not a truncated one — flagging it incomplete
        # would scope the dirty clear on a sweep that re-attributed everything.
        if deadline and time.monotonic() > deadline and i + batch_size < len(walk):
            LOG.warning("RESOLVE full: time budget reached during attribution at %d/%d ids",
                        swept, len(walk))
            complete = False
            break
    # The lap — one full trip around the rotation, however many runs it takes — is
    # what "a complete sweep" means once truncation is the designed steady state.
    lap_swept += swept
    lap_complete = bool(all_ids) and lap_swept >= len(all_ids)
    LOG.info("RESOLVE full attribution swept=%d/%d lap=%d/%d complete=%s lap_complete=%s "
             "resume_at=%s", swept, len(all_ids), lap_swept, len(all_ids), complete,
             lap_complete, last_swept)
    if last_swept is not None:
        step(lambda c, last=last_swept: _record_sweep_progress(
            c, last_id=last, lap_swept=lap_swept, lap_started_at=lap_started_at,
            lap_complete=lap_complete, elapsed_s=time.monotonic() - t0),
            "resolve.sweep_progress")

    step(lambda c: _resolve_firms(c, free, franchise), "resolve.firms")
    # Batch the listings->firm link over the same id chunks — a single global UPDATE
    # joining every linked listing to its firm exceeds the pooler statement timeout
    # now that idnes adds ~125k linkable rows. sreality_id is sparse, so chunk the
    # actual ids (the PR #470 lesson), not a numeric range.
    #
    # Its own FLOOR on top of the shared deadline, not the bare deadline: attribution
    # routinely spends the whole --max-seconds budget (the 08-09 and 08-10 sweeps both
    # logged "time budget reached during attribution"), so reusing `deadline` here
    # would trip on the FIRST check and skip the global firm reconcile outright — and
    # this is the cheap half, 43-186s for the whole ~107-chunk corpus on those same
    # runs. The floor still bounds the accumulation case the guard exists for (~100
    # chunks each timing out once, then succeeding, with nothing watching the clock),
    # it just refuses to trade a phase that normally finishes for that bound. An
    # overrun degrades to a partial link + a warning, and the next sweep redoes it.
    #
    # Walks `walk`, not `all_ids`: on the day this phase does overrun, an
    # ascending-from-the-floor walk would break at the same low id every time and
    # leave every id above it un-reconciled forever — the identical starvation the
    # rotation cursor fixes for attribution.
    firm_deadline = max(deadline, time.monotonic() + _FIRM_LINK_MIN_SECONDS) if deadline else None
    for i in range(0, len(walk), batch_size):
        chunk = walk[i:i + batch_size]
        step(lambda c, ids=chunk: _link_listings_firm(c, "AND l.id = ANY(%(ids)s)", {"ids": ids}),
             "resolve.link_firm", attempts=2)
        if firm_deadline and time.monotonic() > firm_deadline:
            LOG.warning("RESOLVE full: time budget reached during firm linking at %d/%d ids",
                        i, len(walk))
            break
    attached = step(_attach_singletons, "resolve.singletons")
    LOG.info("RESOLVE full attribution+firms done elapsed=%.1fs", time.monotonic() - t0)

    # Merge BEFORE the rollups so dsc / listing_count / membership reflect the unified
    # broker groupings (a merge re-points broker_identities.broker_id). The step is
    # portal-agnostic and runs unless app_settings.broker_auto_merge_enabled is an
    # explicit false (the kill switch — absent means on).
    # Replay-safe: _apply_merges skips a component already on one broker, so a retry
    # after a committed apply converges (the only residue is an UNDERCOUNTED
    # auto_merges on the retried attempt — bookkeeping, not data).
    #
    # attempts=2 from here to the end of the sweep: every phase below lifts the
    # statement timeout (SET LOCAL statement_timeout = 0) or is otherwise unbounded,
    # and none of them is deadline-guarded. Measured tail is 17-25 min total with the
    # merge alone at ~8.4 min, so the default 4 attempts puts the worst case at ~100
    # min of tail on top of a ~53-min prefix — past the 110-min backstop. Two keeps
    # the useful replay (a pooler drop, a passing lock wait) and bounds the tail at
    # ~50 min. Same reasoning, same number as recompute's _BATCH_RESILIENT_ATTEMPTS.
    if auto_merge:
        auto_merges, queued, suppressed = step(lambda c: _auto_merge(c, run_id),
                                               "resolve.merge", attempts=2)
        LOG.info("RESOLVE full merge done auto=%d queued=%d suppressed=%d elapsed=%.1fs",
                 auto_merges, queued, suppressed, time.monotonic() - t0)
    else:
        auto_merges = queued = suppressed = 0
        LOG.warning("RESOLVE full merge SKIPPED: app_settings.%s is false",
                    _AUTO_MERGE_ENABLED_KEY)

    # Identity rollup: ONE global timeout-lifted statement, like the firm rollup
    # below. broker_identities.id is NOT a dense serial — every full-sweep upsert
    # burns a sequence value on ON CONFLICT, so max(id) runs ~2.5M for ~35k live
    # rows (~1.4% of the span) and grows ~35k/sweep. Id-range batching it therefore
    # did ~500 mostly-EMPTY batches per sweep (each a round-trip), climbing forever.
    # The global form is a single seq-scan + hashaggregate over the linked corpus
    # (no join blow-up), so it does NOT need the broker rollup's per-batch granularity.
    def _identity_rollup(c: Any) -> None:
        with c.transaction(), c.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 0")
            cur.execute(_IDENTITY_ROLLUP.format(extra=""))

    step(_identity_rollup, "resolve.identity_rollup", attempts=2)
    # Broker rollup + membership: batched by brokers.id, which IS dense (~7 batches),
    # so each batch commits independently (crash-safe) and the lock heartbeats on every
    # batch ATTEMPT. The id-batch bounds MEMORY + lock granularity, NOT runtime: each batch
    # still aggregates the cold listings corpus (the broker rollup joins listings ~3x
    # for DISTINCT property/active counts), so a single batch's optimal-plan scan can
    # legitimately exceed the 2-min pooler statement timeout once the corpus is large
    # — that timeout is an app-query guardrail, wrong for this serialized once-daily
    # reconcile. Lift it per batch, exactly as the firm rollup / matview / merge below
    # already do; the job's real bounds are the advisory lock + the job timeout.
    # (Shrinking the batch to dodge the 2-min wall would just move the wall — the cost
    # is per-listing-read, not per-broker.)
    def _broker_rollup_batch(c: Any, lo: int) -> None:
        with c.transaction(), c.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 0")
            cur.execute(_BROKER_ROLLUP.format(
                bscope="AND broker_id >= %(lo)s AND broker_id < %(hi)s"),
                {"lo": lo, "hi": lo + batch_size})
            cur.execute(_MEMBERSHIP_RECOMPUTE.format(
                bscope="AND bi.broker_id >= %(lo)s AND bi.broker_id < %(hi)s",
                mscope="m.broker_id >= %(lo)s AND m.broker_id < %(hi)s AND"),
                {"lo": lo, "hi": lo + batch_size})

    max_broker_id = step(lambda c: _max_id(c, "brokers"), "resolve.max_broker_id")
    for lo in range(1, max_broker_id + 1, batch_size):
        step(lambda c, lo=lo: _broker_rollup_batch(c, lo), "resolve.broker_rollup",
             attempts=2)

    # Global firm rollup aggregates the whole linked-listings corpus in one pass;
    # like the matview refresh, lift the statement timeout for this once-per-sweep
    # analytical statement rather than batch it (firms are few, so a firm-id window
    # would just re-scan the same listings).
    def _firm_rollup(c: Any) -> None:
        with c.transaction(), c.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 0")
            cur.execute(_FIRM_ROLLUP)
            cur.execute(_FIRM_DISPLAY_NAMES)

    step(_firm_rollup, "resolve.firm_rollup", attempts=2)
    LOG.info("RESOLVE full rollups done elapsed=%.1fs", time.monotonic() - t0)
    step(_refresh_matview, "resolve.matview", attempts=2)
    candidates = step(_generate_merge_candidates, "resolve.candidates", attempts=2)
    LOG.info("RESOLVE full merge candidates proposed=%d elapsed=%.1fs",
             candidates, time.monotonic() - t0)

    def _finalize(c: Any) -> None:
        with c.cursor() as cur:
            if complete:
                cur.execute(_CLEAR_DIRTY, {"cutoff": cutoff})
            elif first_swept is not None and last_swept is not None:
                cur.execute(
                    _CLEAR_DIRTY_SWEPT_SQL if first_swept <= last_swept
                    else _CLEAR_DIRTY_SWEPT_WRAPPED_SQL,
                    {"cutoff": cutoff, "lo": first_swept, "hi": last_swept},
                )
            cur.execute(_RETIRE_DEAD_CANDIDATES_SQL, {"by": _SWEEP_RETIRE_ACTOR})
            cur.execute(
                "UPDATE broker_resolution_runs SET ended_at = now(), brokers_recomputed = "
                "(SELECT count(*) FROM brokers WHERE status='active'), identities_upserted = "
                "(SELECT count(*) FROM broker_identities), firms_recomputed = (SELECT count(*) FROM firms), "
                "auto_merges = %s, queued_for_review = %s, suppressed_pairs = %s WHERE id = %s",
                (auto_merges, queued, suppressed, run_id),
            )

    step(_finalize, "resolve.finalize")
    return {"attached": attached, "auto_merges": auto_merges, "queued": queued,
            "suppressed": suppressed}, live()


def _run_incremental(conn: Any, free: list[str], franchise: list[str],
                     batch_size: int, holder: str = "", *,
                     reconnect: Callable[[], Any]) -> tuple[dict[str, int], Any]:
    """Drain the dirty queue. Returns (stats, live_conn) — see _run_full.

    `holder` heartbeats the lock inside every retried phase (see _resilient_step).
    A pass normally runs seconds against a 20-min staleness window, but nothing
    guaranteed that: main() acquired the lock and then never refreshed it, so a
    pass that spent its time in retries could be declared stale while alive."""
    step, live = _resilient_step(conn, reconnect, holder)
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        cutoff = cur.fetchone()[0]
        cur.execute("INSERT INTO broker_resolution_runs (mode) VALUES ('incremental') RETURNING id")
        run_id = int(cur.fetchone()[0])
        # Drain the work queue only. New + content-changed listings are enqueued
        # at write time by the detail writers (write_detail_batch / ingest_scraped_
        # listing), so this is the complete set of listings whose broker block may
        # need (re)attribution since the last pass. The claim is bounded by cutoff
        # so a write mid-run survives to the next pass (dirty_properties, rule #20).
        cur.execute(_CLAIM_DIRTY, {"cutoff": cutoff, "limit": batch_size})
        claimed = {int(r[0]) for r in cur.fetchall()}

    if not claimed:
        with conn.cursor() as cur:
            cur.execute("UPDATE broker_resolution_runs SET ended_at = now() WHERE id = %s", (run_id,))
        return {"attributed": 0, "brokers": 0}, live()

    # Everything below is idempotent (latest-wins upserts + scoped rollups) and the
    # claimed ids are held in PYTHON until the _DELETE_DIRTY at the end, so a replay
    # after a pooler drop redoes exactly the same work rather than losing it. Note
    # what "claim" does NOT mean here: _CLAIM_DIRTY is a plain SELECT on an autocommit
    # connection — no row lock, no claim marker, nothing another run would respect.
    # Exclusion comes from broker_resolution_lock (plus this workflow's own
    # concurrency group); the scoped _DELETE_DIRTY is what keeps a concurrent
    # enqueue safe. The 2026-08-10 23:10 red died on the rollup below with
    # 'SSL connection has been closed'.
    ids = sorted(claimed)
    step(lambda c: _attribute(c, "l.id = ANY(%(ids)s)", {"ids": ids}), "resolve.attribute")
    step(lambda c: _resolve_firms(c, free, franchise), "resolve.firms")
    step(lambda c: _link_listings_firm(c, "AND l.id = ANY(%(ids)s)", {"ids": ids}),
         "resolve.link_firm")
    step(_attach_singletons, "resolve.singletons")
    bids = step(lambda c: _affected(c, ids), "resolve.affected")

    def _rollups_and_finalize(c: Any) -> None:
        with c.cursor() as cur:
            if bids:
                cur.execute(_IDENTITY_ROLLUP.format(extra="AND broker_identity_id IN "
                            "(SELECT id FROM broker_identities WHERE broker_id = ANY(%(bids)s))"), {"bids": bids})
                cur.execute(_BROKER_ROLLUP.format(bscope="AND broker_id = ANY(%(bids)s)"), {"bids": bids})
                cur.execute(_MEMBERSHIP_RECOMPUTE.format(bscope="AND bi.broker_id = ANY(%(bids)s)",
                            mscope="m.broker_id = ANY(%(bids)s) AND"), {"bids": bids})
            cur.execute(_DELETE_DIRTY, {"ids": ids, "cutoff": cutoff})
            cur.execute(
                "UPDATE broker_resolution_runs SET ended_at = now(), listings_attributed = %s, "
                "brokers_recomputed = %s WHERE id = %s",
                (len(ids), len(bids), run_id),
            )

    step(_rollups_and_finalize, "resolve.rollups")
    return {"attributed": len(ids), "brokers": len(bids)}, live()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--backfill", action="store_true", help="Alias for the full sweep (first population).")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--max-seconds", type=int, default=0, help="Wall-clock budget for full attribution (0 = none).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # Explicit check before db.connect(): database_url() would raise a bare
    # RuntimeError instead of this friendly message + exit 2.
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    mode = "incremental" if args.incremental else "full"
    LOG.info("RESOLVE config mode=%s batch_size=%d", mode, args.batch_size)
    started = time.monotonic()
    deadline = started + args.max_seconds if args.max_seconds else None

    def reconnect() -> Any:
        return db.connect(db_url)

    # db.connect() instead of a bare psycopg.connect(): same autocommit +
    # prepare_threshold=None, PLUS TCP keepalives and a 3-attempt handshake retry.
    # This connection is held for the whole 60-90 minute sweep, so a pooler idle
    # reaper or a Supabase restart used to kill the run outright.
    with db.connect(db_url) as conn:
        free, franchise, auto_merge = _settings(conn)
        if args.dry_run:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM dirty_broker_listings")
                dirty = int(cur.fetchone()[0])
            LOG.info("RESOLVE dry-run mode=%s free=%d franchise=%d auto_merge=%s dirty=%d; exit",
                     mode, len(free), len(franchise), auto_merge, dirty)
            return 0

        # Pooler-safe mutual exclusion (migration 192). The incremental yields when the
        # lock is held (its work is subsumed by whatever holds it); the full sweep waits,
        # taking over only a stale (dead-holder) lock — it is the reconcile that must run.
        holder = f"{mode}-{uuid.uuid4()}"
        if args.incremental:
            if not _try_acquire_lock(conn, holder, mode):
                LOG.info("RESOLVE skip mode=incremental: lock held by another resolution run")
                return 0
        elif not _acquire_lock_blocking(conn, holder, mode, started + _LOCK_WAIT_MAX_SECONDS):
            LOG.error("RESOLVE abort mode=full: could not acquire lock within %ds", _LOCK_WAIT_MAX_SECONDS)
            return 1

        try:
            # Rebind conn: a phase that rode out a pooler drop hands back a FRESH
            # connection, and the release below must run on the live one.
            if args.incremental:
                res, conn = _run_incremental(conn, free, franchise, args.batch_size,
                                             holder, reconnect=reconnect)
                LOG.info("RESOLVE incremental done attributed=%d brokers=%d elapsed=%.1fs",
                         res["attributed"], res["brokers"], time.monotonic() - started)
            else:
                res, conn = _run_full(conn, free, franchise, auto_merge, args.batch_size,
                                      deadline, holder, reconnect=reconnect)
                LOG.info("RESOLVE full done attached=%d auto_merges=%d queued=%d "
                         "suppressed=%d elapsed=%.1fs",
                         res["attached"], res["auto_merges"], res["queued"],
                         res["suppressed"], time.monotonic() - started)
        finally:
            # `conn` is only rebound on a normal return, so on a raise it can be the
            # socket run_resilient already closed — hence the reconnect fallback.
            _release_lock(conn, holder, reconnect)
    return 0


if __name__ == "__main__":
    sys.exit(main())
