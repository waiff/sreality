"""The registry's publish gate and carry-forward, against a real schema.

The offline suite pins the SQL's shape and drives `ruian_load.run` over faked phases. It
cannot answer what Postgres DOES with that SQL: whether an unchanged boundary really
compares equal to the row it was stored as (and so is carried, every pip piece of it and
nothing of its neighbours), whether a moved one is derived afresh, and which member units
the completeness read reports. Those are PostGIS and anti-join questions, so they run here.

Gated on TEST_DATABASE_URL, like the PREPARE sweep: runs in the schema-replay job
(.github/workflows/migrations.yml), skips in the normal offline suite. Everything rolls back.
"""

from __future__ import annotations

import datetime
import os

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — the registry publish live checks run only in CI's DB job",
)

shapely_geometry = pytest.importorskip("shapely.geometry")

from location_data import loader_db  # noqa: E402
from location_data import ruian_boundaries as rb  # noqa: E402

V1_DATE = datetime.date(2026, 7, 31)
V2_DATE = datetime.date(2026, 8, 31)

# Every value of a stored geometry row but its id and version, in a comparable form.
_ROWS = """
SELECT purpose, encode(ST_AsEWKB(geom), 'hex'), generalization_tolerance_m,
       simplify_algorithm, area_m2, encode(ST_AsEWKB(representative_point), 'hex'),
       inscribed_radius_m, encode(ST_AsEWKB(centroid_point), 'hex'), containment_radius_m,
       max_radius_m
  FROM ruian_admin_unit_geometries
 WHERE unit_id = %s AND registry_version_id = %s
 ORDER BY 1, 2
"""


@pytest.fixture
def conn():
    import psycopg

    with psycopg.connect(_DB_URL) as c:
        try:
            yield c
        finally:
            c.rollback()


def _version(conn, label: str, source_date: datetime.date) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO registry_versions (label, kind, source_date, artifact_urls, "
            "proj_version, proj_pipeline) VALUES (%s, 'baseline', %s, '{}', 'p', 'q') "
            "RETURNING id",
            (f"test:registry-publish:{label}", source_date),
        )
        return int(cur.fetchone()[0])


def _unit(conn, level: str, code: int, first: int, last: int,
          valid_to: datetime.date | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ruian_admin_units (level, code, name, name_norm, path, display_path, "
            "valid_from, valid_to, first_version_id, last_version_id) "
            "VALUES (%s::ruian_level, %s, %s, %s, %s::ltree, %s, %s, %s, %s, %s) RETURNING id",
            (level, code, str(code), str(code), f"t{code}", str(code), V1_DATE, valid_to,
             first, last),
        )
        return int(cur.fetchone()[0])


def _feature(level: str, code: int, lon: float, lat: float) -> rb.BoundaryFeature:
    """A disc of ~800 vertices, so ST_Subdivide(256) cuts it into several pip pieces."""
    disc = shapely_geometry.Point(lon, lat).buffer(0.01, quad_segs=200)
    return rb.BoundaryFeature(level=level, code=code, name=f"Unit {code}",
                              wkb=shapely_geometry.MultiPolygon([disc]).wkb)


def _rows(conn, unit_id: int, version_id: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(_ROWS, (unit_id, version_id))
        return cur.fetchall()


def _layer(level: str) -> rb.Layer:
    return next(layer for layer in rb.LAYERS if layer.level == level)


def test_an_unchanged_unit_is_carried_row_for_row_and_a_moved_one_is_derived(conn):
    v1 = _version(conn, "carry-v1", V1_DATE)
    v2 = _version(conn, "carry-v2", V2_DATE)
    same = _unit(conn, "obec", 9_900_001, v1, v2)
    moved = _unit(conn, "obec", 9_900_002, v1, v2)
    obce = _layer("obec")

    # Neighbours whose bounding boxes overlap, so the carry's bbox join sees the other
    # unit's pip pieces and must leave them behind.
    assert rb.load_feature(conn, _feature("obec", 9_900_001, 14.400, 50.08), obce, v1)[0] \
        == "derived"
    assert rb.load_feature(conn, _feature("obec", 9_900_002, 14.415, 50.08), obce, v1)[0] \
        == "derived"

    assert rb.load_feature(conn, _feature("obec", 9_900_001, 14.400, 50.08), obce, v2)[0] \
        == "carried"
    assert rb.load_feature(conn, _feature("obec", 9_900_002, 14.4151, 50.08), obce, v2)[0] \
        == "derived"

    carried, source = _rows(conn, same, v2), _rows(conn, same, v1)
    assert carried == source
    assert sum(1 for row in carried if row[0] == "pip") > 1
    assert {row[0] for row in carried} == {"authoritative", "pip", "render"}

    recomputed, before = _rows(conn, moved, v2), _rows(conn, moved, v1)
    assert {row[0] for row in recomputed} == {"authoritative", "pip", "render"}
    authoritative = lambda rows: next(r for r in rows if r[0] == "authoritative")  # noqa: E731
    assert authoritative(recomputed)[1] != authoritative(before)[1]


def test_completeness_reports_members_without_both_rows_and_excuses_only_degenerate(conn):
    v1 = _version(conn, "complete-v1", V1_DATE)
    v2 = _version(conn, "complete-v2", V2_DATE)
    obce, ku = _layer("obec"), _layer("katastralni_uzemi")

    _unit(conn, "obec", 9_900_011, v1, v2)                       # complete at v2
    rb.load_feature(conn, _feature("obec", 9_900_011, 14.40, 50.10), obce, v2)
    _unit(conn, "obec", 9_900_012, v1, v2)                       # degenerate: excused
    loader_db.record_discrepancy(conn, v2, entity_kind="obec", entity_code=9_900_012,
                                 discrepancy="degenerate_boundary_geometry")
    _unit(conn, "obec", 9_900_013, v1, v2)                       # failed: NOT excused
    loader_db.record_discrepancy(conn, v2, entity_kind="obec", entity_code=9_900_013,
                                 discrepancy="boundary_load_failed")
    _unit(conn, "obec", 9_900_014, v1, v2, valid_to=V2_DATE)     # closed BY v2: not a member
    _unit(conn, "obec", 9_900_015, v1, v1)                       # gone before v2: not a member
    _unit(conn, "katastralni_uzemi", 9_900_016, v1, v2)          # no geometry at all
    half = _unit(conn, "katastralni_uzemi", 9_900_017, v1, v2)   # authoritative, no pip
    rb.load_feature(conn, _feature("katastralni_uzemi", 9_900_017, 14.50, 50.10), ku, v2)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ruian_admin_unit_geometries WHERE unit_id = %s "
                    "AND registry_version_id = %s AND purpose = 'pip'", (half, v2))
    _unit(conn, "katastralni_uzemi", 9_900_018, v1, v2)          # complete at v1 only
    rb.load_feature(conn, _feature("katastralni_uzemi", 9_900_018, 14.60, 50.10), ku, v1)
    _unit(conn, "spravni_obvod", 9_900_019, v1, v2)              # not a publish level

    assert rb.missing_geometry(conn, v2) == [
        ("katastralni_uzemi", 3, [9_900_016, 9_900_017, 9_900_018]),
        ("obec", 1, [9_900_013]),
    ]
