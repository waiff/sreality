"""`pin_collision_recompute` — mint a new collision epoch (03 §3.8.4, 00 §10.3).

A recompute writes a NEW, IMMUTABLE `pin_cluster_epochs` row plus the `pin_clusters` rows
stamped with it — never an in-place overwrite — and then enqueues into `dirty_locations`
ONLY the listings whose bucket changed. That is the whole point of the epoch: without it a
reclassified cluster cannot invalidate the resolutions that consumed the old
classification, and stale precision keeps serving map pins and geo blocks.

An epoch that never advances silently freezes precision, so the job mints a row even when
it reclassifies nothing (~1 row) and reports `reclassified_count = 0`.

**Bootstrap.** The pin rows are read from `listing_location_current`, which is the
resolver's own output, so the very first epoch on an empty projection produces zero
clusters — every listing then resolves with `classification='normal'` and the SECOND epoch
carries real evidence. That is self-correcting and deliberate: the alternative (clustering
raw claims) would cluster coordinates the resolver has not yet accepted as positions.

CLI:  python -m location_data.resolver.epoch_job [--sources a,b] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys

import psycopg

from location_data.resolver import collision, lease, resolve_db
from location_data.resolver.normalize import normalize_match_key
from location_data.resolver.version import POLICY_VERSION_DEFAULT
from scraper import db

LOG = logging.getLogger("location_data.resolver.epoch_job")

JOB_NAME = "pin_collision_recompute"
CONCURRENCY_GROUP = "location-collision"

_PIN_ROWS_SQL = """
SELECT listing_id, source, ST_Y(geom), ST_X(geom), street_name, obec_kod,
       blur_evidence::text IN ('declared', 'both'), is_cz
  FROM listing_location_current
 WHERE geom IS NOT NULL
   AND (cardinality(%s::text[]) = 0 OR source = ANY(%s::text[]))
 ORDER BY listing_id
"""

_CENTROID_DISTANCE_SQL = """
SELECT u.code,
       ST_Distance(g.representative_point::geography,
                   ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography),
       u.id
  FROM ruian_admin_unit_geometries g
  JOIN ruian_admin_units u ON u.id = g.unit_id
 WHERE g.registry_version_id = %s AND g.purpose = 'authoritative' AND u.code = %s
   AND u.level = 'obec'
 LIMIT 1
"""

_INSERT_EPOCH_SQL = """
INSERT INTO pin_cluster_epochs
       (policy_version, registry_version_id, sources, cluster_count, reclassified_count,
        parent_epoch_id, note)
VALUES (%s, %s, %s, %s, %s, %s, %s)
RETURNING id
"""

_INSERT_CLUSTER_SQL = """
INSERT INTO pin_clusters
       (epoch_id, source, cell_key, geom, listing_count, distinct_streets,
        distinct_obec_kods, nearest_admin_unit_id, distance_to_admin_centroid_m,
        declared_blur_share, classification, registry_version_id, policy_version)
VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s,
        %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (epoch_id, source, cell_key) DO NOTHING
"""

_PREVIOUS_CLUSTERS_SQL = """
SELECT source, cell_key, ST_Y(geom), ST_X(geom), listing_count, distinct_streets,
       distinct_obec_kods, classification, declared_blur_share,
       distance_to_admin_centroid_m
  FROM pin_clusters
 WHERE epoch_id = %s
 ORDER BY source, cell_key
"""

_PREVIOUS_MEMBERS_SQL = """
SELECT listing_id, source, ST_Y(geom), ST_X(geom)
  FROM listing_location_current
 WHERE geom IS NOT NULL
 ORDER BY listing_id
"""

_ENQUEUE_SQL = """
INSERT INTO dirty_locations (listing_id, reason)
VALUES (%s, 'collision_recompute')
ON CONFLICT (listing_id) DO NOTHING
"""

_UPDATE_EPOCH_COUNTS_SQL = """
UPDATE pin_cluster_epochs SET cluster_count = %s, reclassified_count = %s WHERE id = %s
"""


def run(
    conn: psycopg.Connection,
    *,
    sources: list[str] | None = None,
    policy_version: str = POLICY_VERSION_DEFAULT,
    dry_run: bool = False,
) -> int:
    registry_version_id, _ = resolve_db.current_registry_version(conn)
    policy = resolve_db.load_collision_policy(conn, policy_version)
    wanted = sources or []

    with conn.cursor() as cur:
        cur.execute(_PIN_ROWS_SQL, (wanted, wanted))
        rows = cur.fetchall()

    pins = [
        collision.PinRow(
            listing_id=int(r[0]), source=str(r[1]), lat=float(r[2]), lon=float(r[3]),
            street_key=normalize_match_key(r[4]) if r[4] else None,
            obec_kod=r[5], declared_blur=bool(r[6]), is_cz=bool(r[7]),
        )
        for r in rows
    ]
    pins = _with_centroid_distance(conn, pins, registry_version_id)
    clusters = collision.build_clusters(pins, policy)

    parent_epoch = resolve_db.current_epoch(conn)
    previous = _previous_clusters(conn, parent_epoch)
    changed = collision.changed_listings(previous, clusters, policy)
    reclassified = _reclassified_count(previous, clusters)

    LOG.info(
        "EPOCH computed clusters=%d changed_listings=%d reclassified=%d dry_run=%s",
        len(clusters), len(changed), reclassified, dry_run,
    )
    if dry_run:
        return 0

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                _INSERT_EPOCH_SQL,
                (policy_version, registry_version_id, sorted({c.source for c in clusters}),
                 len(clusters), reclassified, parent_epoch, None),
            )
            epoch_id = int(cur.fetchone()[0])
            for cluster in clusters:
                cur.execute(
                    _INSERT_CLUSTER_SQL,
                    (epoch_id, cluster.source, cluster.cell_key, cluster.lon, cluster.lat,
                     cluster.listing_count, cluster.distinct_streets,
                     cluster.distinct_obec_kods, cluster.nearest_admin_unit_id,
                     cluster.distance_to_admin_centroid_m, cluster.declared_blur_share,
                     cluster.classification, registry_version_id, policy_version),
                )
            for listing_id in changed:
                cur.execute(_ENQUEUE_SQL, (listing_id,))
            cur.execute(_UPDATE_EPOCH_COUNTS_SQL, (len(clusters), reclassified, epoch_id))
    LOG.info("EPOCH done id=%d clusters=%d enqueued=%d", epoch_id, len(clusters), len(changed))
    return epoch_id


def _with_centroid_distance(
    conn: psycopg.Connection, pins: list[collision.PinRow], registry_version_id: int
) -> list[collision.PinRow]:
    """Detector 3: distance to the obec's representative point ≈ 0 ⇒ centroid fallback.
    One query per (obec, cell) is wasteful, so it is resolved per DISTINCT obec+point."""
    out: list[collision.PinRow] = []
    cache: dict[tuple[int, str], tuple[float | None, int | None]] = {}
    with conn.cursor() as cur:
        for pin in pins:
            if pin.obec_kod is None:
                out.append(pin)
                continue
            key = (pin.obec_kod, collision.cell_of(pin.lat, pin.lon))
            if key not in cache:
                cur.execute(
                    _CENTROID_DISTANCE_SQL, (pin.lon, pin.lat, registry_version_id, pin.obec_kod)
                )
                row = cur.fetchone()
                cache[key] = (
                    (None, None) if row is None else (float(row[1]), int(row[2]))
                )
            distance, unit_id = cache[key]
            out.append(
                collision.PinRow(
                    listing_id=pin.listing_id, source=pin.source, lat=pin.lat, lon=pin.lon,
                    street_key=pin.street_key, obec_kod=pin.obec_kod,
                    declared_blur=pin.declared_blur, is_cz=pin.is_cz,
                    distance_to_admin_centroid_m=distance, nearest_admin_unit_id=unit_id,
                )
            )
    return out


def _previous_clusters(
    conn: psycopg.Connection, epoch_id: int | None
) -> list[collision.ClusterRow]:
    if epoch_id is None:
        return []
    with conn.cursor() as cur:
        cur.execute(_PREVIOUS_CLUSTERS_SQL, (epoch_id,))
        rows = cur.fetchall()
        cur.execute(_PREVIOUS_MEMBERS_SQL)
        members = cur.fetchall()
    by_cell: dict[tuple[str, str], list[int]] = {}
    for listing_id, source, lat, lon in members:
        if lat is None or lon is None:
            continue
        by_cell.setdefault((str(source), collision.cell_of(float(lat), float(lon))), []).append(
            int(listing_id)
        )
    return [
        collision.ClusterRow(
            source=str(r[0]), cell_key=str(r[1]), lat=float(r[2]), lon=float(r[3]),
            listing_count=int(r[4]), distinct_streets=int(r[5]), distinct_obec_kods=int(r[6]),
            classification=str(r[7]),
            declared_blur_share=float(r[8] or 0.0),
            distance_to_admin_centroid_m=None if r[9] is None else float(r[9]),
            n_25m=int(r[4]), n_100m=int(r[4]),
            listing_ids=tuple(sorted(by_cell.get((str(r[0]), str(r[1])), ()))),
        )
        for r in rows
    ]


def _reclassified_count(
    previous: list[collision.ClusterRow], current: list[collision.ClusterRow]
) -> int:
    before = {(c.source, c.cell_key): c.classification for c in previous}
    return sum(
        1
        for c in current
        if (c.source, c.cell_key) in before and before[(c.source, c.cell_key)] != c.classification
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mint a pin-collision epoch")
    parser.add_argument("--sources", default="", help="comma-separated; blank = every source")
    parser.add_argument("--policy-version", default=POLICY_VERSION_DEFAULT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    with db.connect() as conn:
        with lease.held(
            conn, JOB_NAME, cadence="1 day", concurrency_group=CONCURRENCY_GROUP
        ) as acquired:
            if not acquired:
                LOG.info("EPOCH skipped: another run holds the %s lease", JOB_NAME)
                return 0
            run(conn, sources=sources, policy_version=args.policy_version, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
