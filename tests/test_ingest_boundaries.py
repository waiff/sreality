"""Hermetic tests for scripts/ingest_boundaries.

These tests cover the parsing / coercion / shapefile-reading path with
synthetic in-memory shapefiles. The DB write path and spatial-join SQL
are exercised by the real ingest run; not unit-tested here.

Skipped when geo deps are absent — the default install does not include
them (see the [project.optional-dependencies] geo group in pyproject).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("shapefile")
pytest.importorskip("shapely")
pytest.importorskip("pyproj")

import shapefile as pyshp  # noqa: E402
import shapely.geometry  # noqa: E402
import shapely.wkt  # noqa: E402
from pyproj import Transformer  # noqa: E402

from scripts.ingest_boundaries import (  # noqa: E402
    LEVELS,
    SIMPLIFY_TOLERANCE_DEG,
    BoundaryRow,
    field_index,
    find_shapefile,
    iter_boundary_rows,
    to_multipolygon,
)


# ---------- field_index ----------


def test_field_index_exact_match():
    assert field_index(["KOD", "NAZEV"], ("KOD",)) == 0


def test_field_index_case_insensitive():
    assert field_index(["Kod", "Nazev"], ("KOD",)) == 0


def test_field_index_priority_order():
    # First candidate wins even if a later one would also match.
    assert field_index(["NAZEV", "KOD"], ("KOD", "NAZEV")) == 1


def test_field_index_no_match_returns_none():
    assert field_index(["A", "B"], ("KOD",)) is None


# ---------- to_multipolygon ----------


def test_to_multipolygon_from_polygon():
    poly = shapely.geometry.Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    result = to_multipolygon(poly)
    assert isinstance(result, shapely.geometry.MultiPolygon)
    assert len(result.geoms) == 1


def test_to_multipolygon_passes_through_multi():
    a = shapely.geometry.Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    b = shapely.geometry.Polygon([(2, 2), (3, 2), (3, 3), (2, 3)])
    multi = shapely.geometry.MultiPolygon([a, b])
    result = to_multipolygon(multi)
    assert len(result.geoms) == 2


def test_to_multipolygon_repairs_self_intersecting():
    # bow-tie self-intersection
    bowtie = shapely.geometry.Polygon([(0, 0), (2, 2), (0, 2), (2, 0)])
    assert not bowtie.is_valid
    result = to_multipolygon(bowtie)
    assert isinstance(result, shapely.geometry.MultiPolygon)
    assert result.is_valid


def test_to_multipolygon_rejects_lines():
    line = shapely.geometry.LineString([(0, 0), (1, 1)])
    with pytest.raises(ValueError):
        to_multipolygon(line)


# ---------- find_shapefile ----------


def _touch_shp(dirpath: Path, name: str, size_bytes: int = 100) -> Path:
    """Create a stub .shp file (and required siblings) of the given size."""
    (dirpath / f"{name}.shp").write_bytes(b"x" * size_bytes)
    (dirpath / f"{name}.shx").write_bytes(b"")
    (dirpath / f"{name}.dbf").write_bytes(b"")
    return dirpath / f"{name}.shp"


def test_find_shapefile_matches_token(tmp_path: Path):
    _touch_shp(tmp_path, "20260501_ST_OB")
    _touch_shp(tmp_path, "20260501_ST_KR")
    result = find_shapefile(tmp_path, "obec")
    assert result.name == "20260501_ST_OB.shp"


def test_find_shapefile_prefers_largest(tmp_path: Path):
    _touch_shp(tmp_path, "ST_OB_meta", size_bytes=10)
    _touch_shp(tmp_path, "ST_OB_data", size_bytes=1000)
    result = find_shapefile(tmp_path, "obec")
    assert result.name == "ST_OB_data.shp"


def test_find_shapefile_recursive(tmp_path: Path):
    sub = tmp_path / "nested" / "deep"
    sub.mkdir(parents=True)
    _touch_shp(sub, "ST_KU")
    result = find_shapefile(tmp_path, "ku")
    assert result.parent == sub


def test_find_shapefile_raises_when_missing(tmp_path: Path):
    _touch_shp(tmp_path, "irrelevant")
    with pytest.raises(FileNotFoundError):
        find_shapefile(tmp_path, "kraj")


# ---------- iter_boundary_rows (synthetic fixture) ----------


# Two squares in EPSG:5514 (S-JTSK East/North, false-easting 5,000,000).
# Roughly Czech Republic territory. Real coordinate grid: X ~430..460k east of
# false easting, Y ~1,030..1,170k. We pick safe interior values.
PRAGUE_5514 = (-743000, -1043000)
BRNO_5514 = (-598000, -1160000)


def _square(center: tuple[float, float], half: float = 5000.0) -> list[tuple[float, float]]:
    """Clockwise square (shapefile-convention exterior ring)."""
    cx, cy = center
    return [
        (cx - half, cy - half),
        (cx - half, cy + half),
        (cx + half, cy + half),
        (cx + half, cy - half),
        (cx - half, cy - half),
    ]


@pytest.fixture
def transformer() -> Transformer:
    return Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)


@pytest.fixture
def fake_obec_shp(tmp_path: Path) -> Path:
    """Build a tiny ST_OB-style shapefile with two records.

    Encoding is cp1250 to match real RÚIAN exports (and our reader).
    """
    shp = tmp_path / "ST_OB.shp"
    w = pyshp.Writer(str(shp.with_suffix("")), shapeType=pyshp.POLYGON, encoding="cp1250")
    w.field("KOD", "N", 10, 0)
    w.field("NAZEV", "C", 50)
    w.field("KOD_OK_", "N", 10, 0)
    w.poly([_square(PRAGUE_5514)])
    w.record(554782, "Praha", 3100)
    w.poly([_square(BRNO_5514)])
    w.record(582786, "Brno", 3702)
    w.close()
    return shp


def test_iter_boundary_rows_extracts_records(fake_obec_shp: Path, transformer):
    rows = list(iter_boundary_rows(fake_obec_shp, "obec", transformer))
    assert len(rows) == 2
    by_id = {r.id: r for r in rows}
    assert 554782 in by_id and 582786 in by_id
    assert by_id[554782].name == "Praha"
    assert by_id[582786].name == "Brno"


def test_iter_boundary_rows_sets_parent(fake_obec_shp: Path, transformer):
    rows = list(iter_boundary_rows(fake_obec_shp, "obec", transformer))
    by_id = {r.id: r for r in rows}
    assert by_id[554782].parent_id == 3100
    assert by_id[582786].parent_id == 3702


def test_iter_boundary_rows_reprojects_to_4326(fake_obec_shp: Path, transformer):
    rows = list(iter_boundary_rows(fake_obec_shp, "obec", transformer))
    geom = shapely.wkt.loads(rows[0].geom_wkt)
    minx, miny, maxx, maxy = geom.bounds
    # Czech Republic is between roughly 12-19°E and 48-51°N.
    assert 12 < minx < 19, f"X out of CZ range: {minx}"
    assert 12 < maxx < 19
    assert 48 < miny < 51, f"Y out of CZ range: {miny}"
    assert 48 < maxy < 51


def test_iter_boundary_rows_emits_multipolygon_wkt(fake_obec_shp: Path, transformer):
    rows = list(iter_boundary_rows(fake_obec_shp, "obec", transformer))
    assert all(row.geom_wkt.startswith("MULTIPOLYGON") for row in rows)


def test_iter_boundary_rows_kraj_has_no_parent(tmp_path: Path, transformer):
    """A kraj shapefile has no parent column; parent_id should be NULL.

    cp1250 encoding here mirrors the real RÚIAN dbf encoding and exercises
    the diacritics path.
    """
    shp = tmp_path / "ST_KR.shp"
    w = pyshp.Writer(str(shp.with_suffix("")), shapeType=pyshp.POLYGON, encoding="cp1250")
    w.field("KOD", "N", 10, 0)
    w.field("NAZEV", "C", 50)
    w.poly([_square(PRAGUE_5514, half=20000)])
    w.record(19, "Hlavní město Praha")
    w.close()
    rows = list(iter_boundary_rows(shp, "kraj", transformer))
    assert len(rows) == 1
    assert rows[0].parent_id is None
    assert rows[0].id == 19
    assert rows[0].name == "Hlavní město Praha"


# ---------- BoundaryRow / level constants ----------


def test_levels_constant():
    assert LEVELS == ("kraj", "okres", "obec", "ku")


def test_simplify_tolerance_strictly_decreases():
    """Smaller units get tighter tolerance so they don't collapse."""
    tols = [SIMPLIFY_TOLERANCE_DEG[lvl] for lvl in LEVELS]
    assert tols == sorted(tols, reverse=True)


def test_boundary_row_is_frozen():
    row = BoundaryRow(id=1, level="kraj", name="X", parent_id=None, geom_wkt="MULTIPOLYGON EMPTY")
    with pytest.raises((AttributeError, Exception)):
        row.id = 2  # type: ignore[misc]
