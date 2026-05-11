"""Hermetic tests for find_comparables_along_axis.

DB is a scripted cursor (same pattern as test_amenities.py); the
Overpass client is a fake that records each call. No real network,
no real Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from toolkit import transit_axis as ta
from toolkit.comparables import ComparableFilters, TargetSpec


_FETCHED_AT = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


# Validation


def test_rejects_unknown_transport_type():
    with pytest.raises(ValueError, match="unknown transport_types"):
        ta.find_comparables_along_axis(
            _make_conn([]),  # type: ignore[arg-type]
            TargetSpec(lat=50.0, lng=14.0),
            ComparableFilters(),
            transport_types=["train"],
        )


def test_rejects_empty_transport_type_list():
    with pytest.raises(ValueError, match="non-empty"):
        ta.find_comparables_along_axis(
            _make_conn([]),  # type: ignore[arg-type]
            TargetSpec(lat=50.0, lng=14.0),
            ComparableFilters(),
            transport_types=[],
        )


# Bbox + hash determinism


def test_bbox_grows_with_radius_and_pads():
    small = ta._bbox_around(50.0, 14.0, 500)
    large = ta._bbox_around(50.0, 14.0, 2000)
    assert large["maxlat"] - large["minlat"] > small["maxlat"] - small["minlat"]
    assert large["maxlng"] - large["minlng"] > small["maxlng"] - small["minlng"]
    # padding factor 1.05 -> 500m yields >450 / <550 -> roughly ~0.0047 deg.
    delta = (small["maxlat"] - small["minlat"]) / 2
    assert 0.0040 < delta < 0.0060


def test_hash_query_is_stable_and_order_independent():
    bbox = {"minlat": 50.0, "minlng": 14.0, "maxlat": 50.1, "maxlng": 14.1}
    h1 = ta._hash_query(bbox, ["tram", "bus", "subway"])
    h2 = ta._hash_query(bbox, ["bus", "subway", "tram"])
    assert h1 == h2
    h3 = ta._hash_query(bbox, ["tram", "bus"])
    assert h3 != h1


# Cache hit path: no Overpass call


def test_cache_hit_skips_overpass(monkeypatch):
    overpass = _FakeOverpass([])
    plan = [
        _Step("fetchone", (1,)),                # cache fresh -> hit
        _Step("fetchall", _listing_rows([])),   # corridor query -> empty
        _Step("fetchall", _line_rows([])),      # lines_used -> empty
    ]
    conn = _make_conn(plan)
    res = ta.find_comparables_along_axis(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(active_only=False),
        transport_types=["tram"],
        overpass_client=overpass,
    )
    assert overpass.calls == []
    assert res["metadata"]["from_cache"] is True
    assert res["metadata"]["result_count"] == 0
    assert res["metadata"]["tool"] == "find_comparables_along_axis"


# Cache miss path: Overpass call + cache write


def test_cache_miss_fetches_and_writes(monkeypatch):
    overpass = _FakeOverpass([[
        {
            "source_id":      "relation/100/way/11",
            "transport_type": "tram",
            "route_ref":      "9",
            "name":           "Tram 9",
            "linestring":     [(50.0, 14.0), (50.01, 14.01)],
            "tags":           {"route": "tram"},
        },
    ]])
    plan = [
        _Step("fetchone", None),         # cache miss
        _Step("execute_write", None),    # insert transit_lines row
        _Step("execute_write", None),    # insert transit_line_fetches row
        _Step("fetchall", _listing_rows([])),
        _Step("fetchall", _line_rows([])),
    ]
    conn = _make_conn(plan)
    res = ta.find_comparables_along_axis(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(active_only=False),
        transport_types=["tram"],
        overpass_client=overpass,
    )
    assert len(overpass.calls) == 1
    assert overpass.calls[0]["types"] == ["tram"]
    assert res["metadata"]["from_cache"] is False
    assert res["metadata"]["lines_fetched"] == 1
    # The cache-write inserts use ST_GeomFromText with our WKT
    inserts = [e for e in conn.cursor_obj.executed if "INSERT INTO transit_lines" in e[0]]
    assert len(inserts) == 1
    assert "LINESTRING(14.0 50.0, 14.01 50.01)" in inserts[0][1]["wkt"]
    fetch_inserts = [
        e for e in conn.cursor_obj.executed
        if "INSERT INTO transit_line_fetches" in e[0]
    ]
    assert len(fetch_inserts) == 1
    assert fetch_inserts[0][1]["transport_types"] == ["tram"]
    assert fetch_inserts[0][1]["count"] == 1


def test_corridor_excludes_radius_from_shared_filter():
    """find_comparables_along_axis must NOT add an anchor-radius
    ST_DWithin clause to listings — the corridor replaces it."""
    overpass = _FakeOverpass([])
    plan = [
        _Step("fetchone", (1,)),
        _Step("fetchall", _listing_rows([])),
        _Step("fetchall", _line_rows([])),
    ]
    conn = _make_conn(plan)
    ta.find_comparables_along_axis(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(active_only=False, radius_m=999_999),
        transport_types=["tram"],
        overpass_client=overpass,
    )
    # The corridor SELECT is the first fetchall (second SQL after cache check).
    corridor_sql = conn.cursor_obj.executed[1][0]
    # The corridor ST_DWithin against the line geom IS present.
    assert "ST_DWithin(l.geom, nl.geom" in corridor_sql
    # But the anchor-circle ST_DWithin against the listing geom is NOT.
    assert "ST_DWithin(\n      l.geom" not in corridor_sql
    assert "%(radius_m)s" not in corridor_sql


def test_listing_rows_carry_nearest_line_columns():
    overpass = _FakeOverpass([])
    rows = _listing_rows([
        {
            "sreality_id":   42,
            "corridor_distance_m": 88.0,
            "nearest_line_source_id":      "relation/9/way/1",
            "nearest_line_transport_type": "tram",
            "nearest_line_route_ref":      "9",
        },
    ])
    plan = [
        _Step("fetchone", (1,)),
        _Step("fetchall", rows),
        _Step("fetchall", _line_rows([
            {"source_id": "relation/9/way/1", "transport_type": "tram",
             "route_ref": "9", "name": "Tram 9",
             "distance_m": 100.0, "fetched_at": _FETCHED_AT},
        ])),
    ]
    conn = _make_conn(plan)
    res = ta.find_comparables_along_axis(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(active_only=False),
        transport_types=["tram"],
        overpass_client=overpass,
    )
    listing = res["data"]["listings"][0]
    assert listing["sreality_id"] == 42
    assert listing["nearest_line_route_ref"] == "9"
    assert listing["corridor_distance_m"] == 88.0
    assert "rn" not in listing
    assert res["data"]["lines"][0]["route_ref"] == "9"
    assert res["data"]["lines"][0]["distance_m"] == 100.0
    # data_freshness should fold in the line-cache freshness too.
    assert res["metadata"]["data_freshness"] is not None


def test_envelope_metadata_shape():
    overpass = _FakeOverpass([])
    plan = [
        _Step("fetchone", (1,)),
        _Step("fetchall", _listing_rows([])),
        _Step("fetchall", _line_rows([])),
    ]
    conn = _make_conn(plan)
    res = ta.find_comparables_along_axis(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(active_only=False),
        transport_types=["tram", "subway"],
        anchor_radius_m=600,
        corridor_m=250,
        overpass_client=overpass,
    )
    md = res["metadata"]
    assert md["tool"] == "find_comparables_along_axis"
    fu = md["filters_used"]
    assert fu["transport_types"] == ["subway", "tram"]   # sorted
    assert fu["anchor_radius_m"] == 600
    assert fu["corridor_m"] == 250
    assert fu["target"]["lat"] == 50.0
    assert "lines_considered" in md


def test_transport_types_get_deduped_and_sorted():
    overpass = _FakeOverpass([])
    plan = [
        _Step("fetchone", (1,)),
        _Step("fetchall", _listing_rows([])),
        _Step("fetchall", _line_rows([])),
    ]
    conn = _make_conn(plan)
    res = ta.find_comparables_along_axis(
        conn,  # type: ignore[arg-type]
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(active_only=False),
        transport_types=["tram", "tram", "bus"],
        overpass_client=overpass,
    )
    assert res["metadata"]["filters_used"]["transport_types"] == ["bus", "tram"]


def test_max_iso_picks_later_value():
    assert ta._max_iso(None, None) is None
    assert ta._max_iso("2026-05-10T12:00:00+00:00", None) == "2026-05-10T12:00:00+00:00"
    assert ta._max_iso(None, "2026-05-11T00:00:00+00:00") == "2026-05-11T00:00:00+00:00"
    assert ta._max_iso(
        "2026-05-10T12:00:00+00:00",
        "2026-05-11T00:00:00+00:00",
    ) == "2026-05-11T00:00:00+00:00"


# Helpers


_LISTING_COLS = [
    "sreality_id", "price_czk", "area_m2", "price_per_m2",
    "disposition", "district",
    "locality_district_id", "locality_region_id",
    "floor", "total_floors",
    "building_type", "condition", "energy_rating",
    "has_balcony", "has_lift", "has_parking",
    "distance_m", "first_seen_at", "last_seen_at",
    "data_age_days",
    "nearest_line_source_id",
    "nearest_line_transport_type",
    "nearest_line_route_ref",
    "corridor_distance_m",
    "rn",
]


def _listing_rows(overrides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ov in overrides:
        row: dict[str, Any] = {
            "sreality_id": 1, "price_czk": 20000, "area_m2": 50.0,
            "price_per_m2": 400.0,
            "disposition": "2+kk", "district": "Praha 1",
            "locality_district_id": 42, "locality_region_id": 8,
            "floor": 3, "total_floors": 5,
            "building_type": "cihla", "condition": "novostavba",
            "energy_rating": "B",
            "has_balcony": True, "has_lift": True, "has_parking": False,
            "distance_m": 200.0,
            "first_seen_at": _FETCHED_AT,
            "last_seen_at":  _FETCHED_AT,
            "data_age_days": 1,
            "nearest_line_source_id": "relation/9/way/1",
            "nearest_line_transport_type": "tram",
            "nearest_line_route_ref": "9",
            "corridor_distance_m": 80.0,
            "rn": 1,
        }
        row.update(ov)
        rows.append(row)
    return rows


def _line_rows(overrides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ov in overrides:
        row: dict[str, Any] = {
            "source_id":      "relation/9/way/1",
            "transport_type": "tram",
            "route_ref":      "9",
            "name":           "Tram 9",
            "distance_m":     100.0,
            "fetched_at":     _FETCHED_AT,
        }
        row.update(ov)
        rows.append(row)
    return rows


class _Step:
    def __init__(self, kind: str, payload: Any) -> None:
        self.kind = kind  # "fetchone" | "fetchall" | "execute_write"
        self.payload = payload


class _ScriptedCursor:
    def __init__(self, plan: list[_Step]) -> None:
        self._plan = plan
        self._idx = 0
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self._next_step: _Step | None = None
        self.description: list[tuple[str]] | None = None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        if self._idx >= len(self._plan):
            raise AssertionError(
                f"Cursor.execute called past plan end "
                f"(idx={self._idx}, sql_head={sql[:80]!r})",
            )
        step = self._plan[self._idx]
        self.executed.append((sql, params or {}))
        if step.kind == "execute_write":
            self._idx += 1
            self._next_step = None
            self.description = None
            return
        self._next_step = step
        if step.kind == "fetchall" and step.payload:
            cols = list(step.payload[0].keys())
            self.description = [(c,) for c in cols]
        else:
            self.description = None

    def fetchone(self) -> Any:
        assert self._next_step is not None and self._next_step.kind == "fetchone"
        out = self._next_step.payload
        self._idx += 1
        self._next_step = None
        return out

    def fetchall(self) -> list[tuple[Any, ...]]:
        assert self._next_step is not None and self._next_step.kind == "fetchall"
        rows = self._next_step.payload
        self._idx += 1
        self._next_step = None
        if not rows:
            return []
        cols = list(rows[0].keys())
        return [tuple(r[c] for c in cols) for r in rows]

    def __enter__(self) -> "_ScriptedCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Transaction:
    def __init__(self, conn: "_ScriptedConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Transaction":
        self._conn.transactions_opened += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _ScriptedConn:
    def __init__(self, plan: list[_Step]) -> None:
        self.cursor_obj = _ScriptedCursor(plan)
        self.transactions_opened = 0

    def cursor(self) -> _ScriptedCursor:
        return self.cursor_obj

    def transaction(self) -> _Transaction:
        return _Transaction(self)


def _make_conn(plan: list[_Step]) -> _ScriptedConn:
    return _ScriptedConn(plan)


class _FakeOverpass:
    def __init__(self, response_queue: list[list[dict[str, Any]]]) -> None:
        self._queue = list(response_queue)
        self.calls: list[dict[str, Any]] = []

    def fetch_routes(
        self,
        transport_types: list[str],
        bbox_minlat: float,
        bbox_minlng: float,
        bbox_maxlat: float,
        bbox_maxlng: float,
    ) -> list[dict[str, Any]]:
        self.calls.append({
            "types": list(transport_types),
            "bbox": (bbox_minlat, bbox_minlng, bbox_maxlat, bbox_maxlng),
        })
        if not self._queue:
            raise AssertionError("Unexpected Overpass.fetch_routes call")
        return self._queue.pop(0)
