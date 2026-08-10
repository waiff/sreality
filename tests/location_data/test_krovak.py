"""The coordinate sign trap: the golden point AND the negative control.

The failure this file exists to prevent is silent — feeding the CSV's raw positive
ordinates to a transformer as EPSG:5513 yields a perfectly plausible coordinate that
happens to be in Germany. So both branches are asserted: the audited path lands inside
Prague Castle, and the wrong path is rejected by the guard.
"""

from __future__ import annotations

import pyproj
import pytest

from location_data import krovak

GOLDEN_LAT, GOLDEN_LON = krovak.GOLDEN_WGS84
GERMANY_LAT, GERMANY_LON = 52.278490, 9.471582


def test_golden_point_round_trip():
    point = krovak.KrovakPositive(*krovak.GOLDEN_KROVAK_POSITIVE)
    wgs = krovak.krovak_positive_to_wgs84(point)
    assert krovak.haversine_m(wgs, krovak.Wgs84Point(GOLDEN_LAT, GOLDEN_LON)) < 1.0
    assert krovak.golden_point_error_m(wgs.lat, wgs.lon) <= krovak.GOLDEN_TOLERANCE_M


def test_sign_flip_is_the_only_conversion():
    point = krovak.KrovakPositive(*krovak.GOLDEN_KROVAK_POSITIVE)
    assert point.to_epsg5514() == (-744384.54, -1042569.73)


def test_negative_control_raw_positives_as_5513_land_in_germany():
    """Documents the trap numerically and proves the guard rejects that path."""
    wrong = pyproj.Transformer.from_crs("EPSG:5513", "EPSG:4326", always_xy=True)
    lon, lat = wrong.transform(*krovak.GOLDEN_KROVAK_POSITIVE)

    assert lat == pytest.approx(GERMANY_LAT, abs=1e-4)
    assert lon == pytest.approx(GERMANY_LON, abs=1e-4)

    assert not krovak.in_czechia(lat, lon)
    with pytest.raises(krovak.KrovakSignError):
        krovak.assert_in_czechia(lat, lon, what="unflipped CSV ordinates")

    audited = krovak.krovak_positive_to_wgs84(
        krovak.KrovakPositive(*krovak.GOLDEN_KROVAK_POSITIVE)
    )
    assert krovak.haversine_m(
        audited, krovak.Wgs84Point(GERMANY_LAT, GERMANY_LON)
    ) > 300_000


def test_epsg5514_negatives_are_refused_by_the_positive_type():
    with pytest.raises(krovak.KrovakSignError):
        krovak.KrovakPositive(-744384.54, -1042569.73)


def test_from_epsg5514_adopts_vfr_negatives():
    point = krovak.KrovakPositive.from_epsg5514(-744384.54, -1042569.73)
    assert point == krovak.KrovakPositive(744384.54, 1042569.73)
    with pytest.raises(krovak.KrovakSignError):
        krovak.KrovakPositive.from_epsg5514(744384.54, 1042569.73)


@pytest.mark.parametrize(
    "y,x",
    [("", "1042569.73"), ("744384.54", ""), (None, None), ("abc", "1042569.73")],
)
def test_from_csv_blank_or_unparsable_is_none(y, x):
    assert krovak.KrovakPositive.from_csv(y, x) is None


def test_from_csv_accepts_comma_decimals():
    point = krovak.KrovakPositive.from_csv("744384,54", "1042569,73")
    assert point == krovak.KrovakPositive(744384.54, 1042569.73)


def test_pipeline_is_the_one_metre_path_not_the_six_metre_one():
    env = krovak.proj_environment()
    assert float(env["proj_accuracy_m"]) <= krovak.MAX_PIPELINE_ACCURACY_M
    assert "S-JTSK to WGS 84 (3)" not in env["proj_pipeline"]
    assert env["proj_version"].startswith("PROJ ")


def test_measured_envelope_sits_inside_the_sanity_envelope():
    assert krovak.KROVAK_Y_MIN <= krovak.MEASURED_Y_ENVELOPE[0]
    assert krovak.MEASURED_Y_ENVELOPE[1] <= krovak.KROVAK_Y_MAX
    assert krovak.KROVAK_X_MIN <= krovak.MEASURED_X_ENVELOPE[0]
    assert krovak.MEASURED_X_ENVELOPE[1] <= krovak.KROVAK_X_MAX
