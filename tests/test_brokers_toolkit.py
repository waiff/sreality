"""toolkit.brokers query shape + PII policy — through a fake cursor, no DB.

The route tests mock the toolkit away, so the WHERE clause is exactly what they
cannot see: a lookup keyed on sreality_id silently returns nothing for the eight
portals that insert NULL there, which reads as "unattributed", not as a bug.
"""

from __future__ import annotations

from typing import Any

import pytest

from toolkit import brokers


class _Cur:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.seen = rows, []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.seen.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.cur = _Cur(rows or [])

    def cursor(self, **kw: Any) -> _Cur:
        return self.cur


def test_listing_broker_prefers_the_surrogate_id() -> None:
    conn = _Conn([{"broker_id": 4, "listing_id": 88}])
    out = brokers.listing_broker(conn, 123, listing_id=88)
    sql, params = conn.cur.seen[0]
    assert "WHERE listing_id = %s" in sql and params == (88,)
    assert out is not None and out["metadata"]["tool"] == "listing_broker"


def test_listing_broker_falls_back_to_sreality_id() -> None:
    conn = _Conn([{"broker_id": 4}])
    brokers.listing_broker(conn, 123)
    sql, params = conn.cur.seen[0]
    assert "WHERE sreality_id = %s" in sql and params == (123,)


def test_listing_broker_without_any_id_raises() -> None:
    with pytest.raises(ValueError):
        brokers.listing_broker(_Conn())


def test_listing_brokers_dedupes_and_skips_the_query_when_empty() -> None:
    conn = _Conn([{"listing_id": 7}])
    out = brokers.listing_brokers(conn, [9, 7, 9])
    assert conn.cur.seen[0][1] == ([7, 9],)
    assert out["metadata"]["filters_used"]["listing_ids"] == [7, 9]

    empty = _Conn()
    assert brokers.listing_brokers(empty, [])["data"] == []
    assert empty.cur.seen == []


def test_batch_reads_are_bounded() -> None:
    conn = _Conn()
    brokers.brokers_by_ids(conn, list(range(5000)))
    assert len(conn.cur.seen[0][1][0]) == 1000


def test_geo_options_rejects_an_unknown_level() -> None:
    """Falling back to "no filter" answered `obec` with every region AND okres."""
    with pytest.raises(ValueError):
        brokers.geo_options(_Conn(), geo_level="planet")


def test_geo_options_without_a_level_returns_every_level() -> None:
    conn = _Conn([{"geo_level": "region", "geo_id": 27}])
    out = brokers.geo_options(conn)
    assert conn.cur.seen[0][1] == (None, None)
    assert out["metadata"]["filters_used"]["geo_level"] is None


def test_search_ranks_on_the_cz_scoped_count() -> None:
    """D4 / migration 396. `brokers.active_property_count` counts foreign
    syndication: two idnes feeds (ibero-casa.com 15,028 active, a Croatian one
    5,007) led every name search they matched, 8x the busiest genuinely Czech
    broker at 1,862. Ranking moves to the CZ-scoped column; NOTHING is filtered
    out, so a foreign-heavy broker is still findable and still shows its whole
    book in the unscoped columns the same `select *` returns."""
    conn = _Conn([{"broker_id": 1}])
    brokers.search(conn, "novak")
    sql, params = conn.cur.seen[0]
    assert "ORDER BY cz_active_property_count DESC NULLS LAST" in sql
    assert "ORDER BY active_property_count" not in sql
    # Still a plain `select *` over the whole row — no column was dropped.
    assert sql.startswith("SELECT * FROM brokers_public")
    assert "WHERE" in sql and params == ("%novak%", 12)


def test_search_still_short_circuits_a_one_character_query() -> None:
    conn = _Conn()
    assert brokers.search(conn, "a")["data"] == []
    assert conn.cur.seen == []


def test_policy_masks_a_dossier_but_leaves_an_admin_alone() -> None:
    envelope = {
        "data": {"broker": {"broker_id": 1, "display_name": "RK Alfa",
                            "primary_email": "a@b.cz", "primary_phone": None},
                 "memberships": [{"firm_id": 3, "firm_domain": "alfa.cz"}],
                 "contacts": [{"kind": "email", "value": "a@b.cz"}]},
        "metadata": {"tool": "broker_detail"},
    }
    masked = brokers.apply_pii_policy(envelope, include_pii=False)
    assert masked["data"]["broker"] == {"broker_id": 1, "display_name": "RK Alfa",
                                        "has_email": True, "has_phone": False}
    assert "contacts" not in masked["data"]
    assert masked["data"]["memberships"] == [{"firm_id": 3, "firm_domain": "alfa.cz"}]
    assert masked["metadata"] == {"tool": "broker_detail", "pii_masked": True}
    # the source envelope is not mutated — the admin path must still see everything
    assert envelope["data"]["broker"]["primary_email"] == "a@b.cz"

    full = brokers.apply_pii_policy(envelope, include_pii=True)
    assert full["data"] == envelope["data"]
    assert full["metadata"]["pii_masked"] is False


def test_policy_redacts_a_contact_hiding_under_a_non_contact_column() -> None:
    """The name rule can't see PII under a name it doesn't recognise, and two live
    brokers' display_name IS their email address — `pii_masked: true` must not be a
    stronger promise than what the mask delivers."""
    out = brokers.apply_pii_policy(
        {"data": [{"broker_id": 1, "display_name": "jan.novak@rk.cz"},
                  {"broker_id": 2, "display_name": "+420 777 123 456"},
                  {"broker_id": 3, "display_name": "Jan Novák (jan@rk.cz)"}],
         "metadata": {}}, include_pii=False)
    assert [r["display_name"] for r in out["data"]] == [
        "[redacted]", "[redacted]", "Jan Novák ([redacted])"]


def test_policy_leaves_a_url_and_a_timestamp_intact() -> None:
    """A whole-string-only phone rule: source_url carries a 9-digit listing id."""
    url = "https://www.sreality.cz/detail/prodej/byt/praha/123456789"
    out = brokers.apply_pii_policy(
        {"data": [{"source_url": url, "last_seen_at": "2026-08-12T09:00:00+00:00",
                   "locality": "Praha 8 - Libeň", "area_m2": 74.5}],
         "metadata": {}}, include_pii=False)
    assert out["data"][0] == {"source_url": url, "locality": "Praha 8 - Libeň",
                              "last_seen_at": "2026-08-12T09:00:00+00:00",
                              "area_m2": 74.5}


def test_policy_leaves_an_admin_envelope_untouched_by_the_shape_rule() -> None:
    envelope = {"data": [{"display_name": "jan.novak@rk.cz"}], "metadata": {}}
    assert brokers.apply_pii_policy(
        envelope, include_pii=True)["data"][0]["display_name"] == "jan.novak@rk.cz"


def test_policy_masks_a_contact_column_no_denylist_would_know() -> None:
    """The views behind these reads are read `select *` and have been widened by
    migration twice already, so the rule is the column NAME, not a fixed list."""
    out = brokers.apply_pii_policy(
        {"data": [{"broker_id": 1, "secondary_email": "x@y.cz", "mobile_phone": None}],
         "metadata": {}}, include_pii=False)
    assert out["data"] == [{"broker_id": 1, "has_email": True, "has_phone": False}]
