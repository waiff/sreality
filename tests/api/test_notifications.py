"""Hermetic tests for the notifications backend (Phase U2.7).

Covers WatchdogFilterSpec validation + the SQL-clause generator. The
matcher loop and FastAPI routes are integration-heavy (DB + asyncio
lifespan) and exercised via the live deployment, not here.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from api.notifications import WatchdogFilterSpec, _build_match_clauses


def test_filter_spec_defaults_target_byt_pronajem() -> None:
    """Blank-save watchdogs already target apartments-for-rent."""
    spec = WatchdogFilterSpec()
    assert spec.category_main == "byt"
    assert spec.category_type == "pronajem"


def test_filter_spec_spatial_requires_all_three() -> None:
    """lat/lng/radius_m must be all set or all None — partial spatial
    filters silently broke the SQL in v1, so validate eagerly."""
    with pytest.raises(ValueError):
        WatchdogFilterSpec(lat=50.0, lng=14.0)  # radius_m missing
    with pytest.raises(ValueError):
        WatchdogFilterSpec(lat=50.0, radius_m=1000)  # lng missing

    # All three set is OK.
    s = WatchdogFilterSpec(lat=50.0, lng=14.0, radius_m=1000)
    assert s.lat == 50.0


def test_build_clauses_emits_category_clauses_by_default() -> None:
    spec = WatchdogFilterSpec()
    where, params = _build_match_clauses(spec)
    assert "l.category_main = %(category_main)s" in where
    assert "l.category_type = %(category_type)s" in where
    assert params["category_main"] == "byt"
    assert params["category_type"] == "pronajem"


def test_build_clauses_skips_unset_spatial() -> None:
    spec = WatchdogFilterSpec()
    where, params = _build_match_clauses(spec)
    assert not any("ST_DWithin" in w for w in where)
    assert "radius_m" not in params


def test_build_clauses_emits_spatial_when_set() -> None:
    spec = WatchdogFilterSpec(lat=50.08, lng=14.42, radius_m=1500)
    where, params = _build_match_clauses(spec)
    assert any("ST_DWithin" in w for w in where)
    # properties_public exposes lat/lng, not the raw geom column, so the
    # spatial predicate builds the point from lat/lng (Slice 2b).
    assert any("l.lat IS NOT NULL" in w for w in where)
    assert any("ST_MakePoint(l.lng, l.lat)" in w for w in where)
    assert not any("l.geom" in w for w in where)
    assert params["lat"] == 50.08
    assert params["lng"] == 14.42
    assert params["radius_m"] == 1500


def test_build_clauses_property_grain_derived_predicates() -> None:
    """Slice 2b derived filters render as `>= N` predicates against the
    property-grain columns properties_public exposes."""
    spec = WatchdogFilterSpec(
        distinct_site_count_min=2,
        price_drop_count_min=3,
        price_rise_count_min=1,
        max_price_drop_pct_min=10.0,
    )
    where, params = _build_match_clauses(spec)
    assert "l.distinct_site_count >= %(distinct_site_count_min)s" in where
    assert "l.price_drop_count >= %(price_drop_count_min)s" in where
    assert "l.price_rise_count >= %(price_rise_count_min)s" in where
    assert "l.max_price_drop_pct >= %(max_price_drop_pct_min)s" in where
    assert params["distinct_site_count_min"] == 2
    assert params["price_drop_count_min"] == 3
    assert params["price_rise_count_min"] == 1
    assert params["max_price_drop_pct_min"] == 10.0


def test_build_clauses_handles_price_and_area_bounds() -> None:
    spec = WatchdogFilterSpec(
        min_price_czk=15_000,
        max_price_czk=30_000,
        min_area_m2=40.0,
        max_area_m2=80.0,
    )
    where, params = _build_match_clauses(spec)
    assert "l.price_czk >= %(min_price_czk)s" in where
    assert "l.price_czk <= %(max_price_czk)s" in where
    assert "l.area_m2 >= %(min_area_m2)s" in where
    assert "l.area_m2 <= %(max_area_m2)s" in where
    assert params["min_price_czk"] == 15_000
    assert params["max_price_czk"] == 30_000


def test_build_clauses_tri_state_amenities() -> None:
    spec = WatchdogFilterSpec(
        has_balcony=True,
        terrace=False,
        garage=None,  # explicit "any" — no clause
    )
    where, params = _build_match_clauses(spec)
    assert "l.has_balcony = %(has_balcony)s" in where
    assert "l.terrace = %(terrace)s" in where
    assert not any("l.garage = " in w for w in where)
    assert params["has_balcony"] is True
    assert params["terrace"] is False


def test_build_clauses_dispositions_use_any() -> None:
    """Dispositions are a multi-select; SQL uses ANY() for index-friendly
    lookups against `l.disposition`."""
    spec = WatchdogFilterSpec(dispositions=["2+kk", "2+1", "3+kk"])
    where, params = _build_match_clauses(spec)
    assert "l.disposition = ANY(%(dispositions)s)" in where
    assert params["dispositions"] == ["2+kk", "2+1", "3+kk"]


def test_build_clauses_subtype_use_any() -> None:
    """Subtype is a portal-agnostic multi-select; ANY() against l.subtype."""
    spec = WatchdogFilterSpec(subtype=["rodinny_dum", "vila"])
    where, params = _build_match_clauses(spec)
    assert "l.subtype = ANY(%(subtype)s)" in where
    assert params["subtype"] == ["rodinny_dum", "vila"]


def test_build_clauses_no_subtype_by_default() -> None:
    """Default spec leaves subtype unset → no subtype clause emitted."""
    where, params = _build_match_clauses(WatchdogFilterSpec())
    assert "subtype" not in params
    assert not any("l.subtype" in c for c in where)


def test_build_clauses_district_chip_without_context() -> None:
    """A chip with `context=None` produces a single (district ILIKE name OR
    locality ILIKE name) clause — same shape as the migration 067
    behaviour, preserved for picks at the municipality / okres / kraj
    level (where there's nothing finer to narrow against)."""
    spec = WatchdogFilterSpec(
        districts=[{"name": "okres Jihlava", "context": None}],
    )
    where, params = _build_match_clauses(spec)
    district_clause = next(w for w in where if "district_name_0" in w)
    # Wildcards live in the bound VALUE, not as inline SQL '%' literals —
    # a bare '%' in the query string is a malformed psycopg placeholder and
    # raised at execute time, silently killing every matcher pass.
    assert "l.district ILIKE %(district_name_0)s" in district_clause
    assert "l.locality ILIKE %(district_name_0)s" in district_clause
    assert "'%'" not in district_clause
    assert " AND " not in district_clause
    assert params["district_name_0"] == "%okres Jihlava%"
    assert "district_ctx_0" not in params


def test_build_clauses_district_chip_with_context_anding_the_narrow() -> None:
    """A chip with a parent municipality narrows the name match — the
    fix for 'Edvarda Beneše · Plzeň' no longer dragging in the streets
    of the same name in Olomouc / Hradec Králové. Generates the same
    AND'd predicate browse_stats applies (migration 074)."""
    spec = WatchdogFilterSpec(
        districts=[{"name": "Edvarda Beneše", "context": "Plzeň"}],
    )
    where, params = _build_match_clauses(spec)
    district_clause = next(w for w in where if "district_name_0" in w)
    assert "%(district_name_0)s" in district_clause
    assert "%(district_ctx_0)s" in district_clause
    assert " AND " in district_clause
    assert params["district_name_0"] == "%Edvarda Beneše%"
    assert params["district_ctx_0"] == "%Plzeň%"


def test_build_clauses_district_multiple_chips_are_or_joined() -> None:
    """Two chips OR'd: the cohort matches either (Plzeň-narrowed) or
    (Olomouc-narrowed). Matches the per-chip OR Browse uses."""
    spec = WatchdogFilterSpec(
        districts=[
            {"name": "Edvarda Beneše", "context": "Plzeň"},
            {"name": "Edvarda Beneše", "context": "Olomouc"},
        ],
    )
    where, params = _build_match_clauses(spec)
    district_clause = next(w for w in where if "district_name_0" in w)
    assert "%(district_name_0)s" in district_clause
    assert "%(district_name_1)s" in district_clause
    assert " OR " in district_clause
    assert params["district_ctx_0"] == "%Plzeň%"
    assert params["district_ctx_1"] == "%Olomouc%"


def test_build_clauses_district_no_inline_percent_literals() -> None:
    """Regression: the district ILIKE clauses must carry NO inline SQL '%'
    wildcards. psycopg scans the query string for `%`-placeholders, so a bare
    '%' (as in the old `ILIKE '%' || %(name)s || '%'`) is a malformed
    placeholder that raises ProgrammingError at execute time. That raise sat
    outside the per-subscription guard in match_once, so it silently zeroed
    the entire watchdog feed for every watchdog with a district chip — 0
    dispatches despite real matches. Wildcards must live in the bound VALUE."""
    spec = WatchdogFilterSpec(
        districts=[{"name": "Jihlava", "context": "Vysočina"}],
    )
    where, params = _build_match_clauses(spec)
    district_clause = next(w for w in where if "district_name_0" in w)
    # The only '%' in the SQL must be inside %(...)s placeholders; none bare.
    assert "'%'" not in district_clause
    assert "||" not in district_clause
    # Wildcards moved into the parameter values instead.
    assert params["district_name_0"] == "%Jihlava%"
    assert params["district_ctx_0"] == "%Vysočina%"


def test_build_clauses_district_excluded_chip_is_negated() -> None:
    """An excluded chip becomes a NOT (...) group that subtracts its
    matches. With every chip excluded there is no positive include group —
    only the negation. Mirrors Browse's `not.or(...)` and browse_stats'
    EXCLUDE gate (migration 146)."""
    spec = WatchdogFilterSpec(
        districts=[{"name": "Praha", "context": None, "excluded": True}],
    )
    where, params = _build_match_clauses(spec)
    district_clauses = [w for w in where if "district_name_0" in w]
    assert len(district_clauses) == 1
    assert district_clauses[0].startswith("NOT (")
    assert "l.district ILIKE %(district_name_0)s" in district_clauses[0]
    assert params["district_name_0"] == "%Praha%"
    assert "'%'" not in district_clauses[0]  # wildcards stay in the value


def test_build_clauses_district_mixed_include_exclude() -> None:
    """Include + exclude chips emit two WHERE entries — an OR'd include
    group AND a NOT(...) exclude group — keeping the matcher in lockstep
    with Browse (queries.ts) and browse_stats (migration 146). Params are
    keyed by original chip position regardless of the split."""
    spec = WatchdogFilterSpec(
        districts=[
            {"name": "Praha", "context": None},
            {"name": "Modřany", "context": None, "excluded": True},
        ],
    )
    where, params = _build_match_clauses(spec)
    inc = next(w for w in where if "district_name_0" in w)
    exc = next(w for w in where if "district_name_1" in w)
    assert not inc.startswith("NOT (")
    assert exc.startswith("NOT (")
    assert params["district_name_0"] == "%Praha%"
    assert params["district_name_1"] == "%Modřany%"


def test_filter_spec_lifts_legacy_string_districts() -> None:
    """Pre-migration-070 request bodies passing `districts: ["Praha"]`
    still validate — the field_validator lifts each string to a
    `{name, context: None}` chip so the matcher only sees the new
    shape. Covers the deploy window between the API redeploy and the
    backfill running."""
    spec = WatchdogFilterSpec(districts=["Praha", "okres Jihlava"])
    assert spec.districts is not None
    assert len(spec.districts) == 2
    assert spec.districts[0].name == "Praha"
    assert spec.districts[0].context is None
    assert spec.districts[1].name == "okres Jihlava"
    assert spec.districts[1].context is None


def test_build_clauses_enumerated_columns() -> None:
    # A bare string still coerces to a single-element list (backward-compat).
    spec = WatchdogFilterSpec(furnished="ano", ownership=["osobni", "druzstevni"])
    assert spec.furnished == ["ano"]
    where, params = _build_match_clauses(spec)
    clauses = " ".join(where)
    assert "l.furnished = ANY(%(furnished)s)" in clauses
    assert "l.ownership = ANY(%(ownership)s)" in clauses
    assert params["furnished"] == ["ano"]
    assert params["ownership"] == ["osobni", "druzstevni"]


def test_build_clauses_furnished_unknown_sentinel() -> None:
    spec = WatchdogFilterSpec(furnished=["__unknown__"])
    where, params = _build_match_clauses(spec)
    clauses = " ".join(where)
    assert "l.furnished IS NULL OR NOT (l.furnished = ANY(%(furnished_canon)s))" in clauses
    assert params["furnished_canon"] == ["ano", "ne", "castecne"]


def test_build_clauses_condition_match() -> None:
    """condition_match feeds ANY() against l.condition — the same
    pattern toolkit/comparables uses, so Browse / Watchdog stay
    aligned with the analytical surfaces."""
    spec = WatchdogFilterSpec(
        condition_match=["novostavba", "po_rekonstrukci"],
    )
    where, params = _build_match_clauses(spec)
    assert "l.condition = ANY(%(condition_match)s)" in where
    assert params["condition_match"] == ["novostavba", "po_rekonstrukci"]


def test_build_clauses_condition_match_empty_list_drops_clause() -> None:
    """An empty list behaves like None — no WHERE clause emitted.
    Matches how `dispositions` already behaves."""
    spec = WatchdogFilterSpec(condition_match=[])
    where, params = _build_match_clauses(spec)
    assert not any("l.condition" in w for w in where)
    assert "condition_match" not in params


def test_build_clauses_categoryless_spec() -> None:
    """An operator who explicitly clears the category filters gets a
    spec with no category WHERE clauses — the watchdog matches every
    category. Defaults narrow; explicit None widens."""
    spec = WatchdogFilterSpec(category_main=None, category_type=None)
    where, params = _build_match_clauses(spec)
    assert not any("l.category_main" in w for w in where)
    assert not any("l.category_type" in w for w in where)
    assert "category_main" not in params
    assert "category_type" not in params


def test_build_clauses_condition_level_minimums() -> None:
    """Watchdog must honour the new condition-level filters so the
    operator can say 'alert me when a level-5 apartment shows up'."""
    spec = WatchdogFilterSpec(
        building_condition_level_min=4,
        apartment_condition_level_min=5,
    )
    where, params = _build_match_clauses(spec)
    assert any("building_condition_level >= %(building_condition_level_min)s" in w for w in where)
    assert any("apartment_condition_level >= %(apartment_condition_level_min)s" in w for w in where)
    assert params["building_condition_level_min"] == 4
    assert params["apartment_condition_level_min"] == 5


def test_build_clauses_condition_level_minimums_omitted_by_default() -> None:
    """Absent filters add no clauses — NULL rows would otherwise be
    excluded by an unintended `IS NOT NULL` check."""
    spec = WatchdogFilterSpec()
    where, params = _build_match_clauses(spec)
    assert not any("building_condition_level" in w for w in where)
    assert not any("apartment_condition_level" in w for w in where)
    assert "building_condition_level_min" not in params
    assert "apartment_condition_level_min" not in params


# --- match_once with a fake psycopg connection ---------------------------


from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from api.notifications import match_once


class _FakeCursor:
    """Minimal cursor that returns canned rows in sequence.

    `_FakeConn.results` is a list of (predicate, rows) pairs; the cursor
    finds the first predicate that matches the executed SQL and returns
    its `rows`. Insert/UPDATE SQL is captured in `_FakeConn.executed`
    so the test can assert on what the matcher emitted.
    """

    def __init__(self, conn: "_FakeConn"):
        self._conn = conn
        self._last_rows: list[tuple[Any, ...]] = []
        self.rowcount = 0
        self.description: list[tuple[str]] | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        sql_norm = " ".join(sql.split())
        self._conn.executed.append((sql_norm, params))
        for predicate, rows, rowcount in self._conn.script:
            if predicate(sql_norm):
                self._last_rows = rows
                self.rowcount = rowcount
                return
        self._last_rows = []
        self.rowcount = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._last_rows[0] if self._last_rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._last_rows)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _FakeConn:
    def __init__(
        self,
        script: list[tuple[Any, list[tuple[Any, ...]], int]],
    ):
        self.script = script
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def test_match_once_uses_per_subscription_cursor() -> None:
    """The matcher reads `last_matched_first_seen_at` per subscription
    and injects it as the `cursor` clause — proves the per-subscription
    cursor column drives the SQL, not the old global watermark."""
    sub_id = UUID("46a6005f-dc32-4d94-9ced-4d262b57ef6b")
    cursor_ts = datetime(2026, 5, 14, 13, 57, 48, tzinfo=timezone.utc)
    upper_ts = datetime(2026, 5, 15, 21, 0, 0, tzinfo=timezone.utc)

    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        # app_settings reads for interval / window — default both.
        (lambda s: "FROM app_settings" in s, [], 0),
        # Active subscriptions query
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {"category_main": "byt", "category_type": "pronajem", "districts": ["Praha"]}, cursor_ts)],
            1,
        ),
        # Window upper-bound query
        (lambda s: "SELECT max(first_seen_at), count(*) FROM" in s, [(upper_ts, 5)], 0),
        # INSERT dispatches — return rowcount 3 (idempotent dedup)
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 3),
        # Cursor advance UPDATE — rowcount 1
        (lambda s: "UPDATE notification_subscriptions SET last_matched_first_seen_at" in s, [], 1),
    ]
    conn = _FakeConn(script)

    stats = match_once(conn)  # type: ignore[arg-type]

    assert stats == {
        "subscriptions_evaluated": 1,
        "matches_inserted": 3,
        "listings_in_window": 5,
        "cursors_advanced": 1,
    }

    # Verify the WHERE clause references the per-subscription cursor
    # parameter, not a global watermark.
    window_query = next(
        (sql, p) for sql, p in conn.executed if "max(first_seen_at), count(*)" in sql
    )
    sql, params = window_query
    assert "l.first_seen_at > %(cursor)s" in sql
    assert isinstance(params, dict) and params["cursor"] == cursor_ts

    # And that the filter spec made it through too. The district chip
    # without a context lands as a single ILIKE-OR pair under the
    # `district_name_0` placeholder (matching browse_stats' migration
    # 069 predicate); the row's legacy string form is lifted by
    # `WatchdogFilterSpec._lift_legacy_districts` before SQL build.
    assert params["category_main"] == "byt"
    assert params["category_type"] == "pronajem"
    assert params["district_name_0"] == "%Praha%"
    assert "district_ctx_0" not in params  # null context = no narrow
    assert "l.district ILIKE %(district_name_0)s" in sql


def test_match_once_skips_subscription_with_no_listings() -> None:
    """An empty window (no listings past the cursor) is the steady
    state. The matcher should record zero dispatches and zero cursor
    advances rather than blow up trying to UPDATE with a NULL value."""
    sub_id = UUID("00000000-0000-0000-0000-000000000001")
    cursor_ts = datetime(2030, 1, 1, tzinfo=timezone.utc)

    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM app_settings" in s, [], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {}, cursor_ts)],
            1,
        ),
        # Window query returns (NULL, 0) — no fresh listings.
        (lambda s: "SELECT max(first_seen_at), count(*) FROM" in s, [(None, 0)], 0),
    ]
    conn = _FakeConn(script)

    stats = match_once(conn)  # type: ignore[arg-type]

    assert stats["subscriptions_evaluated"] == 1
    assert stats["matches_inserted"] == 0
    assert stats["cursors_advanced"] == 0
    # Critically: no INSERT or UPDATE was attempted.
    assert not any("INSERT INTO notification_dispatches" in sql for sql, _ in conn.executed)
    assert not any(
        "UPDATE notification_subscriptions" in sql for sql, _ in conn.executed
    )


def test_match_once_skips_invalid_filter_spec() -> None:
    """A subscription whose filter_spec doesn't validate (e.g. corrupt
    legacy row) must NOT crash the whole matcher pass — log + skip."""
    good_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    bad_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    cursor_ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    upper_ts = datetime(2026, 5, 15, tzinfo=timezone.utc)

    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM app_settings" in s, [], 0),
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [
                # Invalid: partial spatial filter (no radius).
                (bad_id, {"lat": 50.0, "lng": 14.0}, cursor_ts),
                # Valid spec
                (good_id, {}, cursor_ts),
            ],
            2,
        ),
        (lambda s: "SELECT max(first_seen_at), count(*) FROM" in s, [(upper_ts, 1)], 0),
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 1),
        (lambda s: "UPDATE notification_subscriptions SET last_matched_first_seen_at" in s, [], 1),
    ]
    conn = _FakeConn(script)

    stats = match_once(conn)  # type: ignore[arg-type]

    # Both rows counted as "evaluated" — the broken one was inspected
    # and skipped, but the surviving one fired one dispatch.
    assert stats["subscriptions_evaluated"] == 2
    assert stats["matches_inserted"] == 1


# --- match_changes_once (the property-change matcher, Slice 2b) ----------


from api.notifications import match_changes_once


def test_match_changes_once_emits_price_drop_for_matching_subs() -> None:
    """The change matcher resolves recently-dropped property ids once, then
    fires a `price_drop` dispatch per matching active subscription, scoped to
    those ids and deduped by the (sub, property, change_kind) constraint."""
    sub_id = UUID("12121212-1212-1212-1212-121212121212")
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        # window-days app_settings read → default
        (lambda s: "FROM app_settings" in s, [], 0),
        # recent price-drop property ids
        (lambda s: "SELECT DISTINCT property_id FROM steps" in s, [(101,), (102,)], 0),
        # active subscriptions
        (
            lambda s: "FROM notification_subscriptions WHERE is_active" in s,
            [(sub_id, {"category_main": "byt", "category_type": "pronajem"})],
            1,
        ),
        # INSERT price_drop dispatches — 2 inserted
        (lambda s: "INSERT INTO notification_dispatches" in s, [], 2),
    ]
    conn = _FakeConn(script)

    stats = match_changes_once(conn)  # type: ignore[arg-type]

    assert stats["subscriptions_evaluated"] == 1
    assert stats["price_drops_in_window"] == 2
    assert stats["changes_inserted"] == 2

    insert_sql, insert_params = next(
        (sql, p) for sql, p in conn.executed
        if "INSERT INTO notification_dispatches" in sql
    )
    assert "'price_drop'" in insert_sql
    assert "l.property_id = ANY(%(drop_ids)s)" in insert_sql
    assert "ON CONFLICT (subscription_id, property_id, change_kind)" in insert_sql
    assert insert_params["drop_ids"] == [101, 102]


def test_match_changes_once_noops_when_no_recent_drops() -> None:
    """No recent price drops → no subscription scan, no inserts."""
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "FROM app_settings" in s, [], 0),
        (lambda s: "SELECT DISTINCT property_id FROM steps" in s, [], 0),
    ]
    conn = _FakeConn(script)

    stats = match_changes_once(conn)  # type: ignore[arg-type]

    assert stats == {
        "subscriptions_evaluated": 0,
        "price_drops_in_window": 0,
        "changes_inserted": 0,
    }
    assert not any(
        "INSERT INTO notification_dispatches" in sql for sql, _ in conn.executed
    )


# --- kickoff (Run estimation) regression --------------------------------------


from api.notifications import _insert_pending_run


def test_insert_pending_run_does_not_reference_nonexistent_columns() -> None:
    """Regression: estimation_runs has NO category_main / category_type
    columns. The kickoff INSERT must not name them (doing so 500s the
    `/dispatches/{id}/estimate` endpoint, which silently no-ops the
    'Run estimation' button). category_main/type ride in input_spec instead."""
    script: list[tuple[Any, list[tuple[Any, ...]], int]] = [
        (lambda s: "INSERT INTO estimation_runs" in s, [(4242,)], 1),
    ]
    conn = _FakeConn(script)

    spec = {
        "lat": 50.0, "lng": 14.0, "area_m2": 60.0, "disposition": "2+kk",
        "floor": 3, "exclude_ids": [99],
        "category_main": "byt", "category_type": "prodej",
    }
    run_id = _insert_pending_run(
        conn,  # type: ignore[arg-type]
        sreality_id=99, spec=spec, estimate_kind="sale",
    )
    assert run_id == 4242

    insert_sql, params = next(
        (sql, p) for sql, p in conn.executed if "INSERT INTO estimation_runs" in sql
    )
    # The two phantom columns must NOT appear in the INSERT column list.
    cols_clause = insert_sql.split("VALUES")[0]
    assert "category_main" not in cols_clause
    assert "category_type" not in cols_clause
    # And the category survives inside the input_spec jsonb param instead.
    spec_param = next(p for p in params if isinstance(p, str) and '"category_main"' in p)
    assert '"category_type": "prodej"' in spec_param


import api.notifications as nf


def test_kickoff_always_runs_a_rent_estimate_even_for_a_sale_listing(monkeypatch) -> None:
    """The watchdog 'Estimate rent' action runs a RENTAL estimate regardless of
    the subject listing's own category_type — a sale flat gets a "what would it
    rent for" figure. So input_spec must carry category_type='pronajem' and the
    run's estimate_kind must be 'rent', even when the listing is 'prodej'."""
    monkeypatch.setattr(
        nf, "_fetch_dispatch",
        lambda conn, did: {"sreality_id": 12345, "estimation_run_id": None},
    )
    monkeypatch.setattr(
        nf, "_resolve_listing_for_estimate",
        lambda conn, sid: {
            "lat": 50.08, "lng": 14.42, "area_m2": 62.0, "disposition": "2+kk",
            "floor": 3, "category_main": "byt", "category_type": "prodej",
            "price_czk": 4_500_000, "price_unit": "czk",
        },
    )
    monkeypatch.setattr(nf, "_link_dispatch_run", lambda conn, did, rid: None)

    captured: dict[str, Any] = {}

    def _fake_insert(conn, *, sreality_id, spec, estimate_kind):
        captured["spec"] = spec
        captured["estimate_kind"] = estimate_kind
        return 777

    monkeypatch.setattr(nf, "_insert_pending_run", _fake_insert)

    _dispatch, run_id = nf.kickoff_estimation_for_dispatch(object(), "d-1")  # type: ignore[arg-type]

    assert run_id == 777
    assert captured["estimate_kind"] == "rent"
    # Forces a rental comparable cohort even though the subject is 'prodej'.
    assert captured["spec"]["category_type"] == "pronajem"
    assert captured["spec"]["category_main"] == "byt"
