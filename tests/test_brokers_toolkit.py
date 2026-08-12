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


def test_geo_options_ignores_an_unknown_level() -> None:
    conn = _Conn([{"geo_level": "region", "geo_id": 27}])
    out = brokers.geo_options(conn, geo_level="planet")
    assert conn.cur.seen[0][1] == (None, None)
    assert out["metadata"]["filters_used"]["geo_level"] is None


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


def test_policy_masks_a_contact_column_no_denylist_would_know() -> None:
    """The views behind these reads are read `select *` and have been widened by
    migration twice already, so the rule is the column NAME, not a fixed list."""
    out = brokers.apply_pii_policy(
        {"data": [{"broker_id": 1, "secondary_email": "x@y.cz", "mobile_phone": None}],
         "metadata": {}}, include_pii=False)
    assert out["data"] == [{"broker_id": 1, "has_email": True, "has_phone": False}]
