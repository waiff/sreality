"""Hermetic tests for find_anchor_amenities.

No DB connection: a scripted fake cursor returns prepared rows in
order. OverpassClient is replaced with a fake that records calls and
returns prepared element lists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from toolkit import amenities as amen


# Static taxonomy invariants


def test_category_tags_all_values_are_list_of_dicts():
    for category, tag_list in amen.CATEGORY_TAGS.items():
        assert isinstance(tag_list, list), category
        assert tag_list, category
        for d in tag_list:
            assert isinstance(d, dict), category
            for k, v in d.items():
                assert isinstance(k, str)
                assert isinstance(v, (str, bool))


def test_category_tags_includes_expected_v1_categories():
    expected = {
        "tram_stop", "metro_station", "bus_stop",
        "supermarket", "convenience", "pharmacy",
        "school_primary", "kindergarten", "park", "restaurant",
    }
    assert expected <= set(amen.CATEGORY_TAGS)


# Validation


def test_unknown_category_raises_value_error():
    conn = _make_conn([])
    client = _FakeOverpass([])
    with pytest.raises(ValueError, match="unknown categories"):
        amen.find_anchor_amenities(
            conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
            categories=["does_not_exist"],
            overpass_client=client,
        )


# Cache hit path


def test_cache_hit_does_not_call_overpass():
    # Plan per-category: _cache_is_fresh(SELECT 1) → row(s); _read_amenities → 1 row.
    plan: list[_Step] = [
        _Step("fetchone", (1,)),  # cache fresh
        _Step("fetchall", [_amenity_row(name="Anděl", distance=87.3)]),
    ]
    conn = _make_conn(plan)
    client = _FakeOverpass([])  # any call would assert

    res = amen.find_anchor_amenities(
        conn, lat=50.075, lng=14.43, radius_m=500,  # type: ignore[arg-type]
        categories=["tram_stop"],
        overpass_client=client,
    )

    assert client.calls == []
    assert res["data"]["from_cache"] == {"tram_stop": True}
    assert res["data"]["categories"]["tram_stop"]["count"] == 1
    assert res["data"]["categories"]["tram_stop"]["nearest_distance_m"] == 87.3


# Cache miss path


def test_cache_miss_calls_overpass_then_writes_then_reads():
    # _cache_is_fresh → None (miss); _write_cache → INSERT amenity + INSERT fetch;
    # _read_amenities → 1 row.
    plan: list[_Step] = [
        _Step("fetchone", None),                 # cache miss
        _Step("execute_write", None),            # INSERT amenity
        _Step("execute_write", None),            # INSERT amenity_fetches
        _Step("fetchall", [_amenity_row()]),     # read back
    ]
    conn = _make_conn(plan)
    client = _FakeOverpass([
        [{
            "source_id": "node/1",
            "name": "Anděl",
            "lat": 50.0747, "lng": 14.4296,
            "tags": {"name": "Anděl", "railway": "tram_stop"},
        }],
    ])

    res = amen.find_anchor_amenities(
        conn, lat=50.075, lng=14.43, radius_m=500,  # type: ignore[arg-type]
        categories=["tram_stop"],
        overpass_client=client,
    )

    # Overpass was called exactly once with the right tag list
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["tags"] == amen.CATEGORY_TAGS["tram_stop"]
    assert call["lat"] == 50.075 and call["lng"] == 14.43
    assert call["radius_m"] == 500

    # Transaction was opened for the write
    assert conn.transactions_opened == 1

    assert res["data"]["from_cache"] == {"tram_stop": False}
    assert res["data"]["categories"]["tram_stop"]["count"] == 1


def test_cache_write_inserts_one_row_per_overpass_element_plus_fetch():
    elements = [
        {"source_id": f"node/{i}", "name": f"stop-{i}",
         "lat": 50.0, "lng": 14.0,
         "tags": {"railway": "tram_stop"}}
        for i in range(3)
    ]
    plan: list[_Step] = [
        _Step("fetchone", None),                            # miss
        _Step("execute_write", None),                       # insert 1
        _Step("execute_write", None),                       # insert 2
        _Step("execute_write", None),                       # insert 3
        _Step("execute_write", None),                       # insert amenity_fetches
        _Step("fetchall", [_amenity_row() for _ in range(3)]),
    ]
    conn = _make_conn(plan)
    client = _FakeOverpass([elements])

    amen.find_anchor_amenities(
        conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
        categories=["tram_stop"],
        overpass_client=client,
    )

    # Five execute calls inside the write transaction:
    # 3 amenity upserts + 1 amenity_fetches insert (the cache-check
    # SELECT and the read-back SELECT happen outside the transaction).
    write_executes = [
        e for e in conn.cursor_obj.executed
        if "INSERT" in e[0].upper()
    ]
    assert len(write_executes) == 4

    # The fetch row carries amenity_count = 3
    fetch_insert = _find_fetch_insert(write_executes)
    assert fetch_insert[1]["count"] == 3


def test_zero_overpass_results_still_writes_fetch_row():
    plan: list[_Step] = [
        _Step("fetchone", None),               # miss
        _Step("execute_write", None),          # only amenity_fetches insert
        _Step("fetchall", []),                 # empty read-back
    ]
    conn = _make_conn(plan)
    client = _FakeOverpass([[]])

    res = amen.find_anchor_amenities(
        conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
        categories=["tram_stop"],
        overpass_client=client,
    )

    assert res["data"]["categories"]["tram_stop"] == {
        "count": 0, "nearest_distance_m": None, "items": [],
    }
    fetch_insert = _find_fetch_insert(conn.cursor_obj.executed)
    assert fetch_insert[1]["count"] == 0


# Mixed hit/miss across categories


def test_partial_cache_only_refetches_missing_categories():
    plan: list[_Step] = [
        # tram_stop: hit
        _Step("fetchone", (1,)),
        _Step("fetchall", [_amenity_row()]),
        # supermarket: miss → fetch + write + read
        _Step("fetchone", None),
        _Step("execute_write", None),  # insert amenity
        _Step("execute_write", None),  # insert amenity_fetches
        _Step("fetchall", [_amenity_row()]),
    ]
    conn = _make_conn(plan)
    client = _FakeOverpass([
        [{"source_id": "node/9", "name": "Albert",
          "lat": 50.0, "lng": 14.0,
          "tags": {"shop": "supermarket"}}],
    ])

    res = amen.find_anchor_amenities(
        conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
        categories=["tram_stop", "supermarket"],
        overpass_client=client,
    )

    assert len(client.calls) == 1
    assert client.calls[0]["tags"] == amen.CATEGORY_TAGS["supermarket"]
    assert res["data"]["from_cache"] == {
        "tram_stop": True, "supermarket": False,
    }


# Envelope


def test_envelope_metadata_shape():
    plan: list[_Step] = [
        _Step("fetchone", (1,)),
        _Step("fetchall", [_amenity_row()]),
    ]
    conn = _make_conn(plan)
    client = _FakeOverpass([])

    res = amen.find_anchor_amenities(
        conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
        categories=["tram_stop"],
        cache_ttl_days=14,
        overpass_client=client,
    )

    md = res["metadata"]
    assert md["tool"] == "find_anchor_amenities"
    assert md["filters_used"] == {
        "lat": 50.0, "lng": 14.0, "radius_m": 500,
        "categories": ["tram_stop"], "cache_ttl_days": 14,
    }
    assert md["result_count"] == 1
    assert md["queried_at"]
    assert md["data_freshness"] == _FETCHED_AT_ISO


def test_data_freshness_uses_max_fetched_at_across_categories():
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 4, 1, tzinfo=timezone.utc)
    plan: list[_Step] = [
        _Step("fetchone", (1,)),
        _Step("fetchall", [_amenity_row(fetched_at=older)]),
        _Step("fetchone", (1,)),
        _Step("fetchall", [_amenity_row(fetched_at=newer)]),
    ]
    conn = _make_conn(plan)
    res = amen.find_anchor_amenities(
        conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
        categories=["tram_stop", "park"],
        overpass_client=_FakeOverpass([]),
    )
    assert res["metadata"]["data_freshness"] == newer.isoformat()


def test_density_note_emitted_above_threshold():
    many = [_amenity_row(distance=float(i)) for i in range(amen._DENSITY_WARN_THRESHOLD + 1)]
    plan: list[_Step] = [
        _Step("fetchone", (1,)),
        _Step("fetchall", many),
    ]
    conn = _make_conn(plan)
    res = amen.find_anchor_amenities(
        conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
        categories=["restaurant"],
        overpass_client=_FakeOverpass([]),
    )
    notes = res["metadata"].get("notes") or []
    assert any("restaurant" in n and "density" in n for n in notes)


def test_no_density_note_when_under_threshold():
    plan: list[_Step] = [
        _Step("fetchone", (1,)),
        _Step("fetchall", [_amenity_row(), _amenity_row()]),
    ]
    conn = _make_conn(plan)
    res = amen.find_anchor_amenities(
        conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
        categories=["restaurant"],
        overpass_client=_FakeOverpass([]),
    )
    assert "notes" not in res["metadata"]


def test_default_categories_are_all_of_them():
    # Every category answers as a hit → 2 steps each.
    plan: list[_Step] = []
    for _ in amen.CATEGORY_TAGS:
        plan.append(_Step("fetchone", (1,)))
        plan.append(_Step("fetchall", []))
    conn = _make_conn(plan)
    res = amen.find_anchor_amenities(
        conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
        overpass_client=_FakeOverpass([]),
    )
    assert set(res["data"]["categories"]) == set(amen.CATEGORY_TAGS)
    assert res["metadata"]["filters_used"]["categories"] == list(amen.CATEGORY_TAGS)


# SQL shape (sanity that we're using the spatial functions correctly)


def test_cache_check_uses_st_dwithin_and_radius_filter():
    plan: list[_Step] = [
        _Step("fetchone", (1,)),
        _Step("fetchall", []),
    ]
    conn = _make_conn(plan)
    amen.find_anchor_amenities(
        conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
        categories=["tram_stop"],
        overpass_client=_FakeOverpass([]),
    )
    cache_check_sql = conn.cursor_obj.executed[0][0]
    assert "amenity_fetches" in cache_check_sql
    assert "ST_DWithin(" in cache_check_sql
    assert "radius_m = %(radius_m)s" in cache_check_sql
    assert "make_interval(days =>" in cache_check_sql


def test_read_query_uses_st_distance_and_orders_by_it():
    plan: list[_Step] = [
        _Step("fetchone", (1,)),
        _Step("fetchall", []),
    ]
    conn = _make_conn(plan)
    amen.find_anchor_amenities(
        conn, lat=50.0, lng=14.0, radius_m=500,  # type: ignore[arg-type]
        categories=["tram_stop"],
        overpass_client=_FakeOverpass([]),
    )
    read_sql = conn.cursor_obj.executed[1][0]
    assert "ST_Distance(" in read_sql
    assert "ORDER BY distance_m" in read_sql


# Helpers


_FETCHED_AT = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
_FETCHED_AT_ISO = _FETCHED_AT.isoformat()


def _find_fetch_insert(
    executed: list[tuple[str, dict[str, Any]]],
) -> tuple[str, dict[str, Any]]:
    return next(
        e for e in executed
        if "INSERT INTO amenity_fetches" in e[0]
    )


def _amenity_row(
    name: str = "X",
    distance: float = 100.0,
    fetched_at: datetime | None = _FETCHED_AT,
) -> dict[str, Any]:
    return {
        "source_id": "node/1",
        "name": name,
        "lat": 50.0,
        "lng": 14.0,
        "distance_m": distance,
        "fetched_at": fetched_at,
    }


class _Step:
    """One scripted DB interaction: tag identifies what to do next."""

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
                f"(idx={self._idx}, sql_head={sql[:60]!r})",
            )
        step = self._plan[self._idx]
        self.executed.append((sql, params or {}))
        if step.kind == "execute_write":
            self._idx += 1
            self._next_step = None
            self.description = None
            return
        # fetchone/fetchall: prep the response, advance later.
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

    def fetch(
        self,
        category_tags: list[dict[str, str | bool]],
        lat: float,
        lng: float,
        radius_m: int,
    ) -> list[dict[str, Any]]:
        self.calls.append({
            "tags": category_tags, "lat": lat, "lng": lng, "radius_m": radius_m,
        })
        if not self._queue:
            raise AssertionError("Unexpected Overpass.fetch call")
        return self._queue.pop(0)
