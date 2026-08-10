"""Křovák (S-JTSK) coordinates as a typed value object with ONE audited WGS84 conversion.

THE SIGN TRAP (design 04 C3, recon/ext-ruian-cuzk.md §4.1) — read before touching this file.

ČÚZK publishes the same physical point under two opposite conventions:

    RÚIAN address CSV   "Souřadnice Y" = 744384.54, "Souřadnice X" = 1042569.73   POSITIVE
    VFR XML (EPSG:5514) <gml:pos>-618016.86 -1203177.88</gml:pos>                 NEGATIVE

EPSG:5514 ("S-JTSK / Krovak East North") is defined with easting=X, northing=Y, both
negative over Czechia. Classic Křovák — what the CSV prints — is the south-west oriented
system with both ordinates positive. The conversion is a pure sign flip:

    easting_5514  = -(Souřadnice Y)
    northing_5514 = -(Souřadnice X)

Feeding the raw CSV positives to a transformer as if they were EPSG:5513 yields
52.278490 N / 9.471582 E — a valid-looking coordinate *in Germany*. The failure is silent
and plausible, which is why raw floats may never reach a `ST_SetSRID(..., 5514)`: the CSV
ordinates parse into `KrovakPositive` and leave only through `krovak_positive_to_wgs84`,
which refuses any result outside the CZ bounding box.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass

import pyproj

# The single canonical CZ bounding box for the location subsystem (design 05 §5.3.5: "one
# shared constant, exported from the registry module"). Values are the prevailing copy the
# eight portal parsers already share (scraper/street.py), so W1 adds no seventh definition.
CZ_LAT_MIN, CZ_LAT_MAX = 48.0, 51.5
CZ_LON_MIN, CZ_LON_MAX = 12.0, 19.0

# Outer sanity envelope for POSITIVE Křovák ordinates — the same numbers as the
# `ruian_ap_krovak_envelope` CHECK in 01 §3.5, so a sign error fails here and at write time.
KROVAK_Y_MIN, KROVAK_Y_MAX = 400_000.0, 950_000.0
KROVAK_X_MIN, KROVAK_X_MAX = 900_000.0, 1_250_000.0

# Measured over all 3,020,222 rows of the 20260731 vintage (ext-ruian-cuzk.md §1.3).
# First-load seed only: every later load is anchored to the previous good load (04 §4.5.3).
MEASURED_Y_ENVELOPE = (432_064.28, 901_942.00)
MEASURED_X_ENVELOPE = (936_371.33, 1_219_794.01)

# The one deliberately absolute load-time assertion (04 §4.5.3): a fixed physical location,
# triple-confirmed (CSV negate-and-transform, GeocodeSOE reverse, MapServer spatial query).
GOLDEN_KOD_ADM = 21690278
GOLDEN_KROVAK_POSITIVE = (744384.54, 1042569.73)
GOLDEN_WGS84 = (50.089480, 14.398606)
GOLDEN_TOLERANCE_M = 5.0

# Pipeline (3) is the 6.0 m one PROJ tends to pick first and which C3.1 rule 3 says to
# avoid; (5)/(1)/(4) are the 1.0 m Helmert paths.
_PREFERRED_PIPELINES = ("S-JTSK to WGS 84 (5)", "S-JTSK to WGS 84 (1)", "S-JTSK to WGS 84 (4)")
MAX_PIPELINE_ACCURACY_M = 1.0

_EARTH_RADIUS_M = 6_371_008.8


class KrovakSignError(ValueError):
    """A coordinate failed the sign / envelope guard — the Germany failure mode."""


@dataclass(frozen=True, slots=True)
class Wgs84Point:
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class KrovakPositive:
    """A positive-convention Křovák ordinate pair, as published in the RÚIAN CSV."""

    y: float
    x: float

    def __post_init__(self) -> None:
        if not (KROVAK_Y_MIN <= self.y <= KROVAK_Y_MAX):
            raise KrovakSignError(
                f"Souřadnice Y={self.y} outside the positive-Křovák envelope "
                f"[{KROVAK_Y_MIN}, {KROVAK_Y_MAX}] — negative values mean EPSG:5514 was "
                "passed where the CSV convention is expected"
            )
        if not (KROVAK_X_MIN <= self.x <= KROVAK_X_MAX):
            raise KrovakSignError(
                f"Souřadnice X={self.x} outside the positive-Křovák envelope "
                f"[{KROVAK_X_MIN}, {KROVAK_X_MAX}]"
            )

    @classmethod
    def from_csv(cls, y: str | None, x: str | None) -> KrovakPositive | None:
        """Parse two CSV cells; None when either is blank (920 of 3.02 M rows)."""
        yv, xv = _parse_ordinate(y), _parse_ordinate(x)
        if yv is None or xv is None:
            return None
        return cls(y=yv, x=xv)

    @classmethod
    def from_epsg5514(cls, easting: float, northing: float) -> KrovakPositive:
        """Adopt a VFR XML pair (both negative) into the positive convention."""
        if easting > 0 or northing > 0:
            raise KrovakSignError(
                f"EPSG:5514 ordinates must both be negative, got ({easting}, {northing}) — "
                "positive values are the CSV convention, use KrovakPositive directly"
            )
        return cls(y=-easting, x=-northing)

    def to_epsg5514(self) -> tuple[float, float]:
        """(easting, northing) in EPSG:5514 — the sign flip, stated once."""
        return (-self.y, -self.x)


def _parse_ordinate(value: str | None) -> float | None:
    text = (value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


@functools.lru_cache(maxsize=1)
def _transformer() -> tuple[pyproj.Transformer, str, float]:
    group = pyproj.transformer.TransformerGroup("EPSG:5514", "EPSG:4326", always_xy=True)
    candidates = [t for t in group.transformers if (t.accuracy or 99.0) <= MAX_PIPELINE_ACCURACY_M]
    if not candidates:
        raise KrovakSignError(
            "no S-JTSK -> WGS84 transformation at or below "
            f"{MAX_PIPELINE_ACCURACY_M} m is available in this PROJ install"
        )
    chosen = next(
        (t for name in _PREFERRED_PIPELINES for t in candidates if name in t.description),
        min(candidates, key=lambda t: t.accuracy or 99.0),
    )
    return chosen, chosen.description, chosen.accuracy or 99.0


def wgs84_transformer() -> pyproj.Transformer:
    """The same explicitly-chosen 1 m pipeline, for callers that transform whole geometries
    (boundary polygons) rather than address points. Never let PROJ pick — its default here
    is the 6 m pipeline."""
    transformer, _, _ = _transformer()
    return transformer


def proj_environment() -> dict[str, str]:
    """What went into `registry_versions.proj_version` / `proj_pipeline` (04 C3.1 rule 3)."""
    _, description, accuracy = _transformer()
    return {
        "proj_version": f"PROJ {pyproj.proj_version_str} / pyproj {pyproj.__version__}",
        "proj_pipeline": description,
        "proj_accuracy_m": str(accuracy),
    }


def krovak_positive_to_wgs84(point: KrovakPositive) -> Wgs84Point:
    """The ONE audited conversion. Negates, transforms on an explicitly chosen 1 m
    pipeline, and refuses a result outside Czechia."""
    easting, northing = point.to_epsg5514()
    transformer, _, _ = _transformer()
    lon, lat = transformer.transform(easting, northing)
    if not in_czechia(lat, lon):
        raise KrovakSignError(
            f"Křovák ({point.y}, {point.x}) transformed to ({lat:.6f}, {lon:.6f}), outside "
            "the CZ bounding box — the classic symptom of an unflipped sign"
        )
    return Wgs84Point(lat=lat, lon=lon)


def in_czechia(lat: float, lon: float) -> bool:
    return CZ_LAT_MIN <= lat <= CZ_LAT_MAX and CZ_LON_MIN <= lon <= CZ_LON_MAX


def assert_in_czechia(lat: float, lon: float, *, what: str) -> None:
    if not in_czechia(lat, lon):
        raise KrovakSignError(f"{what} at ({lat:.6f}, {lon:.6f}) is outside the CZ bounding box")


def haversine_m(a: Wgs84Point, b: Wgs84Point) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))


def golden_point_error_m(lat: float, lon: float) -> float:
    """Distance from the Prague Castle reference — the blocking load-time control."""
    return haversine_m(Wgs84Point(lat=lat, lon=lon), Wgs84Point(*GOLDEN_WGS84))
