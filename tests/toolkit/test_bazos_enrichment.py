"""Tests for the pure mapping/normalization in toolkit.bazos_enrichment and the
selection SQL in scripts.enrich_listing_descriptions (no DB / no LLM needed)."""

from __future__ import annotations

from typing import Any

from toolkit.bazos_enrichment import (
    _norm_building_type,
    _norm_condition,
    _norm_energy,
    columns_from_extraction,
)


def _env(value: Any, confidence: str = "high") -> dict[str, Any]:
    return {"value": value, "confidence": confidence}


_EMPTY = {c: None for c in (
    "floor", "total_floors", "has_balcony", "has_lift", "has_parking",
    "building_type", "condition", "energy_rating",
)}


def test_fills_gap_columns_high_confidence():
    extraction = {
        "floor": _env(3),
        "has_balcony": _env(True),
        "has_lift": _env(False),
        "building_type": _env("Cihla"),
        "condition": _env("velmi dobrý stav"),
        "energy_rating": _env("g"),
    }
    out = columns_from_extraction(extraction, dict(_EMPTY))
    assert out == {
        "floor": 3,
        "has_balcony": True,
        "has_lift": False,
        "building_type": "cihla",
        "condition": "velmi_dobry",
        "energy_rating": "G",
    }


def test_low_confidence_is_dropped():
    out = columns_from_extraction({"floor": _env(3, "low")}, dict(_EMPTY))
    assert out == {}


def test_null_value_is_dropped():
    out = columns_from_extraction({"has_lift": _env(None)}, dict(_EMPTY))
    assert out == {}


def test_never_overwrites_present_column():
    current = dict(_EMPTY, floor=2, condition="dobry")
    extraction = {"floor": _env(5), "condition": _env("novostavba"), "has_lift": _env(True)}
    out = columns_from_extraction(extraction, current)
    assert out == {"has_lift": True}  # floor + condition already set → untouched


def test_deterministic_fields_are_not_mapped():
    # price / area / disposition / locality are authoritative from the HTML and
    # must never be written by the enricher even if the LLM returns them.
    extraction = {
        "price_czk": _env(1_000_000), "area_m2": _env(50), "disposition": _env("2+kk"),
        "locality": _env("Praha"), "category_main": _env("byt"),
    }
    assert columns_from_extraction(extraction, dict(_EMPTY)) == {}


def test_floor_plausibility_guard():
    # floor above the building's total (both from this extraction) -> dropped,
    # total kept.
    out = columns_from_extraction(
        {"floor": _env(8), "total_floors": _env(5)}, dict(_EMPTY)
    )
    assert out == {"total_floors": 5}
    # total from the already-stored column (e.g. the deterministic parser) guards
    # an LLM floor too.
    out = columns_from_extraction({"floor": _env(9)}, dict(_EMPTY, total_floors=4))
    assert out == {}
    # an out-of-band floor is dropped.
    assert columns_from_extraction({"floor": _env(99)}, dict(_EMPTY)) == {}
    # a plausible floor under the total is kept.
    out = columns_from_extraction(
        {"floor": _env(3), "total_floors": _env(6)}, dict(_EMPTY)
    )
    assert out == {"floor": 3, "total_floors": 6}


def test_normalizers():
    assert _norm_condition("Po rekonstrukci") == "po_rekonstrukci"
    assert _norm_condition("velmi dobrý stav") == "velmi_dobry"
    assert _norm_building_type("Smíšená") == "smisena"
    assert _norm_energy("b") == "B"
    assert _norm_energy("not a rating") is None
    assert _norm_energy(None) is None


def test_select_pending_sql_invariants():
    import importlib

    m = importlib.import_module("scripts.enrich_listing_descriptions")

    class _Cur:
        def __init__(self) -> None:
            self.sql = ""
            self.params: Any = None

        def execute(self, sql: str, params: Any = None) -> None:
            self.sql, self.params = sql, params

        def fetchall(self):
            return [(1,), (2,)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self) -> None:
            self.cur = _Cur()

        def cursor(self):
            return self.cur

    conn = _Conn()
    out = m._select_pending(conn, source="bazos", max_age_days=0, limit=500)
    assert out == [1, 2]
    sql = conn.cur.sql
    assert "l.source = %s" in sql
    assert "l.description IS NOT NULL" in sql
    assert "NOT EXISTS (" in sql
    assert "listing_description_enrichments e" in sql
    # The fix: the latest-snapshot check is a per-listing correlated subquery, so
    # there must be NO global `MAX(id) ... GROUP BY` over the whole snapshots table
    # (that form aggregated every listing's history and timed out).
    assert "GROUP BY" not in sql
    assert "MAX(id)" in sql
    # Source-scoped + freshest-first reuses the existing (source, first_seen_at) index.
    assert "ORDER BY l.first_seen_at DESC" in sql
    assert "LIMIT %s" in sql
    assert conn.cur.params == ("bazos", 500)  # no freshness param when max_age_days=0

    # max_age_days>0 adds the freshness clause and threads (source, interval, limit).
    conn2 = _Conn()
    m._select_pending(conn2, source="bazos", max_age_days=7, limit=500)
    assert "last_seen_at > now() - %s::interval" in conn2.cur.sql
    assert conn2.cur.params == ("bazos", "7 days", 500)
