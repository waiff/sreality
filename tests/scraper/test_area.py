"""The one headline-area precedence, shared by all nine portals."""

from scraper.area import AREA_BASES, MIN_AREA_M2, derive_headline_area


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


def test_every_emitted_basis_is_in_the_declared_vocabulary():
    # The CHECK constraint in migration 423 accepts exactly these five.
    emitted = {
        derive_headline_area(category_main=c, **{k: 10.0})[1]
        for c in ("byt", "pozemek")
        for k in ("usable", "floor", "total", "fallback")
    }
    assert emitted <= AREA_BASES
