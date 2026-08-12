"""Read-only broker intelligence queries (leaderboard, detail, contacts, listings).

Mirrors the browser read layer (frontend/src/lib/brokers.ts) server-side so the
agent, API consumers, and outreach (Phase 4) all hit the SAME public views +
broker_leaderboard RPC — one definition of "who has what". Identity-gated routes
live in api/routes/brokers.py. A broker's full contact set is NOT exposed by the
anon public views (PII), so broker_contacts here is the only path to it —
`apply_pii_policy` is what the route layer runs over every other envelope so a
non-admin caller gets has_email / has_phone flags instead of the values.

Read-only — no toolkit write exception (rule #5) is added.
"""

from __future__ import annotations

import re
from typing import Any

from psycopg.rows import dict_row

from toolkit import _listing_id_clause, _now_iso

_VALID_METRICS = {
    "active_property_count", "property_count", "listing_count", "active_listing_count",
}
GEO_LEVELS = ("region", "okres")
MAX_BATCH = 1000
_REDACTED = "[redacted]"
_EMAIL_RE = re.compile(r"[^@\s<>,;()\[\]\"']+@[^@\s<>,;()\[\]\"']+\.[a-z]{2,}",
                       re.IGNORECASE)
_PHONE_RE = re.compile(r"^\+?\d[\d\s./()-]{7,17}$")


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


def _bounded(ids: list[int]) -> list[int]:
    """Deduped, sorted, capped. The HTTP layer rejects an over-cap batch outright
    (both batch routes bound the input); this slice is only the backstop for a
    direct toolkit/agent caller, which must never silently spill into a huge IN."""
    return sorted({int(i) for i in ids})[:MAX_BATCH]


def apply_pii_policy(envelope: dict[str, Any], *, include_pii: bool) -> dict[str, Any]:
    """Keep or mask broker contact PII, always stamping metadata.pii_masked.

    D1/D2 (2026-08-12 broker E2E review): these reads moved off the static shared
    secret — which ships inside the SPA bundle — onto real user identity, so an
    ordinary logged-in caller must not receive 2000 brokers' email + phone.
    """
    masked = envelope if include_pii else {**envelope, "data": _mask(envelope.get("data"))}
    return {**masked,
            "metadata": {**(masked.get("metadata") or {}), "pii_masked": not include_pii}}


def _pii_kind(key: str) -> str | None:
    lowered = key.lower()
    return next((k for k in ("email", "phone") if k in lowered), None)


def _redact_shaped(text: str) -> str:
    """Shape rule under the name rule: contact PII also arrives under non-contact
    columns — two live brokers' display_name IS their email address, and the portal
    name field is free text — so a name-only mask promises more than it delivers.
    A phone is matched whole-string only, or a source_url's numeric id would go."""
    if _PHONE_RE.match(text) and sum(c.isdigit() for c in text) >= 9:
        return _REDACTED
    return _EMAIL_RE.sub(_REDACTED, text)


def _like_escape(term: str) -> str:
    """`%` and `_` inside a BOUND LIKE value are still wildcards — psycopg passes
    the value as data and LIKE, correctly, interprets what is in it. Unescaped,
    `@_` was a one-character probe that walked straight under search()'s two-char
    minimum, and a bare `%` seq-scanned brokers_public on every request. Backslash
    is LIKE's own default escape character, so no ESCAPE clause is needed (and none
    is written: a literal backslash in the SQL TEXT would depend on
    standard_conforming_strings, while the bound value never does)."""
    for ch in ("\\", "%", "_"):
        term = term.replace(ch, "\\" + ch)
    return term


def _matches_masked(display_name: Any, term: str) -> bool:
    """Does `term` still match once the row is masked the way it will be returned?

    A non-admin's PREDICATE must not see what their PROJECTION hides, or search is
    an oracle: probe, and the presence of a row (with its broker_id) confirms the
    guess, recovering an email `_redact_shaped` redacted one character at a time.
    Filtering on the masked text — the same function that builds the response —
    keeps every ordinary name findable (only the redacted SPAN stops matching, so
    "Kancelář Honzík <info@honzik.cz>" is still found by "Honzík") and generalises
    to whatever the shape rule learns to redact next."""
    if not isinstance(display_name, str):
        return False
    return term.casefold() in _redact_shaped(display_name).casefold()


def _mask(value: Any) -> Any:
    """Swap every contact column for a has_* flag, recursing into the dossier.

    Matched on the column NAME, not a fixed list of today's columns: these queries
    are `select *` over views a migration can widen (broker_listings_public has
    already grown twice), so a denylist would leak a new *_email the day it lands.
    The dossier's contact list carries its PII in a generic `value` column no name
    rule can catch, so it is dropped whole — /brokers/{id}/contacts is admin-only.
    `has_email` / `has_phone` mean "a current primary contact is on file", i.e. the
    brokers rollup's primary_email / primary_phone, not every address ever seen —
    and primary_email is picked from the most-recently-seen IDENTITY while
    primary_phone is a max over all of a broker's identities, so a group spanning an
    email-less source can report has_email=false while the dossier still holds the
    address. Firm identifiers (firm_name, firm_domain) are deliberately NOT masked:
    a company's web domain is a business identifier printed on every listing page,
    so `pii_masked` promises that CONTACT VALUES are masked, not that nothing about
    the row is attributable.
    """
    if isinstance(value, list):
        return [_mask(v) for v in value]
    if isinstance(value, str):
        return _redact_shaped(value)
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    flags: dict[str, bool] = {}
    for key, val in value.items():
        if key == "contacts":
            continue
        kind = _pii_kind(key)
        if kind is None:
            out[key] = _mask(val)
        else:
            flags[kind] = flags.get(kind, False) or bool(val)
    for kind, present in flags.items():
        out[f"has_{kind}"] = present
    return out


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


def search(conn: Any, query: str, *, limit: int = 12,
           include_pii: bool = False) -> dict[str, Any]:
    """Brokers whose display name matches `query` (>=2 chars), busiest first.

    Ranked on the CZ-scoped count (migration 396), like every other broker
    ranking: two idnes syndication feeds carry ~26k foreign listings between them
    and would otherwise head the results for any query they matched. Both counts
    are returned, so the row still shows the broker's whole book.

    `include_pii` is the caller's identity, not a formatting flag: without it the
    ILIKE ran over the RAW display_name while the response redacted it, so a
    non-admin could binary-search the redacted content back out. Defaults closed,
    so an agent or a new route gets the masked predicate unless it says otherwise.
    """
    term = (query or "").strip()
    limit = max(1, min(int(limit), 100))
    if len(term) < 2:
        return _envelope("broker_search", [], {"query": term, "limit": limit}, 0, None)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM brokers_public WHERE display_name ILIKE %s "
            "ORDER BY cz_active_property_count DESC NULLS LAST LIMIT %s",
            (f"%{_like_escape(term)}%", limit))
        rows = cur.fetchall()
    if not include_pii:
        rows = [r for r in rows if _matches_masked(r.get("display_name"), term)]
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


def listing_broker(conn: Any, sreality_id: int | None = None, *,
                   listing_id: int | None = None) -> dict[str, Any] | None:
    """The broker behind one listing (listing_broker_public), or None if unattributed.

    Addressable by EITHER id, surrogate wins (`_listing_id_clause`): sreality_id is
    NULL on the eight non-sreality portals, so a sreality-keyed lookup silently
    found nothing for most of the corpus.
    """
    id_clause, id_val = _listing_id_clause(sreality_id, listing_id, lid_col="listing_id")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT * FROM listing_broker_public WHERE {id_clause}", (id_val,))
        row = cur.fetchone()
    if row is None:
        return None
    return _envelope("listing_broker", row,
                     {"sreality_id": sreality_id, "listing_id": listing_id}, 1, None)


def listing_brokers(conn: Any, listing_ids: list[int]) -> dict[str, Any]:
    """The brokers behind many listings in one round-trip, keyed on the surrogate
    listing_id — the board/table hydration path that would otherwise be an N+1."""
    ids = _bounded(listing_ids)
    rows: list[dict[str, Any]] = []
    if ids:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM listing_broker_public WHERE listing_id = ANY(%s)", (ids,))
            rows = cur.fetchall()
    return _envelope("listing_brokers", rows, {"listing_ids": ids}, len(rows), None)


def brokers_by_ids(conn: Any, broker_ids: list[int]) -> dict[str, Any]:
    """Canonical broker rows for an explicit id set (the contact-box hydration
    pair to listing_brokers)."""
    ids = _bounded(broker_ids)
    rows: list[dict[str, Any]] = []
    if ids:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM brokers_public WHERE broker_id = ANY(%s)", (ids,))
            rows = cur.fetchall()
    fresh = max((r["last_seen_at"] for r in rows if r.get("last_seen_at")), default=None)
    for r in rows:
        r["first_seen_at"], r["last_seen_at"] = _iso(r.get("first_seen_at")), _iso(r.get("last_seen_at"))
    return _envelope("brokers_by_ids", rows, {"broker_ids": ids}, len(rows), _iso(fresh))


def geo_options(conn: Any, *, geo_level: str | None = None) -> dict[str, Any]:
    """Region / okres picker metadata with per-area broker counts.

    Not PII, but broker_geo_options is dark to anon AND authenticated (migration
    361), so the server-side route is the only way a browser COULD read it — no
    browser caller exists today: BrokerDetail's region-name map went away when the
    dossier started joining region_shares[].name, and the leaderboard page scopes
    itself through the shared LocationTypeahead. Kept as the capability, not as a
    live SPA path. An unrecognized level raises rather than falling back to "no
    filter" — silently answering `geo_level=obec` with every region AND okres is
    worse than a 422."""
    if geo_level is not None and geo_level not in GEO_LEVELS:
        raise ValueError(f"geo_level must be one of {sorted(GEO_LEVELS)}")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT geo_level, geo_id, name, parent_id, broker_count "
            "FROM broker_geo_options WHERE (%s::text IS NULL OR geo_level = %s) "
            "ORDER BY geo_level, name", (geo_level, geo_level))
        rows = cur.fetchall()
    return _envelope("broker_geo_options", rows, {"geo_level": geo_level}, len(rows), None)


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
