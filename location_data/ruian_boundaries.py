"""Load the ČÚZK boundary SHP pack into `ruian_admin_unit_geometries` (04 C4).

Product: `https://services.cuzk.gov.cz/shp/stat/epsg-5514/1.zip` — the STATE pack, 253 MB,
13 layers, EPSG:5514, refreshed daily-to-weekly (verified live: last-modified
Fri 07 Aug 2026 18:31:50 GMT, 253,094,090 bytes). The per-obec pack and the ATOM
`updated`-diff incremental path (C4.2) belong to the weekly `boundary_delta` job; this
module is the monthly `boundary_baseline` reconcile.

THREE geometries per boundary (C4.3), never one:

    authoritative  as published, 5514 -> 4326, NO simplification — the containment
                   authority and the sliver test's only legitimate input
    pip            ST_Subdivide(authoritative, 256) — small pieces, GiST-prunable,
                   lossless for containment; candidate selection runs against these
    render         ST_SimplifyPreserveTopology, tolerance RECORDED in the row, map only

The current production loader stores ONLY simplified polygons (obec ≈55 m) and then uses
them as the point-in-polygon authority, which is what migration 289's hardcoded 250 m
sliver fallback exists to paper over. Do not repeat that: `render` never decides
containment.

The pack is also the name source for the levels the address CSVs carry no name for
(stát, region soudržnosti, kraj, okres, ORP, POU, katastrální území), so this job upgrades
the placeholder names the CSV loader wrote.

CLI:  python -m location_data.ruian_boundaries [--layers obec,okres] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pyproj
import shapefile  # type: ignore[import-untyped]
import shapely.geometry
import shapely.ops
from shapely.geometry.base import BaseGeometry

from location_data import krovak, loader_db, name_index, ruian_csv

LOG = logging.getLogger("location_data.ruian_boundaries")

STATE_PACK_URL = "https://services.cuzk.gov.cz/shp/stat/epsg-5514/1.zip"
SUBDIVIDE_MAX_VERTICES = 256


@dataclass(frozen=True, slots=True)
class Layer:
    token: str
    level: str
    render_tolerance_deg: float
    expected_features: int | None = None


# `REGION_P` (NUTS 2, 8 areas) is NOT `VUSC_P` (NUTS 3 kraje, 14 areas) — an easy and
# consequential mix-up, so both counts are load-time assertions (C4.1).
LAYERS: tuple[Layer, ...] = (
    Layer("STATY_P", "stat", 0.001, 1),
    Layer("REGION_P", "region_soudrznosti", 0.001, 8),
    Layer("VUSC_P", "kraj", 0.001, 14),
    Layer("OKRESY_P", "okres", 0.00075),
    Layer("ORP_P", "orp", 0.00075),
    Layer("POU_P", "pou", 0.0005),
    Layer("OBCE_P", "obec", 0.0005),
    Layer("KATUZE_P", "katastralni_uzemi", 0.0002),
    Layer("STU_P", "spravni_obvod", 0.0005),
    Layer("PRARES_P", "spravni_obvod", 0.0005),
    Layer("ZSJ_P", "zsj", 0.0002),
)

DEFAULT_LAYERS = ("stat", "region_soudrznosti", "kraj", "okres", "orp", "pou", "obec",
                  "katastralni_uzemi", "spravni_obvod")

# Degrees -> metres for the recorded tolerance. One degree of latitude is ~111.32 km; the
# column is `generalization_tolerance_m` and simplification happens in 4326 degrees, so the
# conversion is stated once here rather than fudged per level.
DEG_TO_M = 111_320.0

_CODE_FIELDS = ("KOD", "Kod", "KOD_KU_", "KOD_OB_", "KOD_OK_", "KOD_KR_", "ID")
_NAME_FIELDS = ("NAZEV", "Nazev", "NAZ_KU", "NAZ_OB", "NAZ_OK", "NAZ_KR", "NAME")


class BoundarySchemaError(RuntimeError):
    """The mirror cannot store the three-geometry model this design requires."""


@dataclass(frozen=True, slots=True)
class BoundaryFeature:
    level: str
    code: int
    name: str
    wkb: bytes


def _pick_field(fields: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {f.lower(): f for f in fields}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def _transformer() -> pyproj.Transformer:
    return krovak.wgs84_transformer()


def _to_multipolygon(geom: BaseGeometry) -> shapely.geometry.MultiPolygon | None:
    if geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return shapely.geometry.MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    return None


def read_layer(shp_dir: Path, layer: Layer) -> list[BoundaryFeature]:
    """Read one shapefile, reprojecting 5514 -> 4326. Unsimplified: this is the authority."""
    candidates = sorted(
        (p for p in shp_dir.rglob("*.shp") if layer.token.lower() in p.name.lower()),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not candidates:
        return []
    reader = shapefile.Reader(str(candidates[0]))
    fields = [f[0] for f in reader.fields[1:]]
    code_field = _pick_field(fields, _CODE_FIELDS)
    name_field = _pick_field(fields, _NAME_FIELDS)
    if code_field is None:
        raise BoundarySchemaError(f"{candidates[0].name}: no code field in {fields}")
    transformer = _transformer()
    out: list[BoundaryFeature] = []
    for record in reader.iterShapeRecords():
        attrs = record.record.as_dict()
        try:
            code = int(attrs[code_field])
        except (TypeError, ValueError):
            continue
        geom = shapely.ops.transform(transformer.transform, shapely.geometry.shape(record.shape))
        multi = _to_multipolygon(geom)
        if multi is None:
            continue
        out.append(BoundaryFeature(
            level=layer.level,
            code=code,
            name=str(attrs.get(name_field) or code).strip(),
            wkb=multi.wkb,
        ))
    reader.close()
    return out


def assert_feature_counts(layer: Layer, features: list[BoundaryFeature]) -> None:
    if layer.expected_features is not None and len(features) != layer.expected_features:
        raise BoundarySchemaError(
            f"{layer.token}: expected {layer.expected_features} features, got {len(features)} "
            "— REGION_P (NUTS 2, 8) and VUSC_P (NUTS 3, 14) are different layers"
        )


def check_pip_supported(conn: psycopg.Connection) -> None:
    """C4.3 needs MANY `pip` rows per unit (one per ST_Subdivide piece) and a CHECK that
    admits 'pip'. 01 §3.3 currently ships neither. Fail before downloading 253 MB rather
    than silently degrading the containment authority to a simplified polygon."""
    # Single '%' on purpose: these run WITHOUT bind parameters, so psycopg does no
    # placeholder processing and a doubled '%%' would reach Postgres literally.
    check = loader_db.scalar(
        conn,
        """
        SELECT pg_get_constraintdef(c.oid)
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE t.relname = 'ruian_admin_unit_geometries' AND c.contype = 'c'
           AND pg_get_constraintdef(c.oid) ILIKE '%purpose%'
        """,
    )
    if check is not None and "pip" not in str(check):
        raise BoundarySchemaError(
            "ruian_admin_unit_geometries.purpose CHECK does not admit 'pip' — 04 C4.3 "
            "requires the CHECK widened to ('authoritative','pip','render'). "
            "Re-run with --allow-missing-pip to load authoritative+render only."
        )
    unique = loader_db.scalar(
        conn,
        """
        SELECT indexdef FROM pg_indexes
         WHERE tablename = 'ruian_admin_unit_geometries'
           AND indexdef ILIKE '%unique%' AND indexdef ILIKE '%purpose%'
        """,
    )
    if unique is not None and "WHERE" not in str(unique).upper():
        raise BoundarySchemaError(
            "UNIQUE (unit_id, registry_version_id, purpose) on "
            "ruian_admin_unit_geometries admits only ONE row per purpose, but 04 C4.3's "
            "'pip' purpose is one row per ST_Subdivide piece. The unique index must "
            "exclude purpose='pip'. Re-run with --allow-missing-pip to skip pip rows."
        )


# No ON CONFLICT anywhere: `pip` is many rows per unit, so 01 §3.3's
# UNIQUE (unit_id, registry_version_id, purpose) has to become partial, and a partial
# unique index is not usable as a bare conflict target. Delete-then-insert per unit is
# idempotent under either shape.
_DELETE_UNIT = """
DELETE FROM ruian_admin_unit_geometries
 WHERE unit_id = %(unit_id)s AND registry_version_id = %(version_id)s
"""

_INSERT_AUTHORITATIVE = """
WITH g AS (
  SELECT ST_Multi(ST_CollectionExtract(
           ST_MakeValid(ST_GeomFromWKB(%(wkb)s, 4326)), 3)) AS geom
), m AS (
  SELECT geom, ST_Transform(geom, 5514) AS geom5514 FROM g
), c AS (
  SELECT geom, geom5514, (ST_MaximumInscribedCircle(geom5514)).* FROM m
)
INSERT INTO ruian_admin_unit_geometries
       (unit_id, registry_version_id, purpose, generalization_tolerance_m,
        simplify_algorithm, geom, area_m2, representative_point, inscribed_radius_m,
        centroid_point, containment_radius_m, max_radius_m)
SELECT %(unit_id)s, %(version_id)s, 'authoritative', 0, 'none', c.geom,
       ST_Area(c.geom::geography),
       ST_Transform(c.center, 4326), c.radius,
       ST_Centroid(c.geom),
       ST_MaxDistance(c.center, ST_Boundary(c.geom5514)),
       ST_MaxDistance(c.geom5514, c.geom5514) / 2
  FROM c
"""

_INSERT_RENDER = """
INSERT INTO ruian_admin_unit_geometries
       (unit_id, registry_version_id, purpose, generalization_tolerance_m,
        simplify_algorithm, geom, area_m2, representative_point, inscribed_radius_m,
        centroid_point, containment_radius_m, max_radius_m)
SELECT a.unit_id, a.registry_version_id, 'render', %(tolerance_m)s,
       'ST_SimplifyPreserveTopology',
       ST_Multi(ST_CollectionExtract(
           ST_MakeValid(ST_SimplifyPreserveTopology(a.geom, %(tolerance_deg)s)), 3)),
       a.area_m2, a.representative_point, a.inscribed_radius_m, a.centroid_point,
       a.containment_radius_m, a.max_radius_m
  FROM ruian_admin_unit_geometries a
 WHERE a.unit_id = %(unit_id)s AND a.registry_version_id = %(version_id)s
   AND a.purpose = 'authoritative'
"""

_INSERT_PIP = """
INSERT INTO ruian_admin_unit_geometries
       (unit_id, registry_version_id, purpose, generalization_tolerance_m,
        simplify_algorithm, geom, area_m2, representative_point, inscribed_radius_m,
        centroid_point, containment_radius_m, max_radius_m)
SELECT a.unit_id, a.registry_version_id, 'pip', 0, 'none',
       ST_Multi(piece), a.area_m2, a.representative_point, a.inscribed_radius_m,
       a.centroid_point, a.containment_radius_m, a.max_radius_m
  FROM ruian_admin_unit_geometries a,
       LATERAL ST_Subdivide(a.geom, %(max_vertices)s) AS piece
 WHERE a.unit_id = %(unit_id)s AND a.registry_version_id = %(version_id)s
   AND a.purpose = 'authoritative'
"""


def unit_id_for(conn: psycopg.Connection, level: str, code: int) -> int | None:
    return loader_db.scalar(
        conn,
        """
        SELECT id FROM ruian_admin_units
         WHERE level::text = %s AND code = %s AND valid_to IS NULL
         LIMIT 1
        """,
        (level, code),
    )


def upgrade_name(conn: psycopg.Connection, unit_id: int, name: str) -> None:
    """Replace a code placeholder written by the CSV loader with the pack's real name.

    Only ever touches a unit still named after its own code, so a real RÚIAN name can
    never be overwritten by a DBF variant. Descendants keep their `display_path` until the
    next baseline recomputes it — the code path (`path`) is unaffected either way.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ruian_admin_units u
               SET name = %s, name_norm = %s,
                   display_path = coalesce(
                       (SELECT p.display_path || ' / ' FROM ruian_admin_units p
                         WHERE p.id = u.parent_id), '') || %s
             WHERE u.id = %s AND u.name = u.code::text
            """,
            (name, name_index.normalize_name(name), name, unit_id),
        )


def load_feature(
    conn: psycopg.Connection,
    feature: BoundaryFeature,
    layer: Layer,
    version_id: int,
    *,
    with_pip: bool,
) -> bool:
    unit_id = unit_id_for(conn, feature.level, feature.code)
    if unit_id is None:
        return False
    with conn.transaction(), conn.cursor() as cur:
        upgrade_name(conn, unit_id, feature.name)
        cur.execute(_DELETE_UNIT, {"unit_id": unit_id, "version_id": version_id})
        cur.execute(_INSERT_AUTHORITATIVE,
                    {"wkb": feature.wkb, "unit_id": unit_id, "version_id": version_id})
        cur.execute(_INSERT_RENDER, {
            "unit_id": unit_id, "version_id": version_id,
            "tolerance_deg": layer.render_tolerance_deg,
            "tolerance_m": round(layer.render_tolerance_deg * DEG_TO_M, 3),
        })
        if with_pip:
            cur.execute(_INSERT_PIP, {
                "unit_id": unit_id, "version_id": version_id,
                "max_vertices": SUBDIVIDE_MAX_VERTICES,
            })
    return True


def run(
    *,
    levels: tuple[str, ...],
    work_dir: Path,
    dry_run: bool,
    reuse: bool,
    allow_missing_pip: bool,
) -> int:
    sess = ruian_csv.session()
    dest = work_dir / "ruian_shp_stat.zip"
    conn = None
    version_id = None
    with_pip = not allow_missing_pip
    if not dry_run:
        conn = loader_db.open_loader_connection()
        if with_pip:
            check_pip_supported(conn)
        version_id = loader_db.scalar(
            conn, "SELECT id FROM registry_versions WHERE is_current LIMIT 1"
        )
        if version_id is None:
            LOG.error("BOUNDARY no current registry_version — run the baseline load first")
            return 1

    if reuse and dest.exists():
        LOG.info("BOUNDARY reusing cached %s", dest)
    else:
        artifact = ruian_csv.download(sess, "shp_stat", STATE_PACK_URL, dest)
        LOG.info("BOUNDARY downloaded bytes=%d sha256=%s", artifact.bytes, artifact.sha256[:16])

    extracted = work_dir / "shp"
    extracted.mkdir(exist_ok=True)
    with zipfile.ZipFile(dest) as zf:
        zf.extractall(extracted)

    loaded = skipped = 0
    for layer in LAYERS:
        if layer.level not in levels:
            continue
        features = read_layer(extracted, layer)
        assert_feature_counts(layer, features)
        LOG.info("BOUNDARY layer=%s level=%s features=%d", layer.token, layer.level,
                 len(features))
        if dry_run or conn is None or version_id is None:
            continue
        for feature in features:
            if load_feature(conn, feature, layer, int(version_id), with_pip=with_pip):
                loaded += 1
            else:
                skipped += 1
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute("ANALYZE ruian_admin_unit_geometries")
        conn.close()
    LOG.info("BOUNDARY done loaded=%d skipped_no_unit=%d pip=%s", loaded, skipped, with_pip)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", default=",".join(DEFAULT_LAYERS),
                        help="comma-separated ruian_level values to load")
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--reuse-downloads", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="download + read + assert feature counts, write nothing")
    parser.add_argument("--allow-missing-pip", action="store_true",
                        help="load authoritative+render only (PIP falls back to the "
                             "authoritative geometry; state why in the PR)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    levels = tuple(x.strip() for x in args.levels.split(",") if x.strip())
    with tempfile.TemporaryDirectory(prefix="ruian-shp-") as tmp:
        work_dir = Path(args.work_dir) if args.work_dir else Path(tmp)
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            return run(
                levels=levels, work_dir=work_dir, dry_run=args.dry_run,
                reuse=args.reuse_downloads, allow_missing_pip=args.allow_missing_pip,
            )
        except BoundarySchemaError as exc:
            LOG.error("BOUNDARY %s", exc)
            return 1


if __name__ == "__main__":
    sys.exit(main())
