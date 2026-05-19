"""Ingest Czech administrative boundaries into admin_boundaries.

Source is ČÚZK's RÚIAN state-level shapefile pack (CC-BY 4.0) — see Part D1
of map-1's design notes. The script does not attempt to chase the dated
filename automatically; the operator pastes the current URL into the
GitHub Actions workflow input after looking it up on the ČÚZK ATOM feed.

Phases:
    1. Fetch       — download the ZIP (or use a local --source-path).
    2. Extract     — unpack into a temp dir.
    3. Per-level   — load shapefile, reproject EPSG:5514 -> EPSG:4326,
                     simplify, INSERT.
    4. Spatial join — for each level, set admin_boundaries.sreality_id
                     to the most-common locality_*_id of listings whose
                     point falls inside the polygon.
    5. Areas       — UPDATE area_km2 once geometries are loaded.

Levels are loaded in hierarchy order (kraj -> okres -> obec -> ku) so
parent_id FKs always resolve.

This script writes to admin_boundaries; it deliberately TRUNCATEs the
table at the start of phase 3 so a re-run reproduces the live state
rather than accumulating stale rows. That's safe because admin_boundaries
is a pure mirror of ČÚZK source data — no derived analytical state lives
in it.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pyproj
import shapefile  # type: ignore[import-untyped]
import shapely.geometry
import shapely.ops
from shapely.geometry.base import BaseGeometry

LOG = logging.getLogger("ingest_boundaries")


LEVELS: tuple[str, ...] = ("kraj", "okres", "obec", "ku")

# Tolerance in degrees (4326 units). 1 degree of latitude is ~111 km.
# Tighter on small units so they don't disappear into single points;
# looser on large units to keep file size and render cost down.
SIMPLIFY_TOLERANCE_DEG: dict[str, float] = {
    "kraj": 0.001,    # ~111 m
    "okres": 0.00075,  # ~83 m
    "obec": 0.0005,   # ~55 m
    "ku": 0.0002,     # ~22 m
}

# Tokens we look for in shapefile filenames per level. Current ČÚZK
# state packs (services.cuzk.gov.cz/shp/stat/epsg-5514/1.zip) use
# names like VUSC_P.shp (kraje as Vyšší územní samosprávné celky),
# OKRESY_P.shp, OBCE_P.shp, KATUZE_P.shp. Older / alternative packs
# may use ST_KR/ST_OK/ST_OB/ST_KU or the bare Czech words KRAJE/
# OKRESY/OBCE/KATASTR. Match is case-insensitive substring; among
# matches we prefer the largest .shp file (heuristic for "actual
# data, not a metadata variant").
#
# `REGION_P.shp` (NUTS-2 cohesion regions, 8 areas like Severozápad)
# is NOT the same as `VUSC_P.shp` (NUTS-3 kraje, 14 regions) — only
# the latter maps to what curated_cities.kraj_name expects, so VUSC
# is the right token for level=kraj.
LEVEL_FILE_TOKENS: dict[str, tuple[str, ...]] = {
    "kraj": ("ST_KR", "KRAJE", "VUSC", "KRAJ"),
    "okres": ("ST_OK", "OKRESY", "OKRES"),
    "obec": ("ST_OB", "OBCE", "OBEC"),
    "ku": ("ST_KU", "KATUZE", "KATASTR", "_KU"),
}

# Candidate DBF column names for each semantic field, in priority order.
# Different RÚIAN exports use slightly different names (KOD vs KOD_KU_,
# NAZEV vs NAZ_KU, KOD_OK_ vs KODOKR). We look up by candidates so the
# script doesn't break on the next minor producer change.
FIELD_CANDIDATES = {
    "code": ("KOD", "Kod", "KOD_KU_", "KOD_OB_", "KOD_OK_", "KOD_KR_", "ID"),
    "name": ("NAZEV", "Nazev", "NAZ_KU", "NAZ_OB", "NAZ_OK", "NAZ_KR", "NAME"),
    "parent_kraj":  ("KOD_KR_", "KodKr", "KOD_KRAJ", "KODKR"),
    "parent_okres": ("KOD_OK_", "KodOk", "KOD_OKRES", "KODOK"),
    "parent_obec":  ("KOD_OB_", "KodOb", "KOD_OBEC", "KODOB"),
}

PARENT_FIELD_BY_LEVEL: dict[str, str | None] = {
    "kraj": None,
    "okres": "parent_kraj",
    "obec": "parent_okres",
    "ku": "parent_obec",
}


@dataclass(frozen=True)
class BoundaryRow:
    id: int
    level: str
    name: str
    parent_id: int | None
    geom_wkt: str  # MULTIPOLYGON in EPSG:4326


# ---------- phase 1: fetch ----------


def fetch_source(url: str, dest_dir: Path) -> Path:
    """Download the ZIP to dest_dir/source.zip; return the path."""
    dest = dest_dir / "source.zip"
    LOG.info("FETCH url=%s", url)
    with urllib.request.urlopen(url, timeout=600) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)
    LOG.info("FETCH done bytes=%d path=%s", dest.stat().st_size, dest)
    return dest


# ---------- phase 2: extract ----------


def extract_zip(zip_path: Path, dest_dir: Path) -> Path:
    """Extract the ZIP and return the directory it was unpacked into."""
    LOG.info("EXTRACT zip=%s into=%s", zip_path, dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    return dest_dir


def find_shapefile(root: Path, level: str) -> Path:
    """Locate the .shp file in `root` whose name matches the level's tokens."""
    tokens = LEVEL_FILE_TOKENS[level]
    all_shps = sorted(root.rglob("*.shp"))
    candidates: list[Path] = []
    for shp in all_shps:
        upper = shp.name.upper()
        if any(tok.upper() in upper for tok in tokens):
            candidates.append(shp)
    if not candidates:
        sample = [str(p.relative_to(root)) for p in all_shps[:30]]
        more = f" (+{len(all_shps) - 30} more)" if len(all_shps) > 30 else ""
        raise FileNotFoundError(
            f"No .shp file matching {tokens} found under {root}. "
            f"Total .shp files in archive: {len(all_shps)}. "
            f"Sample filenames: {sample}{more}. "
            f"If the archive looks partial (e.g. only OBCE_P / ORP_P), "
            f"the source URL points at a per-obec or per-level chunk "
            f"rather than the full state pack. Use the URL from ATOM feed "
            f"https://atom.cuzk.gov.cz/RUIAN-STATY-SHP/datasetFeeds/CZ-00025712-CUZK_RUIAN-STATY-SHP_1.xml"
        )
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    LOG.info("MATCH level=%s file=%s", level, candidates[0].name)
    return candidates[0]


# ---------- phase 3: load + reproject + simplify ----------


def field_index(field_names: list[str], candidates: tuple[str, ...]) -> int | None:
    """Return the index of the first candidate present in field_names, or None."""
    upper_lookup = {f.upper(): i for i, f in enumerate(field_names)}
    for cand in candidates:
        idx = upper_lookup.get(cand.upper())
        if idx is not None:
            return idx
    return None


def to_multipolygon(geom: BaseGeometry) -> shapely.geometry.MultiPolygon:
    """Coerce a shapely geometry into MultiPolygon. Validity-fix on the way."""
    if not geom.is_valid:
        geom = geom.buffer(0)
    if isinstance(geom, shapely.geometry.MultiPolygon):
        return geom
    if isinstance(geom, shapely.geometry.Polygon):
        return shapely.geometry.MultiPolygon([geom])
    if isinstance(geom, shapely.geometry.GeometryCollection):
        polys = [g for g in geom.geoms if isinstance(g, shapely.geometry.Polygon)]
        if not polys:
            raise ValueError("GeometryCollection has no Polygon members")
        return shapely.geometry.MultiPolygon(polys)
    raise ValueError(f"Cannot coerce {geom.geom_type} to MultiPolygon")


def iter_boundary_rows(
    shp_path: Path,
    level: str,
    transformer: pyproj.Transformer,
) -> Iterator[BoundaryRow]:
    """Yield BoundaryRow per record in the shapefile (reprojected, simplified)."""
    parent_field_key = PARENT_FIELD_BY_LEVEL[level]
    tolerance = SIMPLIFY_TOLERANCE_DEG[level]

    reader = shapefile.Reader(str(shp_path), encoding="cp1250")
    field_names = [f[0] for f in reader.fields[1:]]

    code_idx = field_index(field_names, FIELD_CANDIDATES["code"])
    name_idx = field_index(field_names, FIELD_CANDIDATES["name"])
    if code_idx is None or name_idx is None:
        raise ValueError(
            f"Could not locate code/name fields in {shp_path.name}; "
            f"available: {field_names}"
        )
    parent_idx: int | None = None
    if parent_field_key is not None:
        parent_idx = field_index(field_names, FIELD_CANDIDATES[parent_field_key])
        if parent_idx is None:
            LOG.warning(
                "PARENT level=%s no parent column matched; "
                "parent_id will be NULL. Available: %s",
                level, field_names,
            )

    project = transformer.transform

    skipped = 0
    yielded = 0
    for shape_record in reader.iterShapeRecords():
        try:
            geom_geojson = shape_record.shape.__geo_interface__
        except Exception as exc:  # malformed shape entry
            LOG.warning("SHAPE skip level=%s reason=%r", level, exc)
            skipped += 1
            continue
        geom = shapely.geometry.shape(geom_geojson)
        try:
            geom = shapely.ops.transform(project, geom)
            geom = geom.simplify(tolerance, preserve_topology=True)
            multi = to_multipolygon(geom)
        except Exception as exc:
            LOG.warning("GEOM skip level=%s reason=%r", level, exc)
            skipped += 1
            continue

        record = shape_record.record
        try:
            unit_id = int(record[code_idx])
        except (TypeError, ValueError) as exc:
            LOG.warning("CODE skip level=%s reason=%r record=%r", level, exc, record)
            skipped += 1
            continue
        name = str(record[name_idx]).strip() if record[name_idx] is not None else ""
        parent_id: int | None = None
        if parent_idx is not None:
            raw_parent = record[parent_idx]
            if raw_parent not in (None, "", 0):
                try:
                    parent_id = int(raw_parent)
                except (TypeError, ValueError):
                    parent_id = None

        yield BoundaryRow(
            id=unit_id,
            level=level,
            name=name,
            parent_id=parent_id,
            geom_wkt=multi.wkt,
        )
        yielded += 1

    LOG.info("LOAD level=%s yielded=%d skipped=%d", level, yielded, skipped)


# ---------- phase 3 (cont.): DB writes ----------


INSERT_SQL = """
INSERT INTO admin_boundaries (id, level, name, parent_id, geom)
VALUES (
    %s, %s, %s, %s,
    ST_Multi(ST_GeomFromText(%s, 4326))::geography
)
ON CONFLICT (id) DO UPDATE SET
  level = EXCLUDED.level,
  name = EXCLUDED.name,
  parent_id = EXCLUDED.parent_id,
  geom = EXCLUDED.geom,
  ingested_at = now()
"""

INSERT_BATCH_SIZE = 200


def insert_rows(
    conn: psycopg.Connection,
    rows: Iterator[BoundaryRow],
) -> int:
    """Bulk-insert BoundaryRows in batches. Returns total inserted/updated."""
    total = 0
    batch: list[BoundaryRow] = []
    with conn.cursor() as cur:
        for row in rows:
            batch.append(row)
            if len(batch) >= INSERT_BATCH_SIZE:
                _flush_batch(cur, batch)
                total += len(batch)
                batch.clear()
        if batch:
            _flush_batch(cur, batch)
            total += len(batch)
    return total


def _flush_batch(cur: psycopg.Cursor, batch: list[BoundaryRow]) -> None:
    cur.executemany(
        INSERT_SQL,
        [
            (row.id, row.level, row.name, row.parent_id, row.geom_wkt)
            for row in batch
        ],
    )


# ---------- phase 4: spatial join ----------


# Mapping from admin_boundaries.level to the corresponding listings
# sreality-id column. quarter has no admin_boundaries level (městská
# části aren't part of the four ČÚZK shapefiles we ingest); aggregating
# by quarter is intentionally out of scope for v1.
LEVEL_TO_LISTING_COLUMN: dict[str, str] = {
    "kraj": "locality_region_id",
    "okres": "locality_district_id",
    "obec": "locality_municipality_id",
    "ku": "locality_ward_id",
}


def populate_sreality_ids(conn: psycopg.Connection, level: str) -> dict[str, int]:
    """For each polygon at `level`, pick the most-common listing locality_*_id
    of points inside it and write to admin_boundaries.sreality_id.

    Returns counts: matched (polygons that got an id), empty (no listing
    inside), conflicted (multiple listings disagreed; we picked the mode).
    """
    listing_col = LEVEL_TO_LISTING_COLUMN[level]
    sql = f"""
        WITH points_in_poly AS (
            SELECT
                ab.id AS ab_id,
                l.{listing_col} AS sid,
                COUNT(*) AS n
            FROM admin_boundaries ab
            JOIN listings l
              ON ST_Covers(ab.geom, l.geom)
            WHERE ab.level = %s
              AND l.is_active
              AND l.geom IS NOT NULL
              AND l.{listing_col} IS NOT NULL
            GROUP BY ab.id, l.{listing_col}
        ),
        ranked AS (
            SELECT
                ab_id, sid, n,
                ROW_NUMBER() OVER (PARTITION BY ab_id ORDER BY n DESC, sid) AS rk,
                COUNT(*)    OVER (PARTITION BY ab_id) AS n_distinct
            FROM points_in_poly
        ),
        winners AS (
            SELECT ab_id, sid, n_distinct FROM ranked WHERE rk = 1
        ),
        applied AS (
            UPDATE admin_boundaries ab
            SET sreality_id = w.sid
            FROM winners w
            WHERE ab.id = w.ab_id
            RETURNING ab.id, w.n_distinct
        )
        SELECT
            (SELECT COUNT(*) FROM applied) AS matched,
            (SELECT COUNT(*) FROM applied WHERE n_distinct > 1) AS conflicted,
            (SELECT COUNT(*) FROM admin_boundaries
             WHERE level = %s AND sreality_id IS NULL)             AS empty
    """
    with conn.cursor() as cur:
        cur.execute(sql, (level, level))
        row = cur.fetchone()
        if row is None:
            return {"matched": 0, "conflicted": 0, "empty": 0}
        return {"matched": row[0], "conflicted": row[1], "empty": row[2]}


# ---------- phase 5: area_km2 ----------


def compute_areas(conn: psycopg.Connection) -> int:
    """Populate area_km2 for any rows missing it."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE admin_boundaries "
            "SET area_km2 = ROUND((ST_Area(geom) / 1e6)::numeric, 3) "
            "WHERE area_km2 IS NULL"
        )
        return cur.rowcount or 0


# ---------- orchestration ----------


def wipe_table(conn: psycopg.Connection) -> int:
    """Wipe admin_boundaries before a fresh load.

    Pure-mirror table (no derived state); rebuild rather than diff
    against an unknown prior version. Spatial-join updates run after.

    DELETE rather than TRUNCATE because foreign keys point at this
    table — admin_boundaries.parent_id (self-FK from migration 017)
    and curated_cities.admin_boundary_id (from migration 081). Postgres
    refuses TRUNCATE on a table with inbound FKs unless every
    referencing table is also truncated. DELETE fires each FK's
    ON DELETE SET NULL action instead, so curated_cities rows are
    preserved with NULL admin_boundary_id (re-linked below).
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM admin_boundaries")
        return cur.rowcount or 0


def populate_parent_ids_spatial(conn: psycopg.Connection) -> dict[str, int]:
    """Backfill admin_boundaries.parent_id via PostGIS containment.

    The DBF-based parent extraction in iter_boundary_rows stays as a
    no-cost optimisation; when the source column is recognised by
    FIELD_CANDIDATES, parent_id is set during INSERT and this step
    does nothing (idempotent on WHERE parent_id IS NULL). When the
    source column is renamed by ČÚZK and not yet recognised, this
    step fills the gap so the four-level hierarchy stays walkable.

    Same predicate as migration 083: ST_PointOnSurface (interior
    point, robust to concave/annular shapes) and ST_Covers
    (boundary-inclusive, robust to simplification edges).
    """
    sql = '''
        update admin_boundaries c
           set parent_id = (
             select p.id
               from admin_boundaries p
              where p.level = case c.level
                                when 'okres' then 'kraj'
                                when 'obec'  then 'okres'
                                when 'ku'    then 'obec'
                              end
                and st_covers(
                      p.geom,
                      st_pointonsurface(c.geom::geometry)::geography)
              order by st_area(p.geom::geometry) asc
              limit 1
           )
         where c.level <> 'kraj'
           and c.parent_id is null
    '''
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute('''
            select level,
                   count(*) as total,
                   count(parent_id) as with_parent
              from admin_boundaries
             where level <> 'kraj'
             group by level
        ''')
        out: dict[str, int] = {}
        for level, total, with_parent in cur.fetchall():
            out[f"{level}_total"] = int(total)
            out[f"{level}_linked"] = int(with_parent)
    for lvl in ('okres', 'obec', 'ku'):
        out.setdefault(f"{lvl}_total", 0)
        out.setdefault(f"{lvl}_linked", 0)
    return out


def relink_curated_cities(conn: psycopg.Connection) -> dict[str, int]:
    """Re-establish curated_cities.admin_boundary_id after a fresh load.

    Matches each curated city to the obec polygon that contains its
    centroid. Same predicate as migration 082. Direct spatial
    containment is more robust than the name-walk migration 081 used:
    it needs neither admin_boundaries.parent_id (which the current
    ČÚZK DBF schema doesn't expose under the column names
    FIELD_CANDIDATES looks for) nor name disambiguation. Idempotent —
    only touches rows where admin_boundary_id is currently NULL.
    """
    sql = '''
        update curated_cities c
           set admin_boundary_id = (
             select b.id
               from admin_boundaries b
              where b.level = 'obec'
                and st_covers(b.geom, c.centroid)
              order by st_area(b.geom::geometry) asc
              limit 1
           )
         where c.admin_boundary_id is null
    '''
    with conn.cursor() as cur:
        cur.execute(sql)
        linked = cur.rowcount or 0
        cur.execute(
            "select count(*) from curated_cities where admin_boundary_id is null"
        )
        row = cur.fetchone()
        unmatched = int(row[0]) if row else 0
        cur.execute("select count(*) from curated_cities")
        row = cur.fetchone()
        total = int(row[0]) if row else 0
    return {"linked": linked, "unmatched": unmatched, "total": total}


def run_pipeline(args: argparse.Namespace) -> int:
    levels = [level.strip() for level in args.levels.split(",")] if args.levels else list(LEVELS)
    for lvl in levels:
        if lvl not in LEVELS:
            raise SystemExit(f"Unknown level: {lvl!r}; valid: {LEVELS}")

    if not args.source_url and not args.source_path:
        raise SystemExit("Must provide --source-url or --source-path")

    transformer = pyproj.Transformer.from_crs(
        "EPSG:5514", "EPSG:4326", always_xy=True,
    )

    with tempfile.TemporaryDirectory(prefix="ingest_boundaries_") as tmp:
        tmp_dir = Path(tmp)

        if args.source_path:
            src = Path(args.source_path)
            if src.is_dir():
                extract_dir = src
            else:
                extract_dir = extract_zip(src, tmp_dir / "extract")
        else:
            zip_path = fetch_source(args.source_url, tmp_dir)
            extract_dir = extract_zip(zip_path, tmp_dir / "extract")

        inventory = sorted(extract_dir.rglob("*.shp"))
        if not inventory:
            LOG.warning(
                "INVENTORY no .shp files under %s. Archive contents may "
                "be nested (per-obec sub-zips?) — extraction does NOT recurse "
                "into nested zips.", extract_dir,
            )
        else:
            preview = [p.name for p in inventory[:20]]
            more = f" (+{len(inventory) - 20} more)" if len(inventory) > 20 else ""
            LOG.info(
                "INVENTORY shp_count=%d sample=%s%s",
                len(inventory), preview, more,
            )

        if args.dry_run:
            LOG.info("DRY-RUN: would now load %s into admin_boundaries", levels)
            for lvl in levels:
                shp = find_shapefile(extract_dir, lvl)
                # Iterate to validate parsing works end-to-end:
                count = sum(1 for _ in iter_boundary_rows(shp, lvl, transformer))
                LOG.info("DRY-RUN level=%s rows=%d", lvl, count)
            return 0

        with psycopg.connect(
            os.environ["SUPABASE_DB_URL"],
            autocommit=False,
            prepare_threshold=None,
        ) as conn:
            LOG.info("DB connected")
            if args.truncate:
                LOG.info("WIPE admin_boundaries starting")
                deleted = wipe_table(conn)
                conn.commit()
                LOG.info("WIPE done deleted=%d", deleted)

            for lvl in levels:
                shp = find_shapefile(extract_dir, lvl)
                rows_iter = iter_boundary_rows(shp, lvl, transformer)
                inserted = insert_rows(conn, rows_iter)
                conn.commit()
                LOG.info("INSERT level=%s rows=%d", lvl, inserted)

            LOG.info("AREAS computing")
            n_areas = compute_areas(conn)
            conn.commit()
            LOG.info("AREAS done updated=%d", n_areas)

            LOG.info("PARENTS spatial-backfill starting")
            parents = populate_parent_ids_spatial(conn)
            conn.commit()
            LOG.info(
                "PARENTS done okres=%d/%d obec=%d/%d ku=%d/%d",
                parents['okres_linked'], parents['okres_total'],
                parents['obec_linked'],  parents['obec_total'],
                parents['ku_linked'],    parents['ku_total'],
            )

            if not args.skip_spatial_join:
                for lvl in levels:
                    LOG.info("JOIN level=%s starting", lvl)
                    counts = populate_sreality_ids(conn, lvl)
                    conn.commit()
                    LOG.info(
                        "JOIN level=%s matched=%d empty=%d conflicted=%d",
                        lvl, counts["matched"], counts["empty"], counts["conflicted"],
                    )
            else:
                LOG.info("JOIN skipped (--skip-spatial-join)")

            if "obec" in levels:
                LOG.info("RELINK curated_cities starting")
                relink = relink_curated_cities(conn)
                conn.commit()
                LOG.info(
                    "RELINK done linked=%d unmatched=%d total=%d",
                    relink["linked"], relink["unmatched"], relink["total"],
                )

    LOG.info("RUN done")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-url", help="HTTPS URL of the RÚIAN SHP zip")
    p.add_argument("--source-path", help="Local path to a zip or pre-extracted dir")
    p.add_argument("--levels", help="Comma-separated levels to load (default: all)")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and validate but do not write to DB")
    p.add_argument("--truncate", action="store_true", default=True,
                   help="TRUNCATE admin_boundaries before loading (default true)")
    p.add_argument("--no-truncate", dest="truncate", action="store_false",
                   help="Skip TRUNCATE; ON CONFLICT will still upsert")
    p.add_argument("--skip-spatial-join", action="store_true",
                   help="Skip the listings -> sreality_id population step")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
