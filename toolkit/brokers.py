"""Read-only broker intelligence queries (leaderboard, detail, contacts, listings).

The SAME public views + broker_leaderboard RPC the agent, API consumers, and
outreach (Phase 4) all hit — one definition of "who has what". Every broker-*
relation is revoked from `anon`/`authenticated` (Phase 0 Amendment A6, migration
299 — broker PII stays dark to non-admin sessions until Wave 4 ships masked
columns), so this module's `conn` is always a privileged (postgres/service-role)
connection. Bearer-gated (`require_admin`) routes live in api/routes/brokers.py;
frontend/src/lib/brokers.ts is a thin client over those routes, not a second
implementation of these reads.

Read-only — no toolkit write exception (rule #5) is added.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from toolkit import _now_iso

_VALID_METRICS = {
    "active_property_count", "property_count", "listing_count", "active_listing_count",
}


def _envelope(tool: str, data: Any, filters_used: dict[str, Any], result_count: int,
              data_freshness: str | None) -> dict[str, Any]:
    return {
        "data": data,
        "metadata": {
            "tool": tool,
            "filters_used": filters_used,
            "result_count": result_count,
            "queried_at": _now_iso(),
            "data_freshness": data_freshness,
        },
    }


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def leaderboard(conn: Any, *, region_ids: list[int] | None = None,
                okres_ids: list[int] | None = None, obec_ids: list[int] | None = None,
                category_main: str | None = None, category_type: str | None = None,
                metric: str = "active_property_count", limit: int = 100) -> dict[str, Any]:
    """Top brokers by a chosen metric, optionally scoped to admin regions + category.

    Thin wrapper over the broker_leaderboard RPC (the same one Browse calls), so the
    agent and Browse never disagree on the ranking. Empty id arrays = national.
    """
    if metric not in _VALID_METRICS:
        metric = "active_property_count"
    limit = max(1, min(int(limit), 2000))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM broker_leaderboard(%s, %s, %s, %s, %s, %s, %s)",
            (region_ids or None, okres_ids or None, obec_ids or None,
             category_main, category_type, metric, limit))
        rows = cur.fetchall()
    return _envelope(
        "broker_leaderboard", rows,
        {"region_ids": region_ids or [], "okres_ids": okres_ids or [],
         "obec_ids": obec_ids or [], "category_main": category_main,
         "category_type": category_type, "metric": metric, "limit": limit},
        len(rows), None)


def search(conn: Any, query: str, *, limit: int = 12) -> dict[str, Any]:
    """Brokers whose display name matches `query` (>=2 chars), busiest first."""
    term = (query or "").strip()
    limit = max(1, min(int(limit), 100))
    if len(term) < 2:
        return _envelope("broker_search", [], {"query": term, "limit": limit}, 0, None)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM brokers_public WHERE display_name ILIKE %s "
            "ORDER BY active_property_count DESC NULLS LAST LIMIT %s",
            (f"%{term}%", limit))
        rows = cur.fetchall()
    fresh = max((r["last_seen_at"] for r in rows if r.get("last_seen_at")), default=None)
    for r in rows:
        r["first_seen_at"], r["last_seen_at"] = _iso(r.get("first_seen_at")), _iso(r.get("last_seen_at"))
    return _envelope("broker_search", rows, {"query": term, "limit": limit}, len(rows), _iso(fresh))


def get_broker(conn: Any, broker_id: int) -> dict[str, Any] | None:
    """Full broker dossier: identity row + firm memberships + regional footprint +
    every distinct contact. Returns None if the broker id is unknown / merged away."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM brokers_public WHERE broker_id = %s", (broker_id,))
        broker = cur.fetchone()
        if broker is None:
            return None
        cur.execute(
            "SELECT * FROM broker_firm_memberships_public WHERE broker_id = %s "
            "ORDER BY last_seen_at DESC NULLS LAST", (broker_id,))
        memberships = cur.fetchall()
        cur.execute(
            "SELECT s.geo_id, o.name, "
            "  sum(s.property_count)::bigint AS property_count, "
            "  sum(s.active_property_count)::bigint AS active_property_count, "
            "  sum(s.listing_count)::bigint AS listing_count "
            "FROM broker_region_type_stats s "
            "LEFT JOIN broker_geo_options o ON o.geo_level='region' AND o.geo_id=s.geo_id "
            "WHERE s.broker_id = %s AND s.geo_level='region' "
            "GROUP BY s.geo_id, o.name ORDER BY active_property_count DESC", (broker_id,))
        region_shares = cur.fetchall()
        contacts = _contacts(cur, broker_id)
    for coll in (broker, *memberships):
        for k in ("first_seen_at", "last_seen_at"):
            if k in coll:
                coll[k] = _iso(coll[k])
    data = {"broker": broker, "memberships": memberships,
            "region_shares": region_shares, "contacts": contacts}
    return _envelope("broker_detail", data, {"broker_id": broker_id}, 1, broker.get("last_seen_at"))


def broker_listings(conn: Any, broker_id: int, *, limit: int = 500) -> dict[str, Any]:
    """A broker's listings (active first), via broker_listings_public."""
    limit = max(1, min(int(limit), 2000))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM broker_listings_public WHERE broker_id = %s "
            "ORDER BY is_active DESC, last_seen_at DESC NULLS LAST LIMIT %s",
            (broker_id, limit))
        rows = cur.fetchall()
    fresh = max((r["last_seen_at"] for r in rows if r.get("last_seen_at")), default=None)
    for r in rows:
        r["last_seen_at"] = _iso(r.get("last_seen_at"))
    return _envelope("broker_listings", rows, {"broker_id": broker_id, "limit": limit},
                     len(rows), _iso(fresh))


def listing_broker(conn: Any, listing_id: int) -> dict[str, Any] | None:
    """The broker behind one listing (listing_broker_public), or None if unattributed.

    Keyed on the surrogate `listing_id` (migration 343), not `sreality_id` — a
    post-Gate-2 non-sreality listing has a NULL sreality_id, so a sreality-keyed
    lookup would silently find nothing for it.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM listing_broker_public WHERE listing_id = %s", (listing_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return _envelope("listing_broker", row, {"listing_id": listing_id}, 1, None)


def listing_brokers_by_ids(conn: Any, listing_ids: list[int]) -> dict[str, Any]:
    """Batched listing->broker lookup (listing_broker_public), for N-card hydration
    (Pipeline board, Browse cards) without an N+1 round trip per card."""
    if not listing_ids:
        return _envelope("listing_brokers_by_ids", [], {"listing_ids": []}, 0, None)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM listing_broker_public WHERE listing_id = ANY(%s)",
            (listing_ids,))
        rows = cur.fetchall()
    return _envelope("listing_brokers_by_ids", rows, {"listing_ids": listing_ids}, len(rows), None)


def brokers_by_ids(conn: Any, broker_ids: list[int]) -> dict[str, Any]:
    """Batched broker-contact lookup (brokers_public), pairs with
    listing_brokers_by_ids to fill N cards' hover contact boxes in one round trip."""
    if not broker_ids:
        return _envelope("brokers_by_ids", [], {"broker_ids": []}, 0, None)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM brokers_public WHERE broker_id = ANY(%s)", (broker_ids,))
        rows = cur.fetchall()
    return _envelope("brokers_by_ids", rows, {"broker_ids": broker_ids}, len(rows), None)


def broker_contacts(conn: Any, broker_id: int) -> dict[str, Any]:
    """Every distinct (kind, value) contact across a broker's identities — the full
    reachable set for outreach. PII; this is not exposed by the anon public views."""
    with conn.cursor(row_factory=dict_row) as cur:
        contacts = _contacts(cur, broker_id)
    return _envelope("broker_contacts", contacts, {"broker_id": broker_id}, len(contacts), None)


def _contacts(cur: Any, broker_id: int) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT c.kind, c.value, array_agg(DISTINCT c.source ORDER BY c.source) AS sources, "
        "  max(c.last_seen_at) AS last_seen_at "
        "FROM broker_identity_contacts c "
        "JOIN broker_identities bi ON bi.id = c.broker_identity_id "
        "WHERE bi.broker_id = %s "
        "GROUP BY c.kind, c.value ORDER BY c.kind, max(c.last_seen_at) DESC", (broker_id,))
    rows = cur.fetchall()
    for r in rows:
        r["last_seen_at"] = _iso(r.get("last_seen_at"))
    return rows
