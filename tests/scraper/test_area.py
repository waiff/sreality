from scraper.area import derive_headline_area


def test_usable_wins_when_present():
    assert derive_headline_area(category_main="byt", usable=70.0, floor=72.0, total=80.0) == (70.0, "usable")


def test_floor_when_no_usable():
    assert derive_headline_area(category_main="byt", usable=None, floor=72.0, total=80.0) == (72.0, "floor")


def test_total_when_no_usable_or_floor():
    assert derive_headline_area(category_main="dum", usable=None, floor=None, total=120.0) == (120.0, "total")


def test_fallback_is_unknown():
    assert derive_headline_area(category_main="byt", usable=None, fallback=55.0) == (55.0, "unknown")


def test_none_when_nothing():
    assert derive_headline_area(category_main="byt", usable=None) == (None, None)


def test_land_is_always_null_even_with_a_value():
    # A plot has no interior area; the parcel lives in estate_area, never area_m2.
    assert derive_headline_area(category_main="pozemek", usable=400.0, fallback=400.0) == (None, None)


def test_commercial_keeps_interior_area():
    assert derive_headline_area(category_main="komercni", usable=None, total=250.0) == (250.0, "total")
