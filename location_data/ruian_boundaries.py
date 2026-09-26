"""The `boundaries` phase of the registry load: the ČÚZK state SHP pack into
`ruian_admin_unit_geometries` for the version being STAGED (04 C4).

Product: `https://services.cuzk.gov.cz/shp/stat/epsg-5514/1.zip` — the STATE pack, ~253 MB,
12 layers, EPSG:5514, refreshed IN PLACE daily-to-weekly (last-modified moved from 07 Aug to
24 Sep 2026 under the same URL). The URL carries no vintage, so `ruian_load` fetches the pack
as the vintage's third artifact: archived to R2 beside the CSV zips, its sha256 recorded on the
`registry_versions` row, and a resumed load that downloads different bytes refuses to go on
rather than build one version out of two packs.

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
(stát, region soudržnosti, kraj, okres, ORP, POU, katastrální území), so this phase upgrades
the placeholder names the CSV loader wrote; the loader rebuilds the gazetteer once, after it
— the gazetteer skips placeholder-named units.

CARRY-FORWARD. A monthly pack moves few boundaries, while deriving one unit costs ~1.35 s per
KÚ, nearly all of it the `authoritative` row's `ST_MaximumInscribedCircle` / `ST_MaxDistance`
over the raw vertex set. A unit whose validated source geometry is `ST_OrderingEquals` to its
newest earlier `authoritative` row gets that row copied instead, and its render + pip rows are
cut from it by the same two statements a derived unit runs (deterministic, so equal to the
previous version's): ~5 ms per KÚ, ~4.5 s for the state, flat in the number of stored
versions — where copying the pip pieces needed a bounding-box probe over every version's
pieces — and always at the current tolerance and piece size. Vertex-exact, so a PROJ or
GEOS change that moves a coordinate recomputes the unit instead of carrying a stale one.

The per-unit loop is RESUMABLE and RECONNECTING, both bounded, because an hour of per-piece
PostGIS work on one session-mode connection is long enough for the session to be dropped
under it (it was, 45 minutes into OBCE_P: an SSL EOF at obec 576069): a dropped session is
reconnected and the unit retried once (`MAX_RECONNECTS` per run, past which the environment
— not the pack — is what is broken), and a unit whose geometries are already committed for
this registry version is skipped, so a re-dispatch always moves forward. Failure-path
bookkeeping opens its own connection: the original incident reported "the connection is
closed" from the discrepancy INSERT and lost the SSL drop that caused it.
"""

from __future__ import annotations

import contextlib
import logging
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

from location_data import krovak, loader_db, name_index
from scraper import db

LOG = logging.getLogger("location_data.ruian_boundaries")

STATE_PACK_URL = "https://services.cuzk.gov.cz/shp/stat/epsg-5514/1.zip"
SUBDIVIDE_MAX_VERTICES = 256

# Per-unit statement budget. The loader's session runs `statement_timeout = 0` because a
# 3 M-row COPY needs it — but a per-unit geometry statement does not, and on 2026-08-10 a
# pack sat inside OBCE_P for 2 h 04 min in total silence (run 31434818469: last line
# `layer=OBCE_P ... already_loaded=3502`, no failure line, no reconnect line, cancelled by
# hand). `lock_timeout=5s` proves it was not a lock wait and the libpq keepalives
# (`tcp_user_timeout=30000`) prove the socket was alive, so the backend was busy inside one
# statement: `_INSERT_AUTHORITATIVE`'s `ST_MaximumInscribedCircle` / `ST_MaxDistance` are
# O(n^2)-ish over the RAW vertex set of one obec, and OBCE_P is where the monster polygons
# live. 180 s is far above the ~0.8 s/unit the healthy pass measured, so a unit that trips
# it is genuinely pathological — and tripping it is now an ERROR, which
# `load_feature_resilient` already turns into one `boundary_load_failed` discrepancy row
# and a move to the next unit.
DEFAULT_UNIT_TIMEOUT_S = 180
UNIT_TIMEOUT_ENV = "LOCATION_BOUNDARY_UNIT_TIMEOUT_S"

# One heartbeat line per this many attempted units (see `load_layers`).
PROGRESS_EVERY = 100

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
#
# Eight of the pack's 12 layers are mirror admin units, and these are they. The pack has NO
# správní obvod layer: STU_P is 642 stavební úřady keyed by an office `ID` and PRARES_P is 93
# katastrální pracoviště, and while both were mapped to `spravni_obvod` they wrote an office
# polygon onto whichever Praha správní obvod shared its number (51, 60, 78 and 205, the last
# renamed "Kutná Hora") and counted the other 731 as `skipped_no_unit` (run 36130715419).
# VO_P (volební okrsky) and ZSJ_P are not mirror levels this load serves.
LAYERS: tuple[Layer, ...] = (
    Layer("STATY_P", "stat", 111.0, 1),
    Layer("REGION_P", "region_soudrznosti", 111.0, 8),
    Layer("VUSC_P", "kraj", 111.0, 14),
    Layer("OKRESY_P", "okres", 83.0),
    Layer("ORP_P", "orp", 83.0),
    Layer("POU_P", "pou", 55.0),
    Layer("OBCE_P", "obec", 55.0),
    Layer("KATUZE_P", "katastralni_uzemi", 22.0),
)

# The levels a version may not publish without: every member unit carries both a `pip` and
# an `authoritative` row (`missing_geometry`). `stat` because the resolver's in-CZ test reads
# its polygon at the pinned version; `spravni_obvod` is not one — see LAYERS.
COMPLETE_LEVELS = ("stat", "obec", "katastralni_uzemi")

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


# The validated source geometry, stated ONCE: what `_INSERT_AUTHORITATIVE` stores and what
# `_CARRY_SOURCE_SQL` compares against the previous version's stored row.
_SOURCE_GEOM = "ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_GeomFromWKB(%(wkb)s, 4326)), 3))"

# No DELETE and no ON CONFLICT: a unit only reaches `load_feature` when it has no
# `authoritative` row for this version (`done_codes`), and every row of a unit commits in
# ONE transaction, so there is nothing of it to replace. The DELETE this replaced
# (`unit_id` + `registry_version_id`) had no index to use — `pip` rows are outside both
# btree indexes — and scanned the whole table once per unit: 5.3 s cold at two versions,
# growing with every version stored.
_INSERT_AUTHORITATIVE = f"""
WITH g AS (
  SELECT {_SOURCE_GEOM} AS geom
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

# Carry-forward, step 1: the newest EARLIER version holding this unit, if its stored
# `authoritative` geometry is vertex-for-vertex the pack's. One index read
# (`ruian_aug_auth`) and one comparison; `_SOURCE_GEOM` is the exact expression the
# authoritative row was stored with, so an unchanged boundary compares equal.
_CARRY_SOURCE_SQL = f"""
SELECT prev.registry_version_id
  FROM (SELECT a.registry_version_id, a.geom
          FROM ruian_admin_unit_geometries a
         WHERE a.unit_id = %(unit_id)s AND a.purpose = 'authoritative'
           AND a.registry_version_id < %(version_id)s
         ORDER BY a.registry_version_id DESC
         LIMIT 1) prev
 WHERE ST_OrderingEquals(prev.geom, {_SOURCE_GEOM})
"""

# Carry-forward, step 2: that version's `authoritative` row, restamped to this version — one
# row through `ruian_aug_unique_nonpip`. `load_feature` then cuts render + pip from it.
_CARRY_AUTHORITATIVE_SQL = """
INSERT INTO ruian_admin_unit_geometries
       (unit_id, registry_version_id, purpose, generalization_tolerance_m,
        simplify_algorithm, geom, area_m2, representative_point, inscribed_radius_m,
        centroid_point, containment_radius_m, max_radius_m)
SELECT g.unit_id, %(version_id)s, g.purpose, g.generalization_tolerance_m,
       g.simplify_algorithm, g.geom, g.area_m2, g.representative_point,
       g.inscribed_radius_m, g.centroid_point, g.containment_radius_m, g.max_radius_m
  FROM ruian_admin_unit_geometries g
 WHERE g.unit_id = %(unit_id)s AND g.registry_version_id = %(from_version_id)s
   AND g.purpose = 'authoritative'
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


def upgrade_name(cur: psycopg.Cursor, unit_id: int, name: str) -> bool:
    """Replace a code placeholder written by the CSV loader with the pack's real name.

    Takes the CURSOR, not the connection: it runs inside `load_feature`'s bounded
    transaction and must inherit that transaction's `SET LOCAL statement_timeout`, which a
    cursor opened here would still get but a second connection would not.

    Only ever touches a unit still named after its own code, so a real RÚIAN name can
    never be overwritten by a DBF variant. Descendants keep their `display_path` until the
    next baseline recomputes it — the code path (`path`) is unaffected either way.
    """
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
    unit_timeout_s: int | None = None,
) -> tuple[str | None, bool]:
    """(how, name_upgraded) for one feature — `how` is "carried" or "derived", None when no
    unit matches.

    Every statement of the unit runs under a bounded `statement_timeout`, armed with
    `SET LOCAL` INSIDE this transaction (the loader's session default is 0, for COPY).
    `upgrade_name` moved inside the guarded cursor for the same reason — it is part of the
    unit's transaction and must not be the one statement that can hang.
    """
    unit_id = unit_id_for(conn, feature.level, feature.code)
    if unit_id is None:
        return None, False
    budget = (
        loader_db.env_timeout_s(UNIT_TIMEOUT_ENV, DEFAULT_UNIT_TIMEOUT_S)
        if unit_timeout_s is None
        else unit_timeout_s
    )
    unit = {"unit_id": unit_id, "version_id": version_id}
    with loader_db.bounded(conn, budget) as cur:
        upgraded = upgrade_name(cur, unit_id, feature.name)
        cur.execute(_CARRY_SOURCE_SQL, {**unit, "wkb": feature.wkb})
        carried_from = cur.fetchone()
        if carried_from is None:
            cur.execute(_INSERT_AUTHORITATIVE, {**unit, "wkb": feature.wkb})
        else:
            cur.execute(_CARRY_AUTHORITATIVE_SQL, {**unit, "from_version_id": carried_from[0]})
        cur.execute(_INSERT_RENDER, {**unit, "tolerance_m": layer.render_tolerance_m})
        cur.execute(_INSERT_PIP, {**unit, "max_vertices": SUBDIVIDE_MAX_VERTICES})
    return ("derived" if carried_from is None else "carried"), upgraded


def _extract(pack: Path) -> Path:
    extracted = pack.parent / "shp"
    extracted.mkdir(exist_ok=True)
    with zipfile.ZipFile(pack) as zf:
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
# `load_feature`'s per-unit transaction, which commits the name upgrade and all THREE
# geometries (derived or carried) together — so its presence is proof the whole unit
# landed and the unit can be skipped whole. Prefetched ONCE per layer (a per-unit
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

    For the phase's between-unit work — the per-layer done-set probe, the degenerate
    rows, the closing ANALYZE. Reading a 6,258-feature shapefile is
    minutes of DB silence, which is exactly when a session-mode backend gets recycled out
    from under an otherwise-healthy load, so these must survive a drop as much as the unit
    loop does. Every `op` here is idempotent (a read, an ON CONFLICT upsert or ANALYZE),
    so replaying it costs nothing.
    """
    try:
        return op(conn), conn
    except psycopg.Error as exc:
        if not db.is_transient_db_error(exc):
            raise
        dropped = exc
    conn = reconnector(conn, dropped)
    return op(conn), conn


def _is_unit_fault(exc: psycopg.Error) -> bool:
    """A statement timeout is THIS UNIT's problem, not the environment's.

    `db.is_transient_db_error` answers True for every `OperationalError`, and
    `QueryCanceled` (what `SET LOCAL statement_timeout` raises) is one — so without this
    the per-unit budget would burn two reconnects on every pathological geometry and a
    couple of dozen of them would abort the pack with "reconnects exhausted", blaming the
    environment for the data. A timed-out unit is a `boundary_load_failed` discrepancy row
    and the loader moves to the next one, on the same healthy connection.
    """
    return isinstance(exc, psycopg.errors.QueryCanceled)


def load_feature_resilient(
    conn: psycopg.Connection,
    feature: BoundaryFeature,
    layer: Layer,
    version_id: int,
    *,
    reconnector: Reconnector,
) -> tuple[str | None, bool, psycopg.Connection, psycopg.Error | None]:
    """`load_feature` with ONE reconnect-and-retry when the session drops mid-unit.

    Returns (how, name_upgraded, live_conn, error). The caller MUST rebind its handle
    (`db.run_resilient`'s contract): `live_conn` may be a fresh session. A failure is
    RETURNED rather than raised precisely so the fresh session comes back with it — a
    raise would strand the caller on the dead handle it passed in, which is how the
    original incident turned one dropped connection into a dead run. The retry is safe to
    replay because `load_feature` writes the unit inside ONE transaction, so a unit whose
    transaction died half-written has nothing committed to collide with. A non-psycopg
    exception is a bug and still propagates.
    """
    try:
        how, upgraded = load_feature(conn, feature, layer, version_id)
        return how, upgraded, conn, None
    except psycopg.Error as exc:
        # One unloadable geometry fails identically on any connection: reconnecting for it
        # would spend the budget on the data instead of on the outage.
        if _is_unit_fault(exc) or not db.is_transient_db_error(exc):
            return None, False, conn, exc
        dropped = exc
    conn = reconnector(conn, dropped)  # raises LoadAborted past the budget
    try:
        how, upgraded = load_feature(conn, feature, layer, version_id)
    except psycopg.Error as exc:
        return None, False, conn, exc
    return how, upgraded, conn, None


def load_layers(
    conn: psycopg.Connection,
    extracted: Path,
    *,
    version_id: int,
    reconnector: Reconnector | None = None,
) -> tuple[dict[str, int], psycopg.Connection]:
    """Load every layer into `version_id`. Returns (counts, live_conn) — the connection may
    have been replaced mid-pack, so the caller MUST rebind its handle."""
    counts = {"loaded": 0, "carried": 0, "skipped_no_unit": 0, "degenerate": 0, "failed": 0,
              "names": 0, "resumed": 0}
    reconnector = reconnector or Reconnector(loader_db.open_loader_connection)
    for layer in LAYERS:
        features, degenerate = read_layer(extracted, layer)
        assert_feature_counts(layer, features)
        done, conn = run_resilient(
            conn, lambda c: done_codes(c, version_id, layer.level), reconnector,
        )
        LOG.info("BOUNDARY layer=%s level=%s features=%d degenerate=%d already_loaded=%d",
                 layer.token, layer.level, len(features), len(degenerate), len(done))
        if degenerate:
            counts["degenerate"] += len(degenerate)
            _, conn = run_resilient(
                conn, lambda c: _record_degenerate(c, version_id, layer, degenerate),
                reconnector,
            )
        skipped = 0
        attempted = 0
        for feature in features:
            if feature.code in done:
                skipped += 1
                continue
            # A heartbeat, because the 2026-08-10 run's whole diagnosis was "two hours of
            # silence": the only per-unit lines this loader had were failure lines, so a
            # healthy-but-slow pass and a wedged one looked identical from the log. The
            # code being loaded is printed BEFORE the statement runs, so the next silent
            # run names its own suspect.
            attempted += 1
            if attempted % PROGRESS_EVERY == 1:
                LOG.info("BOUNDARY layer=%s at code=%s (%d/%d attempted)",
                         layer.token, feature.code, attempted, len(features) - len(done))
            how, upgraded, conn, error = load_feature_resilient(
                conn, feature, layer, version_id, reconnector=reconnector,
            )
            if error is not None:
                # One unloadable geometry is a discrepancy row, not the end of a 253 MB
                # pack — and the completeness assertion then holds the version back. The
                # row goes on its OWN connection: after a failed retry `conn` is a fresh
                # handle we have not proven yet, and the 2026-08 run showed what a
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
            if how is None:
                counts["skipped_no_unit"] += 1
                continue
            counts["loaded"] += 1
            counts["carried"] += how == "carried"
            counts["names"] += int(upgraded)
        if skipped:
            counts["resumed"] += skipped
            LOG.info("BOUNDARY layer=%s resumed skipped=%d of %d", layer.token, skipped,
                     len(features))
    return counts, conn


def _analyze(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("ANALYZE ruian_admin_unit_geometries")


def load_pack(
    conn: psycopg.Connection, pack: Path, version_id: int,
) -> tuple[dict[str, int], psycopg.Connection]:
    """The loader's `boundaries` phase: every layer of `pack` into `version_id`, then ANALYZE.

    Returns (counts, live_conn); the caller MUST rebind its handle. ONE reconnect budget for
    the whole phase: a session that keeps dying aborts the load instead of buying itself a
    fresh allowance per layer.
    """
    reconnector = Reconnector(loader_db.open_loader_connection, limit=MAX_RECONNECTS)
    counts, conn = load_layers(conn, _extract(pack), version_id=version_id,
                               reconnector=reconnector)
    _, conn = run_resilient(conn, _analyze, reconnector)
    LOG.info("BOUNDARY done version=%s %s", version_id, counts)
    return counts, conn


def dry_run(pack: Path) -> None:
    """Read every layer and assert its feature count; write nothing."""
    extracted = _extract(pack)
    for layer in LAYERS:
        features, degenerate = read_layer(extracted, layer)
        assert_feature_counts(layer, features)
        LOG.info("BOUNDARY layer=%s level=%s features=%d degenerate=%d", layer.token,
                 layer.level, len(features), len(degenerate))


# Membership is the version's, not "whatever is valid today": a unit is in version V when V
# is inside its [first_version_id, last_version_id] span and it was not closed BY V (a close
# stamps last_version_id = V together with valid_to). A missing unit is excused only by a
# `degenerate_boundary_geometry` row — the pack itself holds no usable polygon for it; a
# `boundary_load_failed` row excuses nothing, the unit is simply retried by the next run.
# `landed` is one pass over this version's rows rather than a per-unit probe: `pip` rows sit
# outside both btree indexes, so a per-unit EXISTS would scan the table once per unit.
_MISSING_GEOMETRY_SQL = """
WITH member AS (
  SELECT u.id, u.level, u.code
    FROM ruian_admin_units u
   WHERE u.level::text = ANY(%(levels)s)
     AND u.first_version_id <= %(version_id)s AND u.last_version_id >= %(version_id)s
     AND (u.valid_to IS NULL OR u.last_version_id > %(version_id)s)
), landed AS (
  SELECT g.unit_id
    FROM ruian_admin_unit_geometries g
   WHERE g.registry_version_id = %(version_id)s AND g.purpose IN ('pip', 'authoritative')
   GROUP BY g.unit_id
  HAVING count(DISTINCT g.purpose) = 2
)
SELECT m.level::text, count(*), (array_agg(m.code ORDER BY m.code))[1:10]
  FROM member m
 WHERE NOT EXISTS (SELECT 1 FROM landed l WHERE l.unit_id = m.id)
   AND NOT EXISTS (
         SELECT 1 FROM registry_load_discrepancies d
          WHERE d.registry_version_id = %(version_id)s AND d.entity_kind = m.level
            AND d.entity_code = m.code AND d.discrepancy = 'degenerate_boundary_geometry')
 GROUP BY m.level
 ORDER BY 1
"""


def missing_geometry(
    conn: psycopg.Connection, version_id: int,
) -> list[tuple[str, int, list[int]]]:
    """(level, units, first codes) of every `COMPLETE_LEVELS` member of `version_id` still
    lacking a `pip` or an `authoritative` row; empty when the version may publish."""
    with conn.cursor() as cur:
        cur.execute(_MISSING_GEOMETRY_SQL,
                    {"levels": list(COMPLETE_LEVELS), "version_id": version_id})
        return [(str(level), int(n), list(codes or [])) for level, n, codes in cur.fetchall()]
