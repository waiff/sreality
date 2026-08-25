"""The Python face of the measure, pinned against the SQL it mirrors.

`toolkit/measures.py` restates in Python what migration 425 states in SQL: the
four-token basis vocabulary, the per-basis price floors, the resolution order.
Two statements of one rule drift silently, so the matrix tests below enumerate
EVERY (category_main x category_type) combination the live data can produce and
assert the outcome the SQL CASE would give, and `_MEASURE_SQL` parity tests read
migration 425 itself rather than trusting a remembered number.

The one place the Python deliberately does NOT mirror the SQL is
`spec_ppm2_basis`: a filter spec's None means UNCONSTRAINED, not "this row has
no category", and the two readings part on the capital arm. That divergence is
the point of the function, so it is pinned here explicitly.
"""

from __future__ import annotations

import pathlib

import pytest

from toolkit.measures import (
    BASIS_MIXED,
    BASIS_UNKNOWN,
    LAND_CAPITAL_CZK_M2,
    PPM2_BASES,
    RENT_MONTHLY_CZK_M2,
    SALE_CAPITAL_CZK_M2,
    MeasureBasisError,
    cohort_basis,
    measure_backed,
    per_m2_basis_sql,
    per_m2_sql,
    ppm2_basis,
    price_floor_czk,
    require_scalable_basis,
    spec_ppm2_basis,
    unit_label,
)

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[2]
    / "migrations" / "425_measure_price_per_m2.sql"
)

# Every category_main the live data carries, plus the NULL rule 22 makes normal.
_CATEGORY_MAINS = (None, "byt", "dum", "pozemek", "komercni", "ostatni")
# All FOUR live category_type values, plus NULL and an unknown future value.
_CATEGORY_TYPES = (None, "prodej", "pronajem", "drazba", "podil", "smena")


def _sql_basis(category_main: str | None, category_type: str | None) -> str | None:
    """migration 425's CASE, transcribed. The oracle the mirror is checked against."""
    if category_type == "pronajem":
        return "rent_monthly_czk_m2"
    if category_main == "pozemek" and category_type in ("prodej", "drazba", "podil"):
        return "land_capital_czk_m2"
    if category_type in ("prodej", "drazba", "podil"):
        return "sale_capital_czk_m2"
    return None


# ---- the row mirror -------------------------------------------------------


@pytest.mark.parametrize("category_main", _CATEGORY_MAINS)
@pytest.mark.parametrize("category_type", _CATEGORY_TYPES)
def test_ppm2_basis_matches_the_sql_case_for_every_combination(
    category_main: str | None, category_type: str | None,
) -> None:
    assert ppm2_basis(category_main, category_type) == _sql_basis(
        category_main, category_type
    )


def test_the_three_capital_types_share_one_basis():
    """prodej / drazba / podil are all CAPITAL — one basis, not three."""
    assert {
        ppm2_basis("byt", t) for t in ("prodej", "drazba", "podil")
    } == {SALE_CAPITAL_CZK_M2}


def test_rent_wins_over_land_so_a_rented_plot_is_never_capital():
    assert ppm2_basis("pozemek", "pronajem") == RENT_MONTHLY_CZK_M2


def test_an_unknown_future_category_type_is_a_visible_gap_not_a_guess():
    assert ppm2_basis("byt", "smena") is None
    assert ppm2_basis("byt", None) is None


# ---- the spec reading, and where it parts from the row mirror -------------


@pytest.mark.parametrize("category_type", ("prodej", "drazba", "podil"))
def test_spec_basis_is_undecidable_without_a_pinned_category_main(
    category_type: str,
) -> None:
    """The whole reason the two functions exist separately.

    A capital spec that pins no category_main admits pozemek (Kc/m2 of PLOT)
    beside byt (Kc/m2 of FLOOR). The row mirror answers `sale` — correct for a
    row whose category_main really is NULL, a blanket unit for a cohort.
    """
    assert ppm2_basis(None, category_type) == SALE_CAPITAL_CZK_M2
    assert spec_ppm2_basis(None, category_type) is None


def test_spec_basis_rent_arm_needs_no_category_main():
    """Rent-first resolution never consults category_main, so it is decidable."""
    assert spec_ppm2_basis(None, "pronajem") == RENT_MONTHLY_CZK_M2


@pytest.mark.parametrize("category_main", ("byt", "dum", "pozemek", "komercni"))
@pytest.mark.parametrize("category_type", _CATEGORY_TYPES)
def test_spec_basis_agrees_with_the_row_mirror_once_category_main_is_pinned(
    category_main: str, category_type: str | None,
) -> None:
    assert spec_ppm2_basis(category_main, category_type) == ppm2_basis(
        category_main, category_type
    )


# ---- cohort resolution ----------------------------------------------------


def _row(basis: str | None, value: float | None = 500.0) -> dict[str, object]:
    return {"price_per_m2": value, "price_per_m2_basis": basis}


def test_cohort_basis_of_one_basis_is_that_basis():
    rows = [_row(SALE_CAPITAL_CZK_M2) for _ in range(3)]
    assert cohort_basis(rows) == SALE_CAPITAL_CZK_M2


def test_cohort_basis_of_two_bases_is_mixed():
    rows = [_row(SALE_CAPITAL_CZK_M2), _row(RENT_MONTHLY_CZK_M2)]
    assert cohort_basis(rows) == BASIS_MIXED


def test_land_and_sale_together_are_mixed_not_collapsed_to_capital():
    """Both are capital; they are not the same DENOMINATOR (plot vs floor)."""
    rows = [_row(SALE_CAPITAL_CZK_M2), _row(LAND_CAPITAL_CZK_M2)]
    assert cohort_basis(rows) == BASIS_MIXED


def test_cohort_basis_of_no_rows_is_unknown():
    assert cohort_basis([]) == BASIS_UNKNOWN


def test_one_unlabelled_row_makes_the_whole_cohort_unknown():
    """A cohort that is 90% sale is still not a sale cohort."""
    rows = [_row(SALE_CAPITAL_CZK_M2) for _ in range(9)] + [_row(None)]
    assert cohort_basis(rows) == BASIS_UNKNOWN


def test_caller_supplied_rows_without_the_key_are_unknown_never_sale():
    assert cohort_basis([{"price_per_m2": 500.0}]) == BASIS_UNKNOWN


def test_measure_backed_drops_rows_the_measure_withheld_a_number_from():
    rows = [
        _row(SALE_CAPITAL_CZK_M2, 500.0),
        _row(RENT_MONTHLY_CZK_M2, None),   # below the rent floor: no number
        {"price_per_m2": None},
    ]
    backed = measure_backed(rows)
    assert len(backed) == 1
    # The dropped rent row must not colour the label of a sale-only cohort.
    assert cohort_basis(backed) == SALE_CAPITAL_CZK_M2


# ---- the scaling gate -----------------------------------------------------


@pytest.mark.parametrize(
    "basis,kind",
    [
        (RENT_MONTHLY_CZK_M2, "rent"),
        (SALE_CAPITAL_CZK_M2, "sale"),
        (LAND_CAPITAL_CZK_M2, "sale"),
    ],
)
def test_require_scalable_basis_allows_the_agreeing_pairs(
    basis: str, kind: str,
) -> None:
    assert require_scalable_basis(basis, estimate_kind=kind) == basis


@pytest.mark.parametrize("basis", (BASIS_MIXED, BASIS_UNKNOWN, None, ""))
def test_require_scalable_basis_refuses_a_cohort_with_no_single_basis(
    basis: str | None,
) -> None:
    with pytest.raises(MeasureBasisError, match="no single per-m"):
        require_scalable_basis(basis, estimate_kind="rent")


@pytest.mark.parametrize(
    "basis,kind",
    [
        (SALE_CAPITAL_CZK_M2, "rent"),
        (LAND_CAPITAL_CZK_M2, "rent"),
        (RENT_MONTHLY_CZK_M2, "sale"),
    ],
)
def test_require_scalable_basis_refuses_a_basis_that_contradicts_the_kind(
    basis: str, kind: str,
) -> None:
    """The error worth catching: median x area is the same multiplication either
    way, so only the NAME of the product can be wrong."""
    with pytest.raises(MeasureBasisError, match="cannot be scaled"):
        require_scalable_basis(basis, estimate_kind=kind)


def test_require_scalable_basis_refuses_an_unknown_estimate_kind():
    with pytest.raises(MeasureBasisError, match="unknown estimate_kind"):
        require_scalable_basis(RENT_MONTHLY_CZK_M2, estimate_kind="yield")


# ---- units and floors -----------------------------------------------------


def test_every_basis_has_exactly_one_unit_and_the_non_bases_have_none():
    assert unit_label(RENT_MONTHLY_CZK_M2) == "Kč/m²/měs"
    assert unit_label(SALE_CAPITAL_CZK_M2) == "Kč/m²"
    assert unit_label(LAND_CAPITAL_CZK_M2) == "Kč/m² pozemku"
    assert len({unit_label(b) for b in PPM2_BASES}) == len(PPM2_BASES)
    for non_basis in (BASIS_MIXED, BASIS_UNKNOWN, None, ""):
        assert unit_label(non_basis) is None


def test_floors_match_migration_425():
    sql = _MIGRATION.read_text(encoding="utf-8")
    assert "p_category_type = 'pronajem' AND p_price >= 1000" in sql
    assert "AND p_price >= 100000" in sql
    assert price_floor_czk(RENT_MONTHLY_CZK_M2) == 1_000.0
    assert price_floor_czk(SALE_CAPITAL_CZK_M2) == 100_000.0
    # Land is deliberately unfloored: a cheap plot is a real plot.
    assert price_floor_czk(LAND_CAPITAL_CZK_M2) is None
    assert price_floor_czk(BASIS_MIXED) is None
    assert price_floor_czk(None) is None


def test_the_vocabulary_is_spelled_identically_in_python_and_in_sql():
    sql = _MIGRATION.read_text(encoding="utf-8")
    for token in PPM2_BASES:
        assert f"'{token}'" in sql, token
    # `mixed` / `unknown` are COHORT states, never returned by the SQL label.
    assert "'unknown'" not in sql.split("measure_price_per_m2_basis(")[1][:600]


# ---- the SQL fragments ----------------------------------------------------


def test_the_fragments_always_render_the_four_argument_call():
    """`listings` has no price_per_m2 column, so the column form is a 42703."""
    assert per_m2_sql("l") == (
        "measure_price_per_m2(l.price_czk::numeric, l.area_m2::numeric, "
        "l.category_main, l.category_type)"
    )
    assert per_m2_basis_sql("x") == (
        "measure_price_per_m2_basis(x.category_main, x.category_type)"
    )


def test_alias_is_required_so_a_unit_blind_call_cannot_be_written():
    with pytest.raises(TypeError):
        per_m2_sql()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        per_m2_basis_sql()  # type: ignore[call-arg]
