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
the placeholder names the CSV loader wrote AND rebuilds the gazetteer afterwards — the
gazetteer skips placeholder-named units, so without the rebuild those levels would never be
searchable no matter which job ran last.

CLI:  python -m location_data.ruian_boundaries [--levels obec,okres] [--dry-run]
"""

from __future__ import annotations

import argparse
import contextlib
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
    render_tolerance_m: float
    expected_features: int | None = None


# `REGION_P` (NUTS 2, 8 areas) is NOT `VUSC_P` (NUTS 3 kraje, 14 areas) — an easy and
# consequential mix-up, so both counts are load-time assertions (C4.1).
#
# Tolerances are METRES and the render simplification runs in EPSG:5514 (metric), not in
# 4326 degrees: one degree of latitude is ~111.32 km but one degree of LONGITUDE at Czech
# latitudes is only ~71.7 km, so a single degree tolerance means two different ground
# distances per axis and `generalization_tolerance_m` could not honestly record either.
# The figures below are the ones the current production loader effectively uses.
LAYERS: tuple[Layer, ...] = (
    Layer("STATY_P", "stat", 111.0, 1),
    Layer("REGION_P", "region_soudrznosti", 111.0, 8),
    Layer("VUSC_P", "kraj", 111.0, 14),
    Layer("OKRESY_P", "okres", 83.0),
    Layer("ORP_P", "orp", 83.0),
    Layer("POU_P", "pou", 55.0),
    Layer("OBCE_P", "obec", 55.0),
    Layer("KATUZE_P", "katastralni_uzemi", 22.0),
    Layer("STU_P", "spravni_obvod", 55.0),
    Layer("PRARES_P", "spravni_obvod", 55.0),
    Layer("ZSJ_P", "zsj", 22.0),
)

DEFAULT_LAYERS = ("stat", "region_soudrznosti", "kraj", "okres", "orp", "pou", "obec",
                  "katastralni_uzemi", "spravni_obvod")

# A degenerate feature is skipped and counted, never fatal; the discrepancy rows are capped
# so one broken layer cannot write a million rows.
MAX_DISCREPANCY_ROWS = 1000

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


def read_layer(shp_dir: Path, layer: Layer) -> tuple[list[BoundaryFeature], list[int]]:
    """Read one shapefile, reprojecting 5514 -> 4326. Unsimplified: this is the authority.

    Returns the usable features plus the codes of degenerate ones (empty geometry, or a
    non-areal shape): those are counted into `registry_load_discrepancies` by the caller,
    never dropped in silence and never fatal — one broken feature in 6,258 obce must not
    cost the whole pack.
    """
    candidates = sorted(
        (p for p in shp_dir.rglob("*.shp") if layer.token.lower() in p.name.lower()),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not candidates:
        return [], []
    transformer = _transformer()
    out: list[BoundaryFeature] = []
    degenerate: list[int] = []
    with contextlib.closing(shapefile.Reader(str(candidates[0]))) as reader:
        fields = [f[0] for f in reader.fields[1:]]
        code_field = _pick_field(fields, _CODE_FIELDS)
        name_field = _pick_field(fields, _NAME_FIELDS)
        if code_field is None:
            raise BoundarySchemaError(f"{candidates[0].name}: no code field in {fields}")
        for record in reader.iterShapeRecords():
            attrs = record.record.as_dict()
            try:
                code = int(attrs[code_field])
            except (TypeError, ValueError):
                degenerate.append(0)
                continue
            try:
                geom = shapely.ops.transform(
                    transformer.transform, shapely.geometry.shape(record.shape)
                )
                multi = _to_multipolygon(geom)
            except Exception:  # noqa: BLE001 — a malformed shape is data, not a crash
                multi = None
            if multi is None:
                degenerate.append(code)
                continue
            out.append(BoundaryFeature(
                level=layer.level,
                code=code,
                name=str(attrs.get(name_field) or code).strip(),
                wkb=multi.wkb,
            ))
    return out, degenerate


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
    # A NULL probe is an ABSENT constraint, not a passing one: it means the migration this
    # loader depends on has not been applied (or the table does not exist), and loading on
    # into an unconstrained table is exactly the silent degradation this check exists to stop.
    if check is None or "pip" not in str(check):
        raise BoundarySchemaError(
            "ruian_admin_unit_geometries.purpose CHECK is missing or does not admit 'pip' "
            f"(probe returned {check!r}) — 04 C4.3 requires the CHECK to be "
            "('authoritative','pip','render'). Apply migration 381, or re-run with "
            "--allow-missing-pip to load authoritative+render only."
        )
    unique = loader_db.scalar(
        conn,
        """
        SELECT indexdef FROM pg_indexes
         WHERE tablename = 'ruian_admin_unit_geometries'
           AND indexdef ILIKE '%unique%' AND indexdef ILIKE '%purpose%'
        """,
    )
    definition = str(unique or "")
    if unique is None or "WHERE" not in definition.upper() or "pip" not in definition:
        raise BoundarySchemaError(
            "ruian_admin_unit_geometries needs a PARTIAL unique index on "
            "(unit_id, registry_version_id, purpose) excluding purpose='pip' — 04 C4.3's "
            "'pip' purpose is one row per ST_Subdivide piece, so a total unique index (or "
            f"none at all) cannot hold the pack. Probe returned {unique!r}. Re-run with "
            "--allow-missing-pip to skip pip rows."
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

# Simplification runs in EPSG:5514 metres so `generalization_tolerance_m` is the tolerance
# actually applied, not a degree figure converted with one axis's scale factor. The 5514
# round-trip is confined to the RENDER row — a ~1 m PROJ-path difference is immaterial on a
# 22-111 m simplification and never reaches the containment authority, which is written from
# the Python-side pyproj transform.
_INSERT_RENDER = """
INSERT INTO ruian_admin_unit_geometries
       (unit_id, registry_version_id, purpose, generalization_tolerance_m,
        simplify_algorithm, geom, area_m2, representative_point, inscribed_radius_m,
        centroid_point, containment_radius_m, max_radius_m)
SELECT a.unit_id, a.registry_version_id, 'render', %(tolerance_m)s,
       'ST_SimplifyPreserveTopology',
       ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Transform(
           ST_SimplifyPreserveTopology(ST_Transform(a.geom, 5514), %(tolerance_m)s),
           4326)), 3)),
       a.area_m2, a.representative_point, a.inscribed_radius_m, a.centroid_point,
       a.containment_radius_m, a.max_radius_m
  FROM ruian_admin_unit_geometries a
 WHERE a.unit_id = %(unit_id)s AND a.registry_version_id = %(version_id)s
   AND a.purpose = 'authoritative'
"""

# Every diagnostic on a `pip` row is PIECE-LOCAL. Copying the unit-wide area, representative
# point and radii onto each subdivided piece would put a point that is not in the piece into
# the piece's own representative_point and hand any consumer reading a pip row an
# uncertainty radius describing the whole obec — the exact failure mode
# `inscribed_radius_m must never feed uncertainty_radius_m` warns about (01 §3.3.1).
# The columns are NOT NULL in 01 §3.3, so they are computed, not nulled; a piece has
# <= 256 vertices, which makes ST_MaximumInscribedCircle cheap here.
_INSERT_PIP = """
INSERT INTO ruian_admin_unit_geometries
       (unit_id, registry_version_id, purpose, generalization_tolerance_m,
        simplify_algorithm, geom, area_m2, representative_point, inscribed_radius_m,
        centroid_point, containment_radius_m, max_radius_m)
SELECT a.unit_id, a.registry_version_id, 'pip', 0, 'none',
       ST_Multi(piece),
       ST_Area(piece::geography),
       ST_Transform(c.center, 4326), c.radius,
       ST_Centroid(piece),
       ST_MaxDistance(c.center, ST_Boundary(m.piece5514)),
       ST_MaxDistance(m.piece5514, m.piece5514) / 2
  FROM ruian_admin_unit_geometries a
 CROSS JOIN LATERAL ST_Subdivide(a.geom, %(max_vertices)s) AS piece
 CROSS JOIN LATERAL (SELECT ST_Transform(piece, 5514) AS piece5514) m
 CROSS JOIN LATERAL (SELECT (ST_MaximumInscribedCircle(m.piece5514)).*) c
 WHERE a.unit_id = %(unit_id)s AND a.registry_version_id = %(version_id)s
   AND a.purpose = 'authoritative'
"""

_MARK_HAS_POLYGON = """
UPDATE ruian_admin_units SET has_polygon = true
 WHERE id = %(unit_id)s AND NOT has_polygon
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


def upgrade_name(conn: psycopg.Connection, unit_id: int, name: str) -> bool:
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
        return bool(cur.rowcount)


def load_feature(
    conn: psycopg.Connection,
    feature: BoundaryFeature,
    layer: Layer,
    version_id: int,
    *,
    with_pip: bool,
) -> tuple[bool, bool]:
    """(loaded, name_upgraded) for one feature; (False, False) when no unit matches."""
    unit_id = unit_id_for(conn, feature.level, feature.code)
    if unit_id is None:
        return False, False
    with conn.transaction():
        upgraded = upgrade_name(conn, unit_id, feature.name)
        with conn.cursor() as cur:
            cur.execute(_DELETE_UNIT, {"unit_id": unit_id, "version_id": version_id})
            cur.execute(_INSERT_AUTHORITATIVE,
                        {"wkb": feature.wkb, "unit_id": unit_id, "version_id": version_id})
            cur.execute(_INSERT_RENDER, {
                "unit_id": unit_id, "version_id": version_id,
                "tolerance_m": layer.render_tolerance_m,
            })
            if with_pip:
                cur.execute(_INSERT_PIP, {
                    "unit_id": unit_id, "version_id": version_id,
                    "max_vertices": SUBDIVIDE_MAX_VERTICES,
                })
            cur.execute(_MARK_HAS_POLYGON, {"unit_id": unit_id})
    return True, upgraded


def _fetch_pack(work_dir: Path, *, reuse: bool) -> Path:
    dest = work_dir / "ruian_shp_stat.zip"
    if reuse and dest.exists():
        LOG.info("BOUNDARY reusing cached %s", dest)
    else:
        artifact = ruian_csv.download(ruian_csv.session(), "shp_stat", STATE_PACK_URL, dest)
        LOG.info("BOUNDARY downloaded bytes=%d sha256=%s", artifact.bytes, artifact.sha256[:16])
    extracted = work_dir / "shp"
    extracted.mkdir(exist_ok=True)
    with zipfile.ZipFile(dest) as zf:
        zf.extractall(extracted)
    return extracted


def _record_degenerate(
    conn: psycopg.Connection, version_id: int, layer: Layer, codes: list[int],
) -> None:
    for code in codes[:MAX_DISCREPANCY_ROWS]:
        loader_db.record_discrepancy(
            conn, version_id, entity_kind=layer.level, entity_code=code,
            discrepancy="degenerate_boundary_geometry", detail={"layer": layer.token},
        )


def load_layers(
    conn: psycopg.Connection,
    extracted: Path,
    *,
    levels: tuple[str, ...],
    version_id: int,
    with_pip: bool,
) -> dict[str, int]:
    counts = {"loaded": 0, "skipped_no_unit": 0, "degenerate": 0, "failed": 0, "names": 0}
    for layer in LAYERS:
        if layer.level not in levels:
            continue
        features, degenerate = read_layer(extracted, layer)
        assert_feature_counts(layer, features)
        LOG.info("BOUNDARY layer=%s level=%s features=%d degenerate=%d", layer.token,
                 layer.level, len(features), len(degenerate))
        if degenerate:
            counts["degenerate"] += len(degenerate)
            _record_degenerate(conn, version_id, layer, degenerate)
        for feature in features:
            try:
                loaded, upgraded = load_feature(
                    conn, feature, layer, version_id, with_pip=with_pip
                )
            except psycopg.Error as exc:
                # One unloadable geometry is a discrepancy row, not the end of a 253 MB pack.
                counts["failed"] += 1
                LOG.warning("BOUNDARY level=%s code=%s failed: %s",
                            feature.level, feature.code, exc)
                loader_db.record_discrepancy(
                    conn, version_id, entity_kind=feature.level, entity_code=feature.code,
                    discrepancy="boundary_load_failed",
                    detail={"layer": layer.token, "error": str(exc)[:500]},
                )
                continue
            counts["loaded" if loaded else "skipped_no_unit"] += 1
            counts["names"] += int(upgraded)
    return counts


def run(
    *,
    levels: tuple[str, ...],
    work_dir: Path,
    dry_run: bool,
    reuse: bool,
    allow_missing_pip: bool,
    skip_gazetteer: bool = False,
) -> int:
    with_pip = not allow_missing_pip
    if dry_run:
        extracted = _fetch_pack(work_dir, reuse=reuse)
        for layer in LAYERS:
            if layer.level not in levels:
                continue
            features, degenerate = read_layer(extracted, layer)
            assert_feature_counts(layer, features)
            LOG.info("BOUNDARY layer=%s level=%s features=%d degenerate=%d", layer.token,
                     layer.level, len(features), len(degenerate))
        return 0

    # `with` on the connection: an assertion failure, a missing registry_version or a
    # mid-pack exception must not leak a session-mode backend holding statement_timeout=0.
    with loader_db.open_loader_connection() as conn:
        if with_pip:
            check_pip_supported(conn)
        version_id = loader_db.scalar(
            conn, "SELECT id FROM registry_versions WHERE is_current LIMIT 1"
        )
        if version_id is None:
            LOG.error("BOUNDARY no current registry_version — run the baseline load first")
            return 1
        extracted = _fetch_pack(work_dir, reuse=reuse)
        counts = load_layers(conn, extracted, levels=levels, version_id=int(version_id),
                             with_pip=with_pip)
        with conn.cursor() as cur:
            cur.execute("ANALYZE ruian_admin_unit_geometries")
        # The pack is the ONLY name source for kraj / okres / ORP / POU / KÚ / ZSJ, and the
        # gazetteer skips placeholder-named units — so without this rebuild those levels are
        # never searchable, whatever order the two jobs run in.
        if counts["names"] and not skip_gazetteer:
            rebuilt = name_index.rebuild(conn, int(version_id))
            LOG.info("BOUNDARY gazetteer rebuilt rows=%d after %d name upgrades",
                     rebuilt, counts["names"])
    LOG.info("BOUNDARY done %s pip=%s", counts, with_pip)
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
    parser.add_argument("--skip-gazetteer", action="store_true",
                        help="do not rebuild ruian_name_index after upgrading names "
                             "(the upgraded levels stay unsearchable until it is run)")
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
                skip_gazetteer=args.skip_gazetteer,
            )
        except BoundarySchemaError as exc:
            LOG.error("BOUNDARY %s", exc)
            return 1
        except loader_db.LoadAborted as exc:
            LOG.error("BOUNDARY %s", exc)
            return 1


if __name__ == "__main__":
    sys.exit(main())
