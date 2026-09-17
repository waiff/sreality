"""The one area grammar and the one headline-area precedence, shared by all nine portals."""

from scraper.area import (
    AREA_BASES,
    MAX_AREA_M2,
    MIN_AREA_M2,
    derive_headline_area,
    parse_area_text,
)


# ---------------------------------------------------------------------------
# The grammar (W19). It was five private copies before, four of them the naive
# form that matched the first bare digit run and read "5 870 m²" as 870 —
# 17,207 ceskereality + 13,164 realitymix rows in production. The cases below
# came from idnes's own 2026-08 truncation incident and now guard every portal.
# ---------------------------------------------------------------------------


def test_spaced_thousands_are_one_number():
    assert parse_area_text("5 870 m²") == 5870.0
    assert parse_area_text("1 200,5 m2") == 1200.5
    assert parse_area_text("plocha 12 345 m²") == 12_345.0
    assert parse_area_text("Prodej pole 2 403 m²") == 2403.0
    assert parse_area_text("Prodej pozemku 10 000 m²") == 10_000.0
    assert parse_area_text("Prodej louky 1 074,5 m²") == 1074.5


def test_every_separator_a_portal_renders():
    # remax + ceskereality serve NBSP, realitymix a narrow NBSP, some pages a thin
    # space; idnes emits zero-width joiners inside a rendered figure.
    assert parse_area_text("1\u00a0063 m²") == 1063.0
    assert parse_area_text("12\u202f345 m²") == 12_345.0
    assert parse_area_text("9\u2009800 m²") == 9800.0
    assert parse_area_text("5\u200b870 m²") == 5870.0


def test_plain_numbers_are_unchanged():
    assert parse_area_text("80 m2") == 80.0
    assert parse_area_text("48,5 m²") == 48.5
    assert parse_area_text("Prodej stavební parcely 720 m2") == 720.0
    # The dl text variant "m 2" (from "m<sup>2</sup>") still parses.
    assert parse_area_text("1074 m 2") == 1074.0
    # A decimal is never re-entered mid-number ("1,5 m²" is not "5 m²").
    assert parse_area_text("1,5 m²") == 1.5


def test_a_disposition_digit_is_never_swallowed():
    assert parse_area_text("3+1 174 m²") == 174.0
    assert parse_area_text("Prodej bytu 3+1 174 m²") == 174.0


def test_the_first_complete_token_wins_not_a_fragment():
    assert parse_area_text("pozemky 350 a 1 200 m²") == 1200.0


def test_a_per_m2_price_is_not_an_area():
    # The digits sit AFTER the unit, so there is no area token at all here.
    assert parse_area_text("Cena za m2: 7 759 CZK") is None
    assert parse_area_text("4 990 000 Kč (4 008 Kč/m²)") is None
    # ... and a per-m² note beside a real area must not displace it.
    assert parse_area_text("80 m2, cena 50 000 Kč/m2") == 80.0


def test_no_area_at_all():
    assert parse_area_text(None) is None
    assert parse_area_text("") is None
    assert parse_area_text("Prodej bytu 3+1, Praha 5") is None


def test_usable_wins_when_present():
    assert derive_headline_area(
        category_main="byt", usable=70.0, floor=72.0, total=80.0
    ) == (70.0, "usable")


def test_floor_when_no_usable():
    assert derive_headline_area(
        category_main="byt", usable=None, floor=72.0, total=80.0
    ) == (72.0, "floor")


def test_total_when_no_usable_or_floor():
    assert derive_headline_area(
        category_main="dum", usable=None, floor=None, total=120.0
    ) == (120.0, "total")


def test_fallback_is_unknown():
    assert derive_headline_area(category_main="byt", usable=None, fallback=55.0) == (
        55.0,
        "unknown",
    )


def test_none_when_nothing():
    assert derive_headline_area(category_main="byt", usable=None) == (None, None)


def test_zero_is_a_placeholder_not_a_measure():
    # The per-portal `or` chains this replaces skipped 0.0; so does the resolver.
    assert derive_headline_area(category_main="byt", usable=0.0, total=64.0) == (
        64.0,
        "total",
    )


def test_land_keeps_its_value_and_is_stamped_plot():
    # Option A: area_m2 is POLYMORPHIC. A parcel's area stays in area_m2 — NULLing
    # it would be deletion on the portals that write no estate_area, and area_m2 is
    # hashed, so the rewrite would churn a snapshot per land listing for a non-event.
    assert derive_headline_area(category_main="pozemek", usable=400.0) == (400.0, "plot")


def test_land_prefers_the_plot_shaped_measure():
    # On a land page "celková plocha" IS the parcel; a stray "užitná plocha" on one
    # is a mislabel of the same number.
    assert derive_headline_area(
        category_main="pozemek", usable=400.0, total=1074.0
    ) == (1074.0, "plot")


def test_land_takes_the_labelled_parcel_over_every_other_measure():
    # W17: the portal's own "plocha pozemku" / surfaceLand / estate_area leads the
    # land arm. 52,183 land rows (sreality 44,237, idnes 5,292, bezrealitky 2,654)
    # carried exactly this input and no headline at all, because three parsers only
    # ever handed the resolver an interior measure land pages do not state.
    assert derive_headline_area(
        category_main="pozemek", usable=400.0, floor=410.0, total=1074.0,
        plot=1200.0, fallback=99.0,
    ) == (1200.0, "plot")
    assert derive_headline_area(category_main="pozemek", plot=1200.0) == (1200.0, "plot")


def test_the_plot_is_never_a_dwellings_headline():
    # A house's parcel sits BESIDE its floor area (estate_area) and must never
    # become the headline — that is the mmreality defect the divergence check
    # watches for. The dwelling arm does not read `plot` at all.
    assert derive_headline_area(
        category_main="dum", usable=148.0, plot=905.0
    ) == (148.0, "usable")
    assert derive_headline_area(category_main="dum", plot=905.0) == (None, None)
    assert derive_headline_area(category_main="byt", plot=905.0) == (None, None)
    assert derive_headline_area(category_main=None, plot=905.0) == (None, None)


def test_land_from_free_text_only_is_still_plot():
    # The bazos shape: no structured area field anywhere, only the title/description
    # scrape. Nothing is deleted — the value survives, labelled for what it is.
    assert derive_headline_area(category_main="pozemek", fallback=812.0) == (
        812.0,
        "plot",
    )


def test_commercial_keeps_interior_area():
    assert derive_headline_area(category_main="komercni", total=250.0) == (250.0, "total")


def test_unknown_category_takes_the_dwelling_path():
    assert derive_headline_area(category_main=None, usable=70.0) == (70.0, "usable")


def test_a_sub_metre_measure_is_declined_on_a_dwelling():
    # 527 active byt/dum/komercni rows store an area_m2 under 5 m2 — a title-number
    # garble or a per-m2 note read as the area, never a unit. The bound lives in the
    # resolver, not at the write boundary, so the refusal reaches the content hash.
    assert derive_headline_area(category_main="byt", usable=3.0) == (None, None)
    assert derive_headline_area(category_main="dum", usable=1.0, total=180.0) == (
        180.0,
        "total",
    )
    assert derive_headline_area(category_main="komercni", total=MIN_AREA_M2) == (
        MIN_AREA_M2,
        "total",
    )


def test_the_bound_does_not_reach_small_units_or_land():
    # A 3 m2 cellar or a 2 m2 parking bay is `ostatni` and is REAL; so are the 202
    # active parcels under 5 m2. Bounding those would be deletion, not validation.
    assert derive_headline_area(category_main="ostatni", usable=3.0) == (3.0, "usable")
    assert derive_headline_area(category_main="pozemek", total=4.0) == (4.0, "plot")
    assert derive_headline_area(category_main=None, fallback=2.0) == (2.0, "unknown")


def test_a_measure_the_column_cannot_hold_is_declined_on_every_category():
    # `listings.area_m2` is numeric(7,1): the write boundary NULLs anything at or
    # beyond 10^6 (scraper.db.sane_listing_numerics). Declining it HERE means the
    # resolver falls through instead of stamping a basis for a value the row will
    # not hold — production carries 20 land rows whose parcel is that big (up to
    # 16,809,800 m2), and their parcel belongs in estate_area, not the headline.
    assert derive_headline_area(category_main="pozemek", plot=16_809_800.0) == (None, None)
    assert derive_headline_area(
        category_main="pozemek", plot=16_809_800.0, total=1200.0
    ) == (1200.0, "plot")
    assert derive_headline_area(category_main="byt", usable=MAX_AREA_M2) == (None, None)
    assert derive_headline_area(
        category_main="byt", usable=MAX_AREA_M2, total=64.0
    ) == (64.0, "total")
    # the last value the column DOES hold is still a measure
    assert derive_headline_area(category_main="pozemek", plot=999_999.9) == (999_999.9, "plot")


def test_the_ceiling_is_the_column_bound_the_write_boundary_enforces():
    # Imported HERE and not in scraper/area.py, which is stdlib-only: the test is
    # what keeps the two spellings of one column bound from drifting, the same way
    # `_NUMERIC_ABS_MAX` itself is pinned to LISTING_COLUMNS by an assert.
    from scraper.db import _NUMERIC_ABS_MAX

    assert MAX_AREA_M2 == float(_NUMERIC_ABS_MAX["area_m2"])


def test_every_emitted_basis_is_in_the_declared_vocabulary():
    # The CHECK constraint in migration 423 accepts exactly these five.
    emitted = {
        derive_headline_area(category_main=c, **{k: 10.0})[1]
        for c in ("byt", "pozemek")
        for k in ("usable", "floor", "total", "plot", "fallback")
    }
    # `plot` on a dwelling is not a measure that arm reads, so it emits no basis at
    # all — a legal answer, and not a token. Every token emitted must be declared.
    assert emitted - {None} <= AREA_BASES
    assert {"usable", "floor", "total", "plot", "unknown"} <= emitted
