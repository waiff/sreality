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
  * full (default, daily reconcile): re-attribute EVERY sreality listing (batched by
    id range), recompute all rollups, rebuild memberships + firm counts, run the
    cross-source merge step, REFRESH the leaderboard matview, clear the queue. The
    self-healing backstop.
  * --backfill: alias for full (the one-shot first population from existing
    raw_json). Run in Actions after merge — local has no psycopg, and a raw_json
    scan over the pooler times out, so it is keyset-batched here.

Attribution is the only source-specific step (sreality reads raw_json->'user');
everything downstream (firms, singletons, rollups, grouping, merges) is source-
agnostic, so a future portal adds an attribution step and reuses the rest (rule #21).

Identity keystone: cross-source merges only via contacts personal on BOTH sides
(frequency==1 each source), corroborated (toolkit.broker_resolver). With only
sreality live, the merge step finds no cross-source bridges and is a no-op — it is
built + unit-tested now so the second portal activates it without new code.

Required env: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from typing import Any

from toolkit import broker_resolver as R

LOG = logging.getLogger("resolve_brokers")

_FREE_KEY = "broker_free_email_domains"
_FRANCHISE_KEY = "broker_franchise_domains"
_AUTO_MERGE_KEY = "broker_auto_merge_sources"

# --- Attribution (sreality, set-based from raw_json). {sel} = a listings selector. ---

_IDENTITIES_UPSERT = """
WITH src AS (
  SELECT
    (l.raw_json->'user'->>'user_id')                           AS uid,
    nullif(l.raw_json->'user'->>'user_name', '')               AS name,
    lower(nullif(l.raw_json->'user'->>'user_email', ''))       AS email,
    nullif(l.raw_json->'user'->>'broker_rating', '')::numeric  AS rating,
    nullif(l.raw_json->'user'->>'broker_review_count', '')::int AS reviews,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'sreality' AND l.raw_json ? 'user'
    AND (l.raw_json->'user'->>'user_id') IS NOT NULL
    AND {sel}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (
  SELECT DISTINCT ON (uid) uid, name, email, rating, reviews
  FROM src ORDER BY uid, last_seen_at DESC NULLS LAST
)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, email, rating, review_count,
   first_seen_at, last_seen_at, attrs_computed_at)
SELECT 'sreality', a.uid, lt.name, lt.email, lt.rating, lt.reviews, a.fseen, a.lseen, now()
FROM agg a JOIN latest lt USING (uid)
ON CONFLICT (source, source_broker_id_native) DO UPDATE SET
  display_name = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.display_name ELSE broker_identities.display_name END,
  email        = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.email ELSE broker_identities.email END,
  rating       = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.rating ELSE broker_identities.rating END,
  review_count = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.review_count ELSE broker_identities.review_count END,
  first_seen_at = least(broker_identities.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = greatest(broker_identities.last_seen_at, EXCLUDED.last_seen_at),
  attrs_computed_at = now()
"""

_CONTACTS_EMAIL_UPSERT = """
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'sreality', 'email', lower(nullif(l.raw_json->'user'->>'user_email', '')),
       min(l.first_seen_at), max(l.last_seen_at)
FROM listings l
JOIN broker_identities bi
  ON bi.source = 'sreality' AND bi.source_broker_id_native = (l.raw_json->'user'->>'user_id')
WHERE l.source = 'sreality' AND l.raw_json ? 'user'
  AND nullif(l.raw_json->'user'->>'user_email', '') IS NOT NULL AND {sel}
GROUP BY bi.id, lower(nullif(l.raw_json->'user'->>'user_email', ''))
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
"""

_CONTACTS_PHONE_UPSERT = """
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'sreality', 'phone', ph.norm, min(l.first_seen_at), max(l.last_seen_at)
FROM listings l
JOIN broker_identities bi
  ON bi.source = 'sreality' AND bi.source_broker_id_native = (l.raw_json->'user'->>'user_id')
CROSS JOIN LATERAL (
  SELECT CASE WHEN length(d.digits) = 9 THEN '420' || d.digits ELSE d.digits END AS norm
  FROM (
    SELECT regexp_replace(p->>'phone', '[^0-9]', '', 'g') AS digits
    FROM jsonb_array_elements(coalesce(l.raw_json->'user'->'user_phones', '[]'::jsonb)) p
  ) d
  WHERE length(d.digits) >= 9
) ph
WHERE l.source = 'sreality' AND l.raw_json ? 'user' AND {sel}
GROUP BY bi.id, ph.norm
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
"""

_LINK_LISTINGS_IDENTITY = """
UPDATE listings l SET broker_identity_id = bi.id
FROM broker_identities bi
WHERE bi.source = 'sreality' AND bi.source_broker_id_native = (l.raw_json->'user'->>'user_id')
  AND l.source = 'sreality' AND l.raw_json ? 'user'
  AND (l.raw_json->'user'->>'user_id') IS NOT NULL
  AND l.broker_identity_id IS DISTINCT FROM bi.id AND {sel}
"""

# --- idnes attribution (from raw_json->'broker'; account_oid is the per-broker key,
#     name from the contact heading, email/phone from the contact links). Same shape
#     as sreality but a different JSON path + a scalar phone. ---

_IDNES_IDENTITIES_UPSERT = """
WITH src AS (
  SELECT
    (l.raw_json->'broker'->>'account_oid')            AS uid,
    nullif(l.raw_json->'broker'->>'name', '')          AS name,
    lower(nullif(l.raw_json->'broker'->>'email', ''))  AS email,
    l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'idnes' AND l.raw_json ? 'broker'
    AND (l.raw_json->'broker'->>'account_oid') IS NOT NULL
    AND {sel}
),
agg AS (SELECT uid, min(first_seen_at) AS fseen, max(last_seen_at) AS lseen FROM src GROUP BY uid),
latest AS (SELECT DISTINCT ON (uid) uid, name, email FROM src ORDER BY uid, last_seen_at DESC NULLS LAST)
INSERT INTO broker_identities
  (source, source_broker_id_native, display_name, email, first_seen_at, last_seen_at, attrs_computed_at)
SELECT 'idnes', a.uid, lt.name, lt.email, a.fseen, a.lseen, now()
FROM agg a JOIN latest lt USING (uid)
ON CONFLICT (source, source_broker_id_native) DO UPDATE SET
  display_name = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.display_name ELSE broker_identities.display_name END,
  email        = CASE WHEN EXCLUDED.last_seen_at >= broker_identities.last_seen_at
                      THEN EXCLUDED.email ELSE broker_identities.email END,
  first_seen_at = least(broker_identities.first_seen_at, EXCLUDED.first_seen_at),
  last_seen_at  = greatest(broker_identities.last_seen_at, EXCLUDED.last_seen_at),
  attrs_computed_at = now()
"""

# The chunk CTE is MATERIALIZED so the listings scan is bounded by {sel} (the chunk
# ids) BEFORE the join to broker_identities — otherwise, with cold idnes stats on the
# first sweep, the planner inverts the join and detoasts far more idnes raw_json than
# the chunk, blowing the statement timeout.
_IDNES_CONTACTS_EMAIL_UPSERT = """
WITH chunk AS MATERIALIZED (
  SELECT (l.raw_json->'broker'->>'account_oid') AS uid,
         lower(nullif(l.raw_json->'broker'->>'email', '')) AS email,
         l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'idnes' AND l.raw_json ? 'broker'
    AND nullif(l.raw_json->'broker'->>'email', '') IS NOT NULL AND {sel}
)
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'idnes', 'email', c.email, min(c.first_seen_at), max(c.last_seen_at)
FROM chunk c
JOIN broker_identities bi ON bi.source = 'idnes' AND bi.source_broker_id_native = c.uid
GROUP BY bi.id, c.email
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
"""

_IDNES_CONTACTS_PHONE_UPSERT = """
WITH chunk AS MATERIALIZED (
  SELECT (l.raw_json->'broker'->>'account_oid') AS uid,
         regexp_replace(l.raw_json->'broker'->>'phone', '[^0-9]', '', 'g') AS phone,
         l.first_seen_at, l.last_seen_at
  FROM listings l
  WHERE l.source = 'idnes' AND l.raw_json ? 'broker'
    AND length(regexp_replace(coalesce(l.raw_json->'broker'->>'phone', ''), '[^0-9]', '', 'g')) >= 9
    AND {sel}
)
INSERT INTO broker_identity_contacts (broker_identity_id, source, kind, value, first_seen_at, last_seen_at)
SELECT bi.id, 'idnes', 'phone', c.phone, min(c.first_seen_at), max(c.last_seen_at)
FROM chunk c
JOIN broker_identities bi ON bi.source = 'idnes' AND bi.source_broker_id_native = c.uid
GROUP BY bi.id, c.phone
ON CONFLICT (broker_identity_id, kind, value) DO UPDATE SET
  last_seen_at = greatest(broker_identity_contacts.last_seen_at, EXCLUDED.last_seen_at)
"""

_IDNES_LINK_LISTINGS_IDENTITY = """
UPDATE listings l SET broker_identity_id = bi.id
FROM broker_identities bi
WHERE bi.source = 'idnes' AND bi.source_broker_id_native = (l.raw_json->'broker'->>'account_oid')
  AND l.source = 'idnes' AND l.raw_json ? 'broker'
  AND (l.raw_json->'broker'->>'account_oid') IS NOT NULL
  AND l.broker_identity_id IS DISTINCT FROM bi.id AND {sel}
"""

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

_LINK_LISTINGS_FIRM = """
UPDATE listings l SET broker_firm_id = fi.firm_id
FROM broker_identities bi
JOIN firm_identities fi ON fi.id = bi.firm_identity_id
WHERE l.broker_identity_id = bi.id AND l.broker_firm_id IS DISTINCT FROM fi.firm_id {extra}
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
    count(DISTINCT coalesce(l.property_id, -l.sreality_id)) AS pc,
    count(*) FILTER (WHERE l.is_active AND l.last_seen_at > now() - interval '7 days') AS alc,
    count(DISTINCT coalesce(l.property_id, -l.sreality_id))
      FILTER (WHERE l.is_active AND l.last_seen_at > now() - interval '7 days') AS apc,
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

_BRIDGE_CANDIDATES = """
WITH freq AS (
  SELECT source, kind, value, count(DISTINCT broker_identity_id) AS n
  FROM broker_identity_contacts GROUP BY source, kind, value
),
personal AS (
  SELECT c.broker_identity_id, c.source, c.kind, c.value
  FROM broker_identity_contacts c
  JOIN freq f ON f.source = c.source AND f.kind = c.kind AND f.value = c.value AND f.n = 1
),
multi AS (
  SELECT kind, value FROM personal GROUP BY kind, value HAVING count(DISTINCT source) >= 2
)
SELECT p.broker_identity_id, p.source, p.kind, p.value
FROM personal p JOIN multi m ON m.kind = p.kind AND m.value = p.value
"""

_CLAIM_DIRTY = "SELECT sreality_id FROM dirty_broker_listings WHERE marked_at <= %(cutoff)s ORDER BY marked_at LIMIT %(limit)s"
_DELETE_DIRTY = "DELETE FROM dirty_broker_listings WHERE sreality_id = ANY(%(ids)s) AND marked_at <= %(cutoff)s"
_CLEAR_DIRTY = "DELETE FROM dirty_broker_listings WHERE marked_at <= %(cutoff)s"


def _settings(conn: Any) -> tuple[list[str], list[str], list[str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM app_settings WHERE key = ANY(%s)",
                    ([_FREE_KEY, _FRANCHISE_KEY, _AUTO_MERGE_KEY],))
        rows = {k: v for k, v in cur.fetchall()}
    free = [str(d).lower() for d in (rows.get(_FREE_KEY) or [])]
    franchise = [str(d).lower() for d in (rows.get(_FRANCHISE_KEY) or [])]
    auto = [str(s).lower() for s in (rows.get(_AUTO_MERGE_KEY) or [])]
    return free, franchise, auto


# Pooler-safe mutual exclusion (migration 192). A session pg_advisory_lock is
# unreliable through the transaction-mode pooler, so we claim a single lock row
# instead; the holder heartbeats during a long run and a stale heartbeat lets a
# later run take over after a SIGKILL.
_LOCK_STALE_MIN = 10
_LOCK_POLL_SECONDS = 10
_LOCK_WAIT_MAX_SECONDS = 660  # > the stale window, so a dead holder is always taken over


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
    with conn.cursor() as cur:
        cur.execute("UPDATE broker_resolution_lock SET heartbeat_at=now() WHERE id=1 AND holder=%(h)s",
                    {"h": holder})


def _release_lock(conn: Any, holder: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE broker_resolution_lock SET holder=NULL WHERE id=1 AND holder=%(h)s",
                    {"h": holder})


def _attribute(conn: Any, sel: str, params: dict[str, Any]) -> None:
    """Run per-source attribution (sreality raw_json.user + idnes raw_json.broker)."""
    with conn.cursor() as cur:
        cur.execute(_IDENTITIES_UPSERT.format(sel=sel), params)
        cur.execute(_CONTACTS_EMAIL_UPSERT.format(sel=sel), params)
        cur.execute(_CONTACTS_PHONE_UPSERT.format(sel=sel), params)
        cur.execute(_LINK_LISTINGS_IDENTITY.format(sel=sel), params)
        cur.execute(_IDNES_IDENTITIES_UPSERT.format(sel=sel), params)
        cur.execute(_IDNES_CONTACTS_EMAIL_UPSERT.format(sel=sel), params)
        cur.execute(_IDNES_CONTACTS_PHONE_UPSERT.format(sel=sel), params)
        cur.execute(_IDNES_LINK_LISTINGS_IDENTITY.format(sel=sel), params)


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


def _cross_source_merge(conn: Any, auto_merge_sources: list[str], run_id: int) -> tuple[int, int]:
    """Form corroborated cross-source broker groups and apply reversible merges.

    No-op while only one source is attributed (no cross-source bridges). Review
    pairs are counted only — the operator review queue lands with Phase 5.
    """
    # Cross-source bridges need >=2 sources; with one source the freq scan is
    # guaranteed empty, so skip it entirely (the Phase-1 reality — avoids a costly
    # full-contacts scan). When it does run, lift the statement timeout: it is a
    # once-daily full-table analytical read.
    with conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT source) FROM broker_identities")
        if int(cur.fetchone()[0]) < 2:
            return 0, 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 0")
        cur.execute(_BRIDGE_CANDIDATES)
        rows = cur.fetchall()
    if not rows:
        return 0, 0

    by_value: dict[tuple[str, str], list[tuple[int, str]]] = {}
    identities: dict[int, R.Identity] = {}
    for ident_id, source, kind, value in rows:
        by_value.setdefault((kind, value), []).append((int(ident_id), source))
        identities.setdefault(int(ident_id), R.Identity(int(ident_id), source))
    # Names for corroboration.
    with conn.cursor() as cur:
        cur.execute("SELECT id, display_name FROM broker_identities WHERE id = ANY(%s)",
                    (list(identities),))
        for iid, name in cur.fetchall():
            identities[int(iid)] = R.Identity(int(iid), identities[int(iid)].source, name)

    bridges: list[R.Bridge] = []
    for (kind, value), members in by_value.items():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a[1] != b[1]:
                    bridges.append(R.Bridge(a[0], b[0], kind, value))

    decision = R.decide_merges(list(identities.values()), bridges, auto_merge_sources)
    auto = _apply_merges(conn, decision.auto_merge_groups)
    return auto, len(decision.review_pairs)


def _apply_merges(conn: Any, groups: list[list[int]]) -> int:
    """Unify each group's identities onto one survivor broker, set-based.

    One query fetches every group member's current broker, the plan is built in
    Python (cheap — the groups are union-find output), and the whole thing applies
    in three array-driven statements. Per-group transactions were ~4 pooler
    round-trips each and overran the job timeout once a second source produced
    thousands of merges. Idempotent — a group already sharing one broker is
    skipped, so a re-run after a partial apply converges. Reversible via
    broker_merge_events."""
    if not groups:
        return 0
    ident_ids = sorted({iid for g in groups for iid in g})
    with conn.cursor() as cur:
        cur.execute("SELECT id, broker_id FROM broker_identities WHERE id = ANY(%s)", (ident_ids,))
        broker_of = {int(i): int(b) for i, b in cur.fetchall() if b is not None}

    gids: list[str] = []
    survivors: list[int] = []
    retired: list[int] = []
    idents: list[int] = []
    losers: dict[int, int] = {}
    for group in groups:
        brokers_in = {broker_of[i] for i in group if i in broker_of}
        if len(brokers_in) <= 1:
            continue
        survivor = min(brokers_in)
        gid = str(uuid.uuid4())
        for iid in group:
            prev = broker_of.get(iid)
            if prev is None or prev == survivor:
                continue
            gids.append(gid)
            survivors.append(survivor)
            retired.append(prev)
            idents.append(iid)
            losers[prev] = survivor
    if not idents:
        return 0

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 0")
        cur.execute(
            "INSERT INTO broker_merge_events (merge_group_id, survivor_broker_id, "
            "retired_broker_id, identity_id, prev_broker_id, reason, source) "
            "SELECT g, s, r, i, r, 'contact_bridge', 'auto' "
            "FROM unnest(%(g)s::uuid[], %(s)s::bigint[], %(r)s::bigint[], %(i)s::bigint[]) AS d(g, s, r, i)",
            {"g": gids, "s": survivors, "r": retired, "i": idents},
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
    return len(losers)


def _affected(conn: Any, sids: list[int]) -> list[int]:
    if not sids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT bi.broker_id FROM listings l "
            "JOIN broker_identities bi ON bi.id = l.broker_identity_id "
            "WHERE l.sreality_id = ANY(%s) AND bi.broker_id IS NOT NULL",
            (sids,),
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


def _run_full(conn: Any, free: list[str], franchise: list[str], auto: list[str],
              batch_size: int, deadline: float | None, holder: str = "") -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        cutoff = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO broker_resolution_runs (mode) VALUES ('full') RETURNING id"
        )
        run_id = int(cur.fetchone()[0])
    t0 = time.monotonic()

    # Chunk the ACTUAL listing ids of every broker-bearing source (sreality_id is the
    # sparse PK — a numeric-range loop would walk huge empty gaps). One cheap PK-only
    # scan fetches every id; attribution then runs per id-chunk, source-filtered inside.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sreality_id FROM listings WHERE source IN ('sreality', 'idnes') "
            "ORDER BY sreality_id"
        )
        all_ids = [int(r[0]) for r in cur.fetchall()]
    for i in range(0, len(all_ids), batch_size):
        if holder:
            _heartbeat_lock(conn, holder)
        _attribute(conn, "l.sreality_id = ANY(%(ids)s)", {"ids": all_ids[i:i + batch_size]})
        if deadline and time.monotonic() > deadline:
            LOG.warning("RESOLVE full: time budget reached during attribution at %d/%d ids",
                        i, len(all_ids))
            break

    _resolve_firms(conn, free, franchise)
    # Batch the listings->firm link over the same id chunks — a single global UPDATE
    # joining every linked listing to its firm exceeds the pooler statement timeout
    # now that idnes adds ~125k linkable rows. sreality_id is sparse, so chunk the
    # actual ids (the PR #470 lesson), not a numeric range.
    for i in range(0, len(all_ids), batch_size):
        if holder:
            _heartbeat_lock(conn, holder)
        _link_listings_firm(conn, "AND l.sreality_id = ANY(%(ids)s)", {"ids": all_ids[i:i + batch_size]})
    attached = _attach_singletons(conn)
    LOG.info("RESOLVE full attribution+firms done elapsed=%.1fs", time.monotonic() - t0)

    # Merge BEFORE the rollups so dsc / listing_count / membership reflect the unified
    # broker groupings (a merge re-points broker_identities.broker_id). _cross_source_merge
    # no-ops with one source and is gated per-source by broker_auto_merge_sources.
    if holder:
        _heartbeat_lock(conn, holder)
    auto_merges, queued = _cross_source_merge(conn, auto, run_id)
    LOG.info("RESOLVE full merge done auto=%d queued=%d elapsed=%.1fs",
             auto_merges, queued, time.monotonic() - t0)

    # Rollups batched by our dense serial ids (broker_identity.id / broker.id) — a
    # single global UPDATE over every broker exceeds the pooler statement timeout.
    # Mirrors recompute_property_stats' id-range batching.
    for lo in range(1, _max_id(conn, "broker_identities") + 1, batch_size):
        if holder:
            _heartbeat_lock(conn, holder)
        with conn.cursor() as cur:
            cur.execute(_IDENTITY_ROLLUP.format(
                extra="AND broker_identity_id >= %(lo)s AND broker_identity_id < %(hi)s"),
                {"lo": lo, "hi": lo + batch_size})
    for lo in range(1, _max_id(conn, "brokers") + 1, batch_size):
        if holder:
            _heartbeat_lock(conn, holder)
        with conn.cursor() as cur:
            cur.execute(_BROKER_ROLLUP.format(
                bscope="AND broker_id >= %(lo)s AND broker_id < %(hi)s"),
                {"lo": lo, "hi": lo + batch_size})
            cur.execute(_MEMBERSHIP_RECOMPUTE.format(
                bscope="AND bi.broker_id >= %(lo)s AND bi.broker_id < %(hi)s",
                mscope="m.broker_id >= %(lo)s AND m.broker_id < %(hi)s AND"),
                {"lo": lo, "hi": lo + batch_size})
    # Global firm rollup aggregates the whole linked-listings corpus in one pass;
    # like the matview refresh, lift the statement timeout for this once-per-sweep
    # analytical statement rather than batch it (firms are few, so a firm-id window
    # would just re-scan the same listings).
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 0")
        cur.execute(_FIRM_ROLLUP)
        cur.execute(_FIRM_DISPLAY_NAMES)
    LOG.info("RESOLVE full rollups done elapsed=%.1fs", time.monotonic() - t0)
    _refresh_matview(conn)
    candidates = _generate_merge_candidates(conn)
    LOG.info("RESOLVE full merge candidates proposed=%d elapsed=%.1fs",
             candidates, time.monotonic() - t0)
    with conn.cursor() as cur:
        cur.execute(_CLEAR_DIRTY, {"cutoff": cutoff})
        cur.execute(
            "UPDATE broker_resolution_runs SET ended_at = now(), brokers_recomputed = "
            "(SELECT count(*) FROM brokers WHERE status='active'), identities_upserted = "
            "(SELECT count(*) FROM broker_identities), firms_recomputed = (SELECT count(*) FROM firms), "
            "auto_merges = %s, queued_for_review = %s WHERE id = %s",
            (auto_merges, queued, run_id),
        )
    return {"attached": attached, "auto_merges": auto_merges, "queued": queued}


def _run_incremental(conn: Any, free: list[str], franchise: list[str],
                     batch_size: int) -> dict[str, int]:
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
        sids = {int(r[0]) for r in cur.fetchall()}

    if not sids:
        with conn.cursor() as cur:
            cur.execute("UPDATE broker_resolution_runs SET ended_at = now() WHERE id = %s", (run_id,))
        return {"attributed": 0, "brokers": 0}

    ids = list(sids)
    _attribute(conn, "l.sreality_id = ANY(%(ids)s)", {"ids": ids})
    _resolve_firms(conn, free, franchise)
    _link_listings_firm(conn, "AND l.sreality_id = ANY(%(ids)s)", {"ids": ids})
    _attach_singletons(conn)
    bids = _affected(conn, ids)
    with conn.cursor() as cur:
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
    return {"attributed": len(ids), "brokers": len(bids)}


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

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    import psycopg

    mode = "incremental" if args.incremental else "full"
    LOG.info("RESOLVE config mode=%s batch_size=%d", mode, args.batch_size)
    started = time.monotonic()
    deadline = started + args.max_seconds if args.max_seconds else None

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        free, franchise, auto = _settings(conn)
        if args.dry_run:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM dirty_broker_listings")
                dirty = int(cur.fetchone()[0])
            LOG.info("RESOLVE dry-run mode=%s free=%d franchise=%d dirty=%d; exit",
                     mode, len(free), len(franchise), dirty)
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
            if args.incremental:
                res = _run_incremental(conn, free, franchise, args.batch_size)
                LOG.info("RESOLVE incremental done attributed=%d brokers=%d elapsed=%.1fs",
                         res["attributed"], res["brokers"], time.monotonic() - started)
            else:
                res = _run_full(conn, free, franchise, auto, args.batch_size, deadline, holder)
                LOG.info("RESOLVE full done attached=%d auto_merges=%d queued=%d elapsed=%.1fs",
                         res["attached"], res["auto_merges"], res["queued"], time.monotonic() - started)
        finally:
            _release_lock(conn, holder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
