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

The pack takes ~an hour of per-piece PostGIS work on one session-mode connection, which is
long enough for the session to be dropped under it (it was, 45 minutes into OBCE_P: an SSL
EOF at obec 576069). So the per-unit loop is RESUMABLE and RECONNECTING, both bounded:
a dropped session is reconnected and the unit retried once (`MAX_RECONNECTS` per run, past
which the environment — not the pack — is what is broken), and a unit whose geometries are
already committed for this registry version is skipped, so a re-dispatch always moves
forward. Failure-path bookkeeping opens its own connection: the original incident reported
"the connection is closed" from the discrepancy INSERT and lost the SSL drop that caused it.

CLI:  python -m location_data.ruian_boundaries [--levels obec,okres] [--dry-run]
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import psycopg
import pyproj
import shapefile  # type: ignore[import-untyped]
import shapely.geometry
import shapely.ops
from shapely.geometry.base import BaseGeometry

from location_data import krovak, loader_db, name_index, ruian_csv
from scraper import db

LOG = logging.getLogger("location_data.ruian_boundaries")

STATE_PACK_URL = "https://services.cuzk.gov.cz/shp/stat/epsg-5514/1.zip"
SUBDIVIDE_MAX_VERTICES = 256

_T = TypeVar("_T")


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


# A unit already carrying an `authoritative` row for this registry_version was written by
# `load_feature`'s per-unit transaction, which commits the name upgrade, the DELETE, all
# THREE geometries and the has_polygon flag together — so its presence is proof the whole
# unit landed and the unit can be skipped whole. Prefetched ONCE per layer (a per-unit
# EXISTS probe would add 6,258 round-trips to the obec layer), scoped to the layer's level
# so the set stays small and a code can never be mistaken for another level's. Driven from
# the units side so the two indexes migration 381 already ships do the work:
# `ruian_admin_units_code (level, code)` picks the layer, then the partial
# `ruian_aug_auth (unit_id) WHERE purpose = 'authoritative'` answers each unit.
_DONE_CODES = """
SELECT u.code
  FROM ruian_admin_units u
 WHERE u.level::text = %s AND u.valid_to IS NULL
   AND EXISTS (
       SELECT 1 FROM ruian_admin_unit_geometries g
        WHERE g.unit_id = u.id
          AND g.purpose = 'authoritative'
          AND g.registry_version_id = %s
   )
"""

# A session that dies mid-pack is an environment fault we ride out; a session that dies
# twenty times is an environment that is broken, and grinding through 6,258 obce one
# reconnect at a time would hide that behind a green-ish run.
MAX_RECONNECTS = 20


def done_codes(conn: psycopg.Connection, version_id: int, level: str) -> set[int]:
    """Codes of `level` whose geometries are already committed for this registry version."""
    with conn.cursor() as cur:
        cur.execute(_DONE_CODES, (level, version_id))
        return {int(row[0]) for row in cur.fetchall()}


class Reconnector:
    """Bounded reconnect budget for one run (scraper.db.run_resilient's shape, per-unit).

    `run_resilient` itself does not fit: it discards the connection it opened when the
    attempts are exhausted, and this loader must keep going on the FRESH connection after
    a unit finally fails, not be handed back a dead one.
    """

    def __init__(
        self,
        reconnect: Callable[[], psycopg.Connection],
        *,
        limit: int = MAX_RECONNECTS,
    ) -> None:
        self._reconnect = reconnect
        self.limit = limit
        self.count = 0

    def __call__(self, conn: psycopg.Connection | None, exc: BaseException) -> psycopg.Connection:
        self.count += 1
        if self.count > self.limit:
            raise loader_db.LoadAborted(
                f"boundary load: {self.limit} reconnects exhausted, last error {exc!r} — "
                "the session keeps dying, which is the environment and not one geometry; "
                "re-dispatch once it is healthy (loaded units are skipped on resume)"
            )
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - already dead; close is best-effort
                pass
        LOG.warning("BOUNDARY reconnecting (%d/%d) after %r", self.count, self.limit, exc)
        return self._reconnect()


def run_resilient(
    conn: psycopg.Connection,
    op: Callable[[psycopg.Connection], _T],
    reconnector: Reconnector,
) -> tuple[_T, psycopg.Connection]:
    """`op(conn)` with ONE reconnect-and-retry, returning (result, live_conn).

    For the loader's between-unit work — the per-layer done-set probe, the degenerate
    rows, the closing ANALYZE + gazetteer rebuild. Reading a 6,258-feature shapefile is
    minutes of DB silence, which is exactly when a session-mode backend gets recycled out
    from under an otherwise-healthy load, so these must survive a drop as much as the unit
    loop does. Every `op` here is idempotent (a read, an ON CONFLICT upsert, ANALYZE, or a
    delete-and-repopulate), so replaying it costs nothing.
    """
    try:
        return op(conn), conn
    except psycopg.Error as exc:
        if not db.is_transient_db_error(exc):
            raise
        dropped = exc
    conn = reconnector(conn, dropped)
    return op(conn), conn


def load_feature_resilient(
    conn: psycopg.Connection,
    feature: BoundaryFeature,
    layer: Layer,
    version_id: int,
    *,
    with_pip: bool,
    reconnector: Reconnector,
) -> tuple[bool, bool, psycopg.Connection, psycopg.Error | None]:
    """`load_feature` with ONE reconnect-and-retry when the session drops mid-unit.

    Returns (loaded, name_upgraded, live_conn, error). The caller MUST rebind its handle
    (`db.run_resilient`'s contract): `live_conn` may be a fresh session. A failure is
    RETURNED rather than raised precisely so the fresh session comes back with it — a
    raise would strand the caller on the dead handle it passed in, which is how the
    original incident turned one dropped connection into a dead run. The retry is safe to
    replay because `load_feature` is delete-then-insert inside ONE transaction, so a unit
    whose transaction died half-written has nothing committed to collide with. A
    non-psycopg exception is a bug and still propagates.
    """
    try:
        loaded, upgraded = load_feature(conn, feature, layer, version_id, with_pip=with_pip)
        return loaded, upgraded, conn, None
    except psycopg.Error as exc:
        # One unloadable geometry fails identically on any connection: reconnecting for it
        # would spend the budget on the data instead of on the outage.
        if not db.is_transient_db_error(exc):
            return False, False, conn, exc
        dropped = exc
    conn = reconnector(conn, dropped)  # raises LoadAborted past the budget
    try:
        loaded, upgraded = load_feature(conn, feature, layer, version_id, with_pip=with_pip)
    except psycopg.Error as exc:
        return False, False, conn, exc
    return loaded, upgraded, conn, None


def load_layers(
    conn: psycopg.Connection,
    extracted: Path,
    *,
    levels: tuple[str, ...],
    version_id: int,
    with_pip: bool,
    reconnector: Reconnector | None = None,
    resume: bool = True,
) -> tuple[dict[str, int], psycopg.Connection]:
    """Load every requested layer. Returns (counts, live_conn) — the connection may have
    been replaced mid-pack, so the caller MUST rebind its handle."""
    counts = {"loaded": 0, "skipped_no_unit": 0, "degenerate": 0, "failed": 0, "names": 0,
              "resumed": 0}
    reconnector = reconnector or Reconnector(loader_db.open_loader_connection)
    # Two layers share the `spravni_obvod` level, so a done-set prefetched after the first
    # of them has run would contain codes THIS run just wrote; subtracting them keeps a
    # code that collides across the pair from being skipped as if it were already loaded.
    loaded_here: dict[str, set[int]] = {}
    for layer in LAYERS:
        if layer.level not in levels:
            continue
        features, degenerate = read_layer(extracted, layer)
        assert_feature_counts(layer, features)
        done: set[int] = set()
        if resume:
            done, conn = run_resilient(
                conn, lambda c: done_codes(c, version_id, layer.level), reconnector,
            )
            done -= loaded_here.get(layer.level, set())
        LOG.info("BOUNDARY layer=%s level=%s features=%d degenerate=%d already_loaded=%d",
                 layer.token, layer.level, len(features), len(degenerate), len(done))
        if degenerate:
            counts["degenerate"] += len(degenerate)
            _, conn = run_resilient(
                conn, lambda c: _record_degenerate(c, version_id, layer, degenerate),
                reconnector,
            )
        skipped = 0
        for feature in features:
            if feature.code in done:
                skipped += 1
                continue
            loaded, upgraded, conn, error = load_feature_resilient(
                conn, feature, layer, version_id,
                with_pip=with_pip, reconnector=reconnector,
            )
            if error is not None:
                # One unloadable geometry is a discrepancy row, not the end of a 253 MB
                # pack. The row goes on its OWN connection: after a failed retry `conn` is
                # a fresh handle we have not proven yet, and the 2026-08 run showed what a
                # bookkeeping INSERT on a dead session does to the error you actually need.
                counts["failed"] += 1
                LOG.warning("BOUNDARY level=%s code=%s failed: %s",
                            feature.level, feature.code, error)
                loader_db.record_discrepancy(
                    None, version_id, entity_kind=feature.level, entity_code=feature.code,
                    discrepancy="boundary_load_failed",
                    detail={"layer": layer.token, "error": str(error)[:500]},
                    own_connection=True,
                )
                continue
            counts["loaded" if loaded else "skipped_no_unit"] += 1
            counts["names"] += int(upgraded)
            if loaded:
                loaded_here.setdefault(layer.level, set()).add(feature.code)
        if skipped:
            counts["resumed"] += skipped
            LOG.info("BOUNDARY layer=%s resumed skipped=%d of %d", layer.token, skipped,
                     len(features))
    return counts, conn


def _analyze(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("ANALYZE ruian_admin_unit_geometries")


def run(
    *,
    levels: tuple[str, ...],
    work_dir: Path,
    dry_run: bool,
    reuse: bool,
    allow_missing_pip: bool,
    skip_gazetteer: bool = False,
    resume: bool = True,
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

    # try/finally, not `with`: an assertion failure, a missing registry_version or a
    # mid-pack exception must not leak a session-mode backend holding statement_timeout=0
    # — and `load_layers` may hand back a DIFFERENT connection than it was given, so the
    # handle that gets closed has to be the live one, not the dead original.
    conn = loader_db.open_loader_connection()
    try:
        if with_pip:
            check_pip_supported(conn)
        version_id = loader_db.scalar(
            conn, "SELECT id FROM registry_versions WHERE is_current LIMIT 1"
        )
        if version_id is None:
            LOG.error("BOUNDARY no current registry_version — run the baseline load first")
            return 1
        extracted = _fetch_pack(work_dir, reuse=reuse)
        # ONE reconnect budget for the whole run: a session that keeps dying should abort
        # the load, not buy itself a fresh allowance at every phase boundary.
        reconnector = Reconnector(loader_db.open_loader_connection, limit=MAX_RECONNECTS)
        counts, conn = load_layers(conn, extracted, levels=levels,
                                   version_id=int(version_id), with_pip=with_pip,
                                   reconnector=reconnector, resume=resume)
        _, conn = run_resilient(conn, _analyze, reconnector)
        # The pack is the ONLY name source for kraj / okres / ORP / POU / KÚ / ZSJ, and the
        # gazetteer skips placeholder-named units — so without this rebuild those levels are
        # never searchable, whatever order the two jobs run in. `resumed` counts too: those
        # units were name-upgraded by the pass that died, which by definition never reached
        # this rebuild, and `name_index.rebuild` is a full idempotent recompute.
        if (counts["names"] or counts["resumed"]) and not skip_gazetteer:
            rebuilt, conn = run_resilient(
                conn, lambda c: name_index.rebuild(c, int(version_id)), reconnector,
            )
            LOG.info("BOUNDARY gazetteer rebuilt rows=%d after %d name upgrades (%d resumed)",
                     rebuilt, counts["names"], counts["resumed"])
    finally:
        conn.close()
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
    parser.add_argument("--no-resume", action="store_true",
                        help="re-load units that already have geometry for the current "
                             "registry version (default: skip them, so a re-dispatch "
                             "after a crash makes forward progress)")
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
                skip_gazetteer=args.skip_gazetteer, resume=not args.no_resume,
            )
        except BoundarySchemaError as exc:
            LOG.error("BOUNDARY %s", exc)
            return 1
        except loader_db.LoadAborted as exc:
            LOG.error("BOUNDARY %s", exc)
            return 1


if __name__ == "__main__":
    sys.exit(main())
