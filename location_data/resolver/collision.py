"""The collision-epoch producer: `pin_clusters` + the six-value classification (03 §3.8.4,
00 §10, 01 §7.3). Pure — the psycopg job that feeds it rows is `epoch_job`.

Collision is the most reliable precision detector available and **decimal count is not
one**: bazos's uniform 5-6 decimals with 5.56 listings/point is high stated precision with
town-centroid reality. Nothing here reads a decimal count.

Because collision evidence is a function of every OTHER active listing of a source, a
recompute mints a NEW IMMUTABLE EPOCH rather than overwriting in place, and only listings
whose BUCKET changed are enqueued into `dirty_locations` — that is what makes
`collision_epoch_id` a real fifth version input instead of a stale cache.

**h3-pg is unavailable on this instance**, so the shipped key is the rounded 4-dp cell of
`location_geo_cell_key`, and the 3×3 NEIGHBOURHOOD EXPANSION is mandatory (a single-cell
exact-equality blocker silently loses matches, and a 6th-decimal-jittered centroid walks
straight past an exact test). The three radii the projection carries — exact / 25 m / 100 m
— are computed here from the expanded neighbourhood.

*Known design gap, flagged not worked around:* `location_collision_policy`'s PK is
`(policy_version, source, obec_key)`, so it cannot hold one row per radius even though it
carries a `radius_m` column and 00 §10.1 says the three projection radii "correspond to
`location_collision_policy.radius_m ∈ (0, 25, 100)`". The three radii are therefore computed
unconditionally from `pin_clusters` and the policy row is read only for `threshold_n`,
`min_distinct_streets` and `pin_collision_semantics`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from location_data.resolver.derived import geo_cell_key
from location_data.resolver.geo import haversine_m
from location_data.resolver.types import CollisionPolicyRow

# The three radii 00 §10.1 names. `0` is exact cell equality.
RADII_M: tuple[int, int, int] = (0, 25, 100)
CELL_DEGREES = 0.0001  # the 4-dp cell the fallback key rounds to

# UNCALIBRATED (03 OQ1): "distance to the obec's centroid ≈ 0 ⇒ centroid fallback" has no
# number attached anywhere in the corpus. Kept as a named module constant rather than
# smuggled into a policy column that has no room for it.
TOWN_CENTROID_EPS_M = 150.0
FOREIGN_SHARE_FOR_RESORT = 0.5

CLASSIFICATIONS = (
    "normal",
    "legitimate_multiunit",
    "building_1_to_many",
    "town_centroid_suspect",
    "parser_collapse_suspect",
    "foreign_resort_centroid",
)


@dataclass(frozen=True, slots=True)
class PinRow:
    """One active, geocoded listing as the epoch producer sees it."""

    listing_id: int
    source: str
    lat: float
    lon: float
    street_key: str | None = None
    obec_kod: int | None = None
    declared_blur: bool = False
    is_cz: bool = True
    distance_to_admin_centroid_m: float | None = None
    nearest_admin_unit_id: int | None = None


@dataclass(frozen=True, slots=True)
class ClusterRow:
    source: str
    cell_key: str
    lat: float
    lon: float
    listing_count: int
    distinct_streets: int
    distinct_obec_kods: int
    classification: str
    declared_blur_share: float
    distance_to_admin_centroid_m: float | None
    nearest_admin_unit_id: int | None
    n_25m: int
    n_100m: int
    listing_ids: tuple[int, ...] = ()

    @property
    def heterogeneity_ok(self) -> bool:
        return self.distinct_streets <= 1

    def bucket(self, threshold_n: int) -> tuple[str, bool, bool]:
        """What a listing's re-resolution actually depends on: the class, the heterogeneity
        band, and which side of the threshold the count is on (03 §3.8.4)."""
        return (self.classification, self.heterogeneity_ok, self.listing_count <= threshold_n)


def cell_of(lat: float, lon: float) -> str:
    key = geo_cell_key(lat, lon)
    assert key is not None  # lat/lon are non-null by construction here
    return key


def neighbourhood(cell_key: str) -> tuple[str, ...]:
    """The mandatory 3×3 expansion around a rounded cell."""
    _, lat_text, lon_text = cell_key.split(":")
    lat, lon = float(lat_text), float(lon_text)
    cells = []
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            cells.append(cell_of(lat + dlat * CELL_DEGREES, lon + dlon * CELL_DEGREES))
    return tuple(sorted(set(cells)))


def build_clusters(
    rows: Iterable[PinRow], policy: Sequence[CollisionPolicyRow]
) -> list[ClusterRow]:
    """Group per (source, cell) — clusters are PER-PORTAL because portals collapse
    differently — and classify each one."""
    by_cell: dict[tuple[str, str], list[PinRow]] = {}
    for row in rows:
        by_cell.setdefault((row.source, cell_of(row.lat, row.lon)), []).append(row)

    clusters: list[ClusterRow] = []
    for (source, cell_key), members in sorted(by_cell.items()):
        members = sorted(members, key=lambda r: r.listing_id)
        lat = sum(m.lat for m in members) / len(members)
        lon = sum(m.lon for m in members) / len(members)
        neighbours = [
            m
            for cell in neighbourhood(cell_key)
            for m in by_cell.get((source, cell), ())
        ]
        n_25 = sum(1 for m in neighbours if haversine_m(lat, lon, m.lat, m.lon) <= 25.0)
        n_100 = sum(1 for m in neighbours if haversine_m(lat, lon, m.lat, m.lon) <= 100.0)
        streets = {m.street_key for m in members if m.street_key}
        obec_kods = {m.obec_kod for m in members if m.obec_kod is not None}
        centroid_distances = [
            m.distance_to_admin_centroid_m
            for m in members
            if m.distance_to_admin_centroid_m is not None
        ]
        row = ClusterRow(
            source=source,
            cell_key=cell_key,
            lat=lat,
            lon=lon,
            listing_count=len(members),
            distinct_streets=len(streets),
            distinct_obec_kods=len(obec_kods),
            classification="normal",
            declared_blur_share=sum(1 for m in members if m.declared_blur) / len(members),
            distance_to_admin_centroid_m=(
                min(centroid_distances) if centroid_distances else None
            ),
            nearest_admin_unit_id=next(
                (m.nearest_admin_unit_id for m in members if m.nearest_admin_unit_id), None
            ),
            n_25m=max(n_25, len(members)),
            n_100m=max(n_100, len(members)),
            listing_ids=tuple(m.listing_id for m in members),
        )
        foreign_share = sum(1 for m in members if not m.is_cz) / len(members)
        clusters.append(
            _reclassify(row, classify(row, _policy_for(policy, source, members), foreign_share))
        )
    return clusters


def classify(cluster: ClusterRow, policy: CollisionPolicyRow, foreign_share: float) -> str:
    """The six-value vocabulary of 00 §10.2, in precedence order.

    `normal` is the "everything is fine" value and an unclustered listing IS `normal` —
    the class is never NULL, which is exactly why the retired gate that tested that column
    for NULL could never fire.
    """
    if cluster.listing_count < 2:
        return "normal"
    if foreign_share >= FOREIGN_SHARE_FOR_RESORT:
        # Foreign pins are heavily collapsed by construction: one pin per resort/town
        # (740 listings on one Spanish point).
        return "foreign_resort_centroid"
    heterogeneous = cluster.distinct_streets >= policy.min_distinct_streets
    over_threshold = cluster.listing_count > policy.threshold_n
    if over_threshold and heterogeneous:
        near_centroid = (
            cluster.distance_to_admin_centroid_m is not None
            and cluster.distance_to_admin_centroid_m <= TOWN_CENTROID_EPS_M
        )
        return "town_centroid_suspect" if near_centroid else "parser_collapse_suspect"
    if policy.pin_collision_semantics == "legitimate_multiunit" and not over_threshold:
        return "legitimate_multiunit"
    if cluster.distinct_streets <= 1:
        # One address, many listings: a real building, capped at `building` and NOT lower.
        return "building_1_to_many"
    return "normal"


def changed_listings(
    previous: Sequence[ClusterRow],
    current: Sequence[ClusterRow],
    policy: Sequence[CollisionPolicyRow],
) -> list[int]:
    """Only listings whose BUCKET changed are re-resolved (03 §3.8.4). A recompute that
    reclassifies nothing still mints an epoch row but enqueues nobody."""
    before = {
        (c.source, c.cell_key): c.bucket(_threshold(policy, c.source)) for c in previous
    }
    listings: set[int] = set()
    for cluster in current:
        key = (cluster.source, cluster.cell_key)
        if before.get(key) != cluster.bucket(_threshold(policy, cluster.source)):
            listings.update(cluster.listing_ids)
    gone = set(before) - {(c.source, c.cell_key) for c in current}
    for cluster in previous:
        if (cluster.source, cluster.cell_key) in gone:
            listings.update(cluster.listing_ids)
    return sorted(listings)


def _policy_for(
    policy: Sequence[CollisionPolicyRow], source: str, members: Sequence[PinRow]
) -> CollisionPolicyRow:
    obec_kods = {m.obec_kod for m in members if m.obec_kod is not None}
    obec_kod = next(iter(sorted(obec_kods))) if len(obec_kods) == 1 else None
    best: CollisionPolicyRow | None = None
    best_score = -1
    for row in policy:
        if row.source not in ("*", source):
            continue
        if row.obec_kod is not None and row.obec_kod != obec_kod:
            continue
        score = (2 if row.source == source else 0) + (1 if row.obec_kod is not None else 0)
        if score > best_score:
            best, best_score = row, score
    if best is None:
        raise LookupError("location_collision_policy has no ('*', NULL) fallback row")
    return best


def _threshold(policy: Sequence[CollisionPolicyRow], source: str) -> int:
    rows = [r for r in policy if r.source in ("*", source)]
    if not rows:
        raise LookupError("location_collision_policy has no ('*', NULL) fallback row")
    return max(rows, key=lambda r: (r.source == source, r.obec_kod is None)).threshold_n


def _reclassify(cluster: ClusterRow, classification: str) -> ClusterRow:
    return ClusterRow(
        source=cluster.source, cell_key=cluster.cell_key, lat=cluster.lat, lon=cluster.lon,
        listing_count=cluster.listing_count, distinct_streets=cluster.distinct_streets,
        distinct_obec_kods=cluster.distinct_obec_kods, classification=classification,
        declared_blur_share=cluster.declared_blur_share,
        distance_to_admin_centroid_m=cluster.distance_to_admin_centroid_m,
        nearest_admin_unit_id=cluster.nearest_admin_unit_id, n_25m=cluster.n_25m,
        n_100m=cluster.n_100m, listing_ids=cluster.listing_ids,
    )
