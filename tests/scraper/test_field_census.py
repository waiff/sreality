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
    """9/9. The portal set comes from the factory table, not a tenth list here."""
    blessed = {c["portal"] for c in fc.load_censuses()}
    assert blessed == set(PORTAL_CLASSES)


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
    cells = fc.load_baseline()["cells"]
    assert set(cells) == {
        fc.cell_key(p, f) for p in PORTAL_CLASSES for f in fc.ATTRIBUTE_FIELDS
    }


def test_baseline_records_the_known_zero_cells() -> None:
    """The cells this whole wave exists for: a parser reading a key the portal has
    never emitted. They are blessed as zero so the check is green on day one — and
    named in its report on every run until W2 declares a producer for each."""
    known = set(fc.known_zero_cells(fc.load_baseline()))
    assert {
        "remax/has_balcony", "mmreality/has_balcony",
        "ceskereality/has_parking", "ceskereality/garage", "ceskereality/terrace",
        "ceskereality/parking_lots", "ceskereality/total_floors",
        "realitymix/has_lift",
    } <= known


def test_attribute_fields_are_the_column_contract_minus_the_three_non_attributes() -> None:
    assert set(fc.ATTRIBUTE_FIELDS) | {"description", "published_at", "source_url"} == set(
        LISTING_COLUMNS
    )


def test_canon_comes_from_the_filter_registry_and_price_unit_has_none() -> None:
    """`price_unit` is the one enum-shaped column no filter constrains, so W1 can count
    its vocabulary but not judge it — the collapse to two members is W5's."""
    assert "novostavba" in (fc.canonical_values("condition") or set())
    assert fc.canonical_values("price_unit") is None
    assert json.loads(fc.canon_param()).keys() <= set(fc.ATTRIBUTE_FIELDS)


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


_MATRIX_ROWS = [
    (100, "remax", "has_balcony", 0, 0, 0, None, None, None),
    # The witness that makes `has_balcony` a boolean FIELD: remax's own cell is empty,
    # so a per-cell rule would report it as untyped exactly where it matters most.
    (100, "sreality", "has_balcony", 50, 2, 0, "true", 30, False),
    (100, "sreality", "has_balcony", 50, 2, 0, "false", 20, False),
    (100, "remax", "has_lift", 30, 1, 0, "true", 30, False),
    (100, "remax", "condition", 90, 3, 9, "velmi_dobry", 81, False),
    (100, "remax", "condition", 90, 3, 9, "spatny", 6, True),
    (100, "remax", "condition", 90, 3, 9, "projekt", 3, True),
    (100, "remax", "price_unit", 100, 1, 0, "za nemovitost", 100, False),
]


def test_reduce_matrix_scores_fill_validity_and_the_boolean_arms() -> None:
    cells = fc.reduce_matrix(_MATRIX_ROWS, generated_at=_WHEN)["cells"]
    assert cells["remax/has_balcony"] == {
        "n": 100, "filled": 0, "fill": 0.0, "distinct": 0, "true": 0, "false": 0
    }
    assert cells["remax/has_lift"]["true"] == 30 and cells["remax/has_lift"]["false"] == 0
    assert cells["remax/condition"]["off_canon"] == 0.1
    assert cells["remax/condition"]["off_canon_values"] == ["projekt", "spatny"]
    # No canon -> the vocabulary itself is the evidence, and nothing is judged.
    assert "off_canon" not in cells["remax/price_unit"]
    assert cells["remax/price_unit"]["values"] == {"za nemovitost": 100}


def test_reduce_matrix_off_canon_share_comes_from_sql_not_the_capped_value_rows() -> None:
    """A rogue spelling ranked outside the value cap must still count: computing the
    share from the returned rows is exactly how a validity check reads perfect."""
    rows = [(100, "idnes", "condition", 90, 40, 9, "dobry", 81, False)]
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


def test_sampling_noise_at_the_measured_scale_does_not_flap() -> None:
    """Three standard errors at n=1,000 is under 5 pp; the warn tier sits at 10."""
    base = _baseline(**{"idnes/condition": {"fill": 0.88, "filled": 880, "n": 1000}})
    live = _baseline(**{"idnes/condition": {"fill": 0.833, "filled": 833, "n": 1000}})
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


def test_booleans_never_false_reports_the_count_and_never_a_verdict() -> None:
    matrix = fc.reduce_matrix(_MATRIX_ROWS, generated_at=_WHEN)
    assert fc.booleans_never_false(matrix) == ["remax/has_lift 30/100 true, 0 false"]


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
            .replace("%(max_distinct)s", "").replace("%(canon)s", "")
