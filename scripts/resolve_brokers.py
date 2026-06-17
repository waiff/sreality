"""Broker identity resolver — the decoupled job behind broker intelligence (phase 1).

Mirrors scripts.recompute_property_stats: a pure-SQL, set-based, idempotent driver
that runs OFF the scrape hot path. Three modes:

  * --incremental (cron */10): attribute unattributed-straggler + dirty listings
    (cheap, scoped), then recompute only the affected brokers' rollups + firm
    memberships. O(changes); never touches the leaderboard matview. The expensive
    raw_json scan is avoided here.
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
_STRAGGLERS = """
SELECT sreality_id FROM listings
WHERE source = 'sreality' AND broker_identity_id IS NULL AND raw_json ? 'user'
  AND (raw_json->'user'->>'user_id') IS NOT NULL
LIMIT %(limit)s
"""


def _settings(conn: Any) -> tuple[list[str], list[str], list[str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM app_settings WHERE key = ANY(%s)",
                    ([_FREE_KEY, _FRANCHISE_KEY, _AUTO_MERGE_KEY],))
        rows = {k: v for k, v in cur.fetchall()}
    free = [str(d).lower() for d in (rows.get(_FREE_KEY) or [])]
    franchise = [str(d).lower() for d in (rows.get(_FRANCHISE_KEY) or [])]
    auto = [str(s).lower() for s in (rows.get(_AUTO_MERGE_KEY) or [])]
    return free, franchise, auto


def _attribute(conn: Any, sel: str, params: dict[str, Any]) -> None:
    """Run the four sreality attribution statements for a listings selector."""
    with conn.cursor() as cur:
        cur.execute(_IDENTITIES_UPSERT.format(sel=sel), params)
        cur.execute(_CONTACTS_EMAIL_UPSERT.format(sel=sel), params)
        cur.execute(_CONTACTS_PHONE_UPSERT.format(sel=sel), params)
        cur.execute(_LINK_LISTINGS_IDENTITY.format(sel=sel), params)


def _resolve_firms(conn: Any, free: list[str], franchise: list[str], *, link_listings_extra: str = "",
                   listings_params: dict[str, Any] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(_FIRMS_UPSERT, {"free": free, "franchise": franchise})
        cur.execute(_FIRM_IDENTITIES_UPSERT, {"free": free})
        cur.execute(_LINK_IDENTITY_FIRM)
        cur.execute(_LINK_LISTINGS_FIRM.format(extra=link_listings_extra), listings_params or {})


def _attach_singletons(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute(_SINGLETON_ATTACH)
        return cur.rowcount or 0


def _cross_source_merge(conn: Any, auto_merge_sources: list[str], run_id: int) -> tuple[int, int]:
    """Form corroborated cross-source broker groups and apply reversible merges.

    No-op while only one source is attributed (no cross-source bridges). Review
    pairs are counted only — the operator review queue lands with Phase 5.
    """
    with conn.cursor() as cur:
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
    auto = 0
    for group in decision.auto_merge_groups:
        auto += _apply_merge(conn, group, run_id)
    return auto, len(decision.review_pairs)


def _apply_merge(conn: Any, identity_ids: list[int], run_id: int) -> int:
    """Unify the brokers of these identities onto one survivor; reversible ledger."""
    group = str(uuid.uuid4())
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT id, broker_id FROM broker_identities WHERE id = ANY(%s) ORDER BY id",
            (identity_ids,),
        )
        rows = cur.fetchall()
        broker_of = {int(i): int(b) for i, b in rows if b is not None}
        if len(set(broker_of.values())) <= 1:
            return 0
        survivor = min(broker_of.values())
        for ident_id, prev_broker in broker_of.items():
            if prev_broker == survivor:
                continue
            cur.execute(
                "INSERT INTO broker_merge_events (merge_group_id, survivor_broker_id, "
                "retired_broker_id, identity_id, prev_broker_id, reason, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'auto')",
                (group, survivor, prev_broker, ident_id, prev_broker, "contact_bridge"),
            )
            cur.execute("UPDATE broker_identities SET broker_id = %s WHERE id = %s", (survivor, ident_id))
        cur.execute(
            "UPDATE brokers SET status = 'merged_away', merged_into = %s, merged_at = now() "
            "WHERE id = ANY(%s) AND id <> %s",
            (survivor, list(set(broker_of.values())), survivor),
        )
    return len(set(broker_of.values())) - 1


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


def _refresh_matview(conn: Any) -> None:
    with conn.cursor() as cur:
        try:
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY broker_region_type_stats")
        except Exception:  # noqa: BLE001 — first refresh on an unpopulated matview
            cur.execute("REFRESH MATERIALIZED VIEW broker_region_type_stats")


def _run_full(conn: Any, free: list[str], franchise: list[str], auto: list[str],
              batch_size: int, deadline: float | None) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        cutoff = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO broker_resolution_runs (mode) VALUES ('full') RETURNING id"
        )
        run_id = int(cur.fetchone()[0])

    # Chunk the ACTUAL sreality listing ids (sreality_id is the portal's sparse id —
    # 37k..4.3bn — so a numeric-range loop would walk ~859k empty ranges). One cheap
    # PK-only scan fetches every id; attribution then runs per id-chunk.
    with conn.cursor() as cur:
        cur.execute("SELECT sreality_id FROM listings WHERE source = 'sreality' ORDER BY sreality_id")
        all_ids = [int(r[0]) for r in cur.fetchall()]
    for i in range(0, len(all_ids), batch_size):
        _attribute(conn, "l.sreality_id = ANY(%(ids)s)", {"ids": all_ids[i:i + batch_size]})
        if deadline and time.monotonic() > deadline:
            LOG.warning("RESOLVE full: time budget reached during attribution at %d/%d ids",
                        i, len(all_ids))
            break

    _resolve_firms(conn, free, franchise)
    attached = _attach_singletons(conn)
    with conn.cursor() as cur:
        cur.execute(_IDENTITY_ROLLUP.format(extra=""))
        cur.execute(_BROKER_ROLLUP.format(bscope=""))
        cur.execute(_MEMBERSHIP_RECOMPUTE.format(bscope="", mscope=""))
        cur.execute(_FIRM_ROLLUP)
    auto_merges, queued = _cross_source_merge(conn, auto, run_id)
    _refresh_matview(conn)
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
        cur.execute(_STRAGGLERS, {"limit": batch_size})
        sids = {int(r[0]) for r in cur.fetchall()}
        cur.execute(_CLAIM_DIRTY, {"cutoff": cutoff, "limit": batch_size})
        sids |= {int(r[0]) for r in cur.fetchall()}

    if not sids:
        with conn.cursor() as cur:
            cur.execute("UPDATE broker_resolution_runs SET ended_at = now() WHERE id = %s", (run_id,))
        return {"attributed": 0, "brokers": 0}

    ids = list(sids)
    _attribute(conn, "l.sreality_id = ANY(%(ids)s)", {"ids": ids})
    _resolve_firms(conn, free, franchise, link_listings_extra="AND l.sreality_id = ANY(%(ids)s)",
                   listings_params={"ids": ids})
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
                cur.execute("SELECT count(*) FROM listings WHERE source='sreality' "
                            "AND broker_identity_id IS NULL AND raw_json ? 'user'")
                stragglers = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM dirty_broker_listings")
                dirty = int(cur.fetchone()[0])
            LOG.info("RESOLVE dry-run mode=%s free=%d franchise=%d stragglers=%d dirty=%d; exit",
                     mode, len(free), len(franchise), stragglers, dirty)
            return 0

        if args.incremental:
            res = _run_incremental(conn, free, franchise, args.batch_size)
            LOG.info("RESOLVE incremental done attributed=%d brokers=%d elapsed=%.1fs",
                     res["attributed"], res["brokers"], time.monotonic() - started)
        else:
            res = _run_full(conn, free, franchise, auto, args.batch_size, deadline)
            LOG.info("RESOLVE full done attached=%d auto_merges=%d queued=%d elapsed=%.1fs",
                     res["attached"], res["auto_merges"], res["queued"], time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
