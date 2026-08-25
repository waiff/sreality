"""provable_basis() rules of the area_basis stamp — proof, never inference.

The stamp is a provenance claim about a value already stored. Getting it wrong
is worse than leaving it NULL, because a wrong stamp is indistinguishable from a
right one downstream. So every rule here has to be a PROOF that a specific arm
of `derive_headline_area` won, and everything else has to decline.
"""

from __future__ import annotations

import pytest

from scraper.area import AREA_BASES
from scripts.backfill_area_basis import (
    DECLINED,
    FALLBACK,
    LAND,
    NO_AREA,
    USABLE_COL,
    _COUNT_SQL,
    _FALLBACK_ONLY,
    _SELECT_SQL,
    _UNCOLLAPSED_USABLE,
    provable_basis,
)

# --- land: the one rule that needs no portal input at all --------------------


@pytest.mark.parametrize("source", [
    "sreality", "idnes", "ceskereality", "realitymix", "bazos",
    "mmreality", "remax", "maxima", "bezrealitky",
])
def test_land_with_an_area_is_plot_on_every_portal(source: str) -> None:
    # derive_headline_area's land arm stamps 'plot' on whichever measure the
    # page carried, and that value IS area_m2 — so which portal supplied it
    # cannot change the answer. This is what takes 'plot' off zero.
    basis, reason = provable_basis(source, "pozemek", 1200.0, None)
    assert (basis, reason) == ("plot", LAND)


def test_land_without_an_area_is_null_not_plot():
    # sreality's 39,371 pozemek rows: area_m2 IS NULL on all of them because the
    # parser offers only usable=, which sreality does not populate for land. A
    # basis describes area_m2; with no area there is nothing to stamp.
    assert provable_basis("sreality", "pozemek", None, None) == (None, LAND)


def test_land_ignores_a_usable_area_column_that_disagrees():
    # The land arm never consults usable_area's provenance — first truthy wins
    # and it is stamped 'plot' regardless.
    assert provable_basis("idnes", "pozemek", 900.0, 130.0) == ("plot", LAND)


def test_a_zero_area_is_a_placeholder_not_a_parcel():
    # derive_headline_area skips a falsy measure by design, so 0 m² land is
    # (None, None) — the stamp must be NULL, not 'plot'.
    assert provable_basis("idnes", "pozemek", 0.0, None) == (None, LAND)


# --- the anomaly this backfill exists to correct -----------------------------


def test_a_stamp_with_no_area_is_provably_wrong():
    # 8 live rows carry a basis derive_headline_area cannot produce: 7 sreality
    # pozemek with area_basis='usable' and area_m2 NULL. Both branches that
    # reach them return None, so the backfill clears the stamp rather than
    # treating an existing non-NULL value as authoritative.
    assert provable_basis("sreality", "pozemek", None, None)[0] is None
    assert provable_basis("sreality", "byt", None, None) == (None, NO_AREA)


# --- bazos: fallback is its only arm ----------------------------------------


def test_bazos_non_land_with_an_area_is_unknown():
    # bazos passes fallback= and nothing else, and writes no usable_area at all,
    # so a non-land row that has an area can only have come from that arm.
    assert provable_basis("bazos", "byt", 74.0, None) == ("unknown", FALLBACK)


def test_bazos_land_is_still_plot():
    assert provable_basis("bazos", "pozemek", 800.0, None) == ("plot", LAND)


# --- the usable column, only where it is not collapsed -----------------------


@pytest.mark.parametrize("source", sorted(_UNCOLLAPSED_USABLE))
def test_matching_usable_column_proves_the_usable_arm(source: str) -> None:
    assert provable_basis(source, "byt", 74.0, 74.0) == ("usable", USABLE_COL)


@pytest.mark.parametrize("source", sorted(_UNCOLLAPSED_USABLE))
def test_a_diverging_usable_column_proves_nothing(source: str) -> None:
    # mmreality's 3,588 damaged houses and realitymix's 3,396 stale byt rows
    # both live here: area_m2 and usable_area both present and different. The
    # winner was some other arm, or the row is a stale parse — either way the
    # columns do not say which.
    assert provable_basis(source, "dum", 905.0, 130.0) == (None, DECLINED)


@pytest.mark.parametrize("source", ["idnes", "ceskereality"])
def test_collapsed_portals_are_declined_even_on_an_exact_match(source: str) -> None:
    # THE fabrication guard, and the reason ~183,000 rows stay NULL. idnes
    # stores `užitná ?? podlahová ?? plocha` in usable_area and ceskereality
    # `plocha užitná ?? užitná plocha ?? plocha`, so an exact match proves only
    # that ONE OF THREE labels won — 'usable' would be a made-up provenance.
    assert provable_basis(source, "byt", 74.0, 74.0) == (None, DECLINED)


def test_the_collapsed_portals_are_absent_from_the_uncollapsed_set():
    # Pinned separately from the behaviour above: adding either portal to the
    # set is the single edit that would silently start fabricating.
    assert "idnes" not in _UNCOLLAPSED_USABLE
    assert "ceskereality" not in _UNCOLLAPSED_USABLE
    assert not (_UNCOLLAPSED_USABLE & _FALLBACK_ONLY)


def test_a_null_usable_column_is_declined_not_guessed():
    # The winner was floor / total / the title, none of which any column stores.
    assert provable_basis("realitymix", "byt", 88.0, None) == (None, DECLINED)


def test_an_unknown_portal_is_declined():
    assert provable_basis("someportal", "byt", 74.0, 74.0) == (None, DECLINED)


# --- vocabulary + query shape ------------------------------------------------


@pytest.mark.parametrize("args,expected", [
    (("bazos", "byt", 74.0, None), "unknown"),
    (("sreality", "byt", 74.0, 74.0), "usable"),
    (("idnes", "pozemek", 900.0, None), "plot"),
])
def test_every_written_token_comes_from_the_shared_vocabulary(args, expected):
    # The tokens are not spelled here — they are whatever derive_headline_area
    # returns, which is the point: this script has no vocabulary of its own.
    basis, _reason = provable_basis(*args)
    assert basis == expected
    assert basis in AREA_BASES


def test_the_read_names_no_wide_column():
    # `listings` carries 9.1 GB of TOAST and its two W2 siblings were both
    # cancelled by the statement timeout for detoasting a page at a time. This
    # one must stay a narrow primary-key walk.
    for sql in (_SELECT_SQL, _COUNT_SQL):
        assert "raw_json" not in sql
    assert "ORDER BY l.id" in _SELECT_SQL
    assert "LIMIT" in _SELECT_SQL


def test_the_selection_reaches_the_wrongly_stamped_land_rows():
    # `area_basis IS NULL` alone would walk straight past the 8 anomalies.
    assert "IS DISTINCT FROM 'plot'" in _SELECT_SQL
    assert "IS DISTINCT FROM 'plot'" in _COUNT_SQL
