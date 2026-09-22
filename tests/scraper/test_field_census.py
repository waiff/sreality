"""Offline tests for the field-capture instrument (no DB).

Two things are tested here: the pure row -> census/matrix reductions (so a re-bless is
reproducible from the same rows), and the STRUCTURAL validity of the checked-in goldens.

Structural, deliberately, and not freshness: a test that failed when a `generated_at` got
older than 30 days would red `main` on a calendar date rather than on a defect, on a branch
that touched nothing. Staleness is a `verify_pipeline` warning instead (`field_fill_matrix`),
where it lands in front of the operator with the rest of the pipeline's health.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from scraper import field_census as fc
from scraper.db import LISTING_COLUMNS
from scraper.portal_factory import PORTAL_CLASSES

_WHEN = _dt.datetime(2026, 9, 21, 19, 20, tzinfo=_dt.timezone.utc)


# --- the checked-in goldens ------------------------------------------------


def test_every_portal_has_a_census() -> None:
    """9/9 today. Stated as "every census is a real portal AND the two goldens agree",
    not as equality with the factory table: onboarding a tenth portal must not red a
    branch that cannot re-bless (re-blessing needs the production DB — the operator has
    it, a session's shell does not)."""
    blessed = {c["portal"] for c in fc.load_censuses()}
    assert blessed <= set(PORTAL_CLASSES)
    assert blessed == {key.split("/", 1)[0] for key in fc.load_baseline()["cells"]}


@pytest.mark.parametrize("census", fc.load_censuses(), ids=lambda c: c["portal"])
def test_census_is_structurally_valid(census: dict) -> None:
    assert _dt.datetime.fromisoformat(census["generated_at"]).tzinfo is not None
    assert census["sample"]["n_rows"] > 0
    assert census["sample"]["predicate"] == fc.SAMPLE_PREDICATE
    for key, entry in census["keys"].items():
        assert 0 < entry["n"] <= census["sample"]["n_rows"], key
        assert entry["pct"] >= fc.RARE_KEY_PCT, key
        # Values are all-or-nothing above the cap, so a truncated vocabulary can never
        # be mistaken for a complete one.
        if "values" in entry:
            assert entry["distinct"] <= fc.MAX_DISTINCT_FOR_VALUES, key
            assert len(entry["values"]) == entry["distinct"], key
            assert sum(entry["values"].values()) == entry["n"], key
    assert census["rare"]["n_keys"] == len(census["rare"]["keys"])


def test_baseline_covers_every_portal_and_attribute_field() -> None:
    """A subset check, not equality: re-blessing needs the production DB, which only the
    operator has, so a session adding a column or a portal must not be red-walled by a
    golden it cannot regenerate. A missing cell is caught at RUN time instead, by
    `compare_to_baseline`'s coverage arm — where it can name a live portal going dark."""
    baseline = fc.load_baseline()
    cells = baseline["cells"]
    assert set(cells) <= {
        fc.cell_key(p, f) for p in PORTAL_CLASSES for f in fc.ATTRIBUTE_FIELDS
    }
    assert {key.split("/", 1)[0] for key in cells} == set(PORTAL_CLASSES)
    # The cohort is stamped in the file, because it is what makes the file comparable
    # to a later run at all — a sampled baseline would be a different measurement.
    assert baseline["cohort"] == fc.MATRIX_COHORT


def test_baseline_records_the_known_zero_cells() -> None:
    """The cells this whole wave exists for: a parser reading a key the portal has
    never emitted. They are blessed as zero so the check is green on day one — and
    named in its report on every run until W2 declares a producer for each."""
    known = set(fc.zero_fill_cells(fc.load_baseline()))
    assert {
        "remax/has_balcony", "mmreality/has_balcony",
        "ceskereality/has_parking", "ceskereality/garage", "ceskereality/terrace",
        "ceskereality/parking_lots", "ceskereality/total_floors",
        "realitymix/has_lift",
    } <= known


def test_the_baseline_carries_the_statutory_energy_placeholder_per_portal() -> None:
    """The 67% G share is the corpus's biggest measure-validity fact, and W5 measured that
    no portal marks which of them is the statutory unassessed placeholder — so it stays one
    member and this share is the only instrument on it. Read out of the blessed artifact,
    never by re-querying production."""
    cells = fc.load_baseline()["cells"]
    for portal in ("sreality", "idnes", "remax", "ceskereality"):
        values = cells[f"{portal}/energy_rating"]["values"]
        assert values["G"] > 0 and values["G"] < cells[f"{portal}/energy_rating"]["filled"]


def test_attribute_fields_are_the_column_contract_minus_the_three_non_attributes() -> None:
    assert set(fc.ATTRIBUTE_FIELDS) | {"description", "published_at", "source_url"} == set(
        LISTING_COLUMNS
    )


def test_canon_comes_from_the_filter_registry_by_column_not_by_filter() -> None:
    """A filter is the wrong index: `building_material` is one over `building_type`
    whose values are BUCKET names, so a scan made `ostatni` a canonical construction,
    and `price_unit` has no filter at all yet carries a two-member canon."""
    assert "novostavba" in (fc.canonical_values("condition") or set())
    assert fc.canonical_values("price_unit") == {"za nemovitost", "za mesic"}
    assert "ostatni" not in (fc.canonical_values("building_type") or set())


# --- the reductions --------------------------------------------------------

_CENSUS_ROWS = [
    (100, "garaz", 60, 1, "Ano", 60),
    (100, "balkon", 40, 2, "Ano", 30),
    (100, "balkon", 40, 2, None, 10),
    (100, "cislo zakazky", 100, 100, None, None),
    (100, "delka", 0, 0, None, None),  # a key below the rare floor
]


def test_reduce_census_shapes_shares_values_and_the_rare_bucket() -> None:
    out = fc.reduce_census(_CENSUS_ROWS, portal="remax", generated_at=_WHEN)
    assert out["portal"] == "remax" and out["sample"]["n_rows"] == 100
    assert out["keys"]["garaz"] == {
        "n": 60, "pct": 60.0, "distinct": 1, "values": {"Ano": 60}
    }
    # A jsonb null is a VALUE, not an absence: idnes ships has_lift that way.
    assert out["keys"]["balkon"]["values"] == {"Ano": 30, fc.JSON_NULL: 10}
    # Above the cap the SQL returns no value rows, so the entry carries none.
    assert "values" not in out["keys"]["cislo zakazky"]
    assert out["rare"] == {"n_keys": 1, "keys": ["delka"]}


def test_reduce_census_does_not_depend_on_row_order() -> None:
    assert fc.reduce_census(
        list(reversed(_CENSUS_ROWS)), portal="remax", generated_at=_WHEN
    ) == fc.reduce_census(_CENSUS_ROWS, portal="remax", generated_at=_WHEN)


# (source, n_active, field, n_filled, n_distinct, n_off_canon, n_true, n_false,
#  value_counts, off_canon_values) — one row per cell, exactly what the aggregate emits.
_MATRIX_ROWS = [
    ("remax", 100, "has_balcony", 0, None, None, 0, 0, None, None),
    # The witness that makes `has_balcony` a boolean FIELD: remax's own cell is empty,
    # so a per-cell rule would report it as untyped exactly where it matters most.
    ("sreality", 100, "has_balcony", 50, None, None, 30, 20, None, None),
    ("remax", 100, "has_lift", 30, None, None, 30, 0, None, None),
    ("remax", 100, "condition", 90, 3, 9, 0, 0, {"velmi_dobry": 81, "dobry": 0}, ["spatny", "projekt"]),
    ("remax", 100, "price_unit", 100, None, None, 0, 0, None, None),
]


def test_reduce_matrix_scores_fill_validity_and_the_boolean_arms() -> None:
    cells = fc.reduce_matrix(_MATRIX_ROWS, generated_at=_WHEN)["cells"]
    assert cells["remax/has_balcony"] == {
        "n": 100, "filled": 0, "fill": 0.0, "true": 0, "false": 0
    }
    assert cells["remax/has_lift"]["true"] == 30 and cells["remax/has_lift"]["false"] == 0
    assert cells["remax/condition"]["off_canon"] == 0.1
    assert cells["remax/condition"]["off_canon_values"] == ["projekt", "spatny"]
    # A canonical member nobody wrote is not recorded as a zero — the file stays a diff.
    assert cells["remax/condition"]["values"] == {"velmi_dobry": 81}
    # No canon -> nothing to judge and nothing to enumerate; W5 gives `price_unit` one.
    assert "off_canon" not in cells["remax/price_unit"]
    assert "values" not in cells["remax/price_unit"]


def test_reduce_matrix_off_canon_share_is_counted_over_every_row() -> None:
    """The off-canon count is an aggregate over the whole stock, never a sum of the
    recorded values: a rogue spelling nobody enumerates is exactly how a validity check
    reads perfect."""
    rows = [("idnes", 100, "condition", 90, 40, 9, 0, 0, {"dobry": 81}, ["x"])]
    assert fc.reduce_matrix(rows, generated_at=_WHEN)["cells"]["idnes/condition"][
        "off_canon"
    ] == 0.1


# --- the regression arms ---------------------------------------------------


def _baseline(**cells: dict) -> dict:
    return {"generated_at": _WHEN.isoformat(), "cells": cells}


def test_live_equal_to_blessed_is_green() -> None:
    blessed = fc.load_baseline()
    assert fc.compare_to_baseline(blessed, blessed) == ([], [])


def test_a_fill_collapse_fails_and_a_smaller_slip_warns() -> None:
    base = _baseline(**{"idnes/condition": {"fill": 0.88, "filled": 880, "n": 1000}})
    fails, warns = fc.compare_to_baseline(
        _baseline(**{"idnes/condition": {"fill": 0.60, "filled": 600, "n": 1000}}), base)
    assert fails and not warns
    fails, warns = fc.compare_to_baseline(
        _baseline(**{"idnes/condition": {"fill": 0.75, "filled": 750, "n": 1000}}), base)
    assert warns and not fails


def test_a_weeks_cohort_drift_does_not_flap() -> None:
    """The stock's own drift, measured 2026-09-21 over 90 cells: the worst cell moved
    4.1 pp in a WEEK of arrivals (bezrealitky `disposition` 55.0% -> 50.9% against the
    rows already active a week earlier), and a 6-hourly run sees ~0.15 pp of that. Both
    arms must sit clear of it — which is why neither is sized on a binomial SE: the
    earlier newest-1,000 cohort rotated by 30+ pp on the same untouched parsers."""
    base = _baseline(**{"bezrealitky/disposition": {"fill": 0.55, "filled": 3148, "n": 5724}})
    live = _baseline(**{"bezrealitky/disposition": {"fill": 0.509, "filled": 2913, "n": 5724}})
    assert fc.compare_to_baseline(live, base) == ([], [])


def test_a_partial_break_below_the_absolute_floor_still_fails() -> None:
    """ceskereality's `furnished` key mismatch shape: a cell at 5% dropping to 1% is
    invisible to a 10 pp rule, and is a four-fold collapse."""
    base = _baseline(**{"ceskereality/furnished": {"fill": 0.05, "filled": 50, "n": 1000}})
    live = _baseline(**{"ceskereality/furnished": {"fill": 0.01, "filled": 10, "n": 1000}})
    fails, _ = fc.compare_to_baseline(live, base)
    assert len(fails) == 1 and "ceskereality/furnished" in fails[0]


def test_a_known_zero_cell_cannot_offend() -> None:
    base = _baseline(**{"remax/has_balcony": {"fill": 0.0, "filled": 0, "n": 1000}})
    assert fc.compare_to_baseline(base, base) == ([], [])


def test_an_off_canon_rise_rings_and_names_the_new_spellings() -> None:
    base = _baseline(**{"remax/condition": {"fill": 0.9, "filled": 900, "off_canon": 0.01}})
    live = _baseline(**{
        "remax/condition": {"fill": 0.9, "filled": 900, "off_canon": 0.2,
                            "off_canon_values": ["zdena_kamenna"]}})
    fails, _ = fc.compare_to_baseline(live, base)
    assert len(fails) == 1 and "zdena_kamenna" in fails[0]


def test_a_cell_the_baseline_has_never_seen_arrives_green() -> None:
    """A new portal or a new column is blessed by the next re-bless, not by a red run."""
    live = _baseline(**{"newportal/condition": {"fill": 0.0, "filled": 0, "n": 1000}})
    assert fc.compare_to_baseline(live, _baseline()) == ([], [])


def test_a_source_that_vanishes_from_the_matrix_fails_and_a_single_cell_warns() -> None:
    """The denominator arm. A portal disabled in `portals`, or one whose stock goes
    inactive, takes all its cells out of the spine — and a check that only walks LIVE
    cells would certify the silence."""
    base = _baseline(**{
        "remax/condition": {"fill": 0.8, "filled": 800, "n": 1000},
        "remax/floor": {"fill": 0.4, "filled": 400, "n": 1000},
        "idnes/condition": {"fill": 0.8, "filled": 800, "n": 1000},
    })
    fails, warns = fc.compare_to_baseline(
        _baseline(**{"idnes/condition": {"fill": 0.8, "filled": 800, "n": 1000}}), base)
    assert len(fails) == 1 and "remax: 2 blessed cell(s) absent" in fails[0]
    assert warns == []
    # One cell of a source that is otherwise present is a warn, not a fail.
    fails, warns = fc.compare_to_baseline(
        _baseline(**{
            "remax/condition": {"fill": 0.8, "filled": 800, "n": 1000},
            "idnes/condition": {"fill": 0.8, "filled": 800, "n": 1000},
        }), base)
    assert fails == [] and len(warns) == 1 and "remax: 1 blessed cell(s) absent" in warns[0]


def test_booleans_never_false_reports_the_count_and_never_a_verdict() -> None:
    matrix = fc.reduce_matrix(_MATRIX_ROWS, generated_at=_WHEN)
    assert fc.booleans_never_false(matrix) == ["remax/has_lift 30/100 true, 0 false"]


def test_zero_fill_cells_are_read_from_whatever_matrix_is_passed() -> None:
    """Live, in the check — so the run that repairs a cell is the run that stops naming
    it, instead of the day someone re-blesses."""
    matrix = fc.reduce_matrix(_MATRIX_ROWS, generated_at=_WHEN)
    assert fc.zero_fill_cells(matrix) == ["remax/has_balcony"]
    repaired = fc.reduce_matrix(
        [("remax", 100, "has_balcony", 40, None, None, 40, 0, None, None)],
        generated_at=_WHEN)
    assert fc.zero_fill_cells(repaired) == []


def test_stale_census_is_measured_in_days_against_the_stamp() -> None:
    fresh = {"portal": "remax", "generated_at": _WHEN.isoformat()}
    assert fc.stale_censuses([fresh], now=_WHEN + _dt.timedelta(days=29)) == []
    assert fc.stale_censuses([fresh], now=_WHEN + _dt.timedelta(days=31)) == [
        "remax census is 31d old"
    ]
    assert fc.stale_censuses(
        [{"portal": "remax", "generated_at": "never"}], now=_WHEN
    ) == ["remax has no readable generated_at"]


# --- the golden's own text -------------------------------------------------


def test_dumps_round_trips_and_keeps_one_line_per_entry() -> None:
    doc = {"portal": "x", "keys": {"b": {"n": 2}, "a": {"n": 1}}}
    text = fc.dumps(doc, line_maps=("keys",))
    assert json.loads(text) == doc
    assert '\n    "b": {"n": 2},\n    "a": {"n": 1}\n' in text


def test_the_checked_in_goldens_are_exactly_what_the_writer_emits() -> None:
    """Otherwise a hand-edited golden would survive review as a generated one."""
    for census in fc.load_censuses():
        path = fc.CENSUS_DIR / f"{census['portal']}.json"
        assert path.read_text(encoding="utf-8") == fc.dumps(census, line_maps=("keys",))
    assert fc.BASELINE_PATH.read_text(encoding="utf-8") == fc.dumps(
        fc.load_baseline(), line_maps=("cells",)
    )


def test_both_sql_constants_carry_only_named_placeholders() -> None:
    """The psycopg `%` trap this repo has been bitten by twice: a literal percent in
    SQL text is a format placeholder to psycopg and raises at execute time."""
    for sql in (fc.FIELD_CENSUS_SQL, fc.FIELD_MATRIX_SQL, fc.PORTAL_SOURCES_SQL):
        assert "%" not in sql.replace("%(source)s", "").replace("%(sample)s", "") \
            .replace("%(max_distinct)s", "")


def test_the_matrix_sql_measures_the_stock_and_carries_the_canon_it_judges_by() -> None:
    """The cohort is the alarm's whole credibility: a sampled one cannot be compared to
    itself a week later. And the canon is interpolated from `toolkit.filter_registry`, so
    a member added there is judged here without a second list."""
    sql = fc.FIELD_MATRIX_SQL
    assert "limit" not in sql and "first_seen_at" not in sql
    assert "where l.is_active" in sql
    assert "'novostavba'" in sql and "'G'" in sql
    for field in fc.ATTRIBUTE_FIELDS:
        assert f"count(l.{field})" in sql, field
