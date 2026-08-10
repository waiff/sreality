"""S6 — precision tier and uncertainty (03 §3.8). Four orthogonal axes, `unknown` is a
value, NULL is never permitted.

Order of operations, and it matters:

1. **Granularity comes from the RUNG REACHED** (§3.8.1) — "how well do we know the
   address", not "how good is the coordinate". A listing with a portal-declared-exact,
   uncollapsed pin and no street text resolves at `obec`, and that is the honest answer for
   a street filter or a Tier-0 dedup key.
2. **Then the declared cap is applied** (§3.8.2). Every portal precision signal is an UPPER
   BOUND, never a certification: mmreality's one `accurate: true` row in the corpus is
   wrong and its one correct row is `accurate: false`.
3. **Then the collision evidence caps it again** (§3.8.4), read from the STAMPED epoch —
   the corpus-wide input that makes `collision_epoch_id` the fifth version in the key.

`blur_evidence` is a separate axis from `position_source` and is populated on every write
path: the declared half from the portal flags, the detected half from the collision
analysis. Collapsing them into `portal_pin_blurred` would destroy the only labels
available for calibrating the detector.

`position_quality_class` (§3.8.6) is a DERIVED convenience carried ALONGSIDE the two
canonical booleans, never in place of them — `renderable_as_point` and `geo_blockable`
keep their granularity rung. Its two cuts are uncalibrated (03 OQ3), so they live in
`location_constants` and are reported per source rather than trusted.
"""

from __future__ import annotations

from collections.abc import Sequence

from location_data.resolver import uncertainty
from location_data.resolver.position import DeclaredPrecision
from location_data.resolver.types import (
    AdminAssignment,
    ClusterEvidence,
    CollisionPolicyRow,
    Position,
    Precision,
    ResolverContext,
)

# The declared-label ladder of 03 §3.8.2. A label the contract does not map is NOT a cap:
# inventing one is as wrong as ignoring one.
DECLARED_CAP: dict[str, str] = {
    "gps": "address_point",
    "address": "address_point",
    "exact": "address_point",
    "presna": "address_point",
    "rooftop": "building",
    "street": "street",
    "approximate": "street",
    "priblizna": "street",
    "estimated": "street",
    "ward": "cast_obce_or_quarter",
    "quarter": "cast_obce_or_quarter",
    "citypart": "cast_obce_or_quarter",
    "area": "cast_obce_or_quarter",
    "polygon": "cast_obce_or_quarter",
    "regional": "obec",
    "municipality": "obec",
    "obec": "obec",
}

PRECISE_SOURCES = frozenset({"registry_point", "portal_pin"})
APPROX_SOURCES = frozenset({"portal_pin", "portal_pin_blurred", "derived_geocode"})
AREA_CLASSES = frozenset(
    {"town_centroid_suspect", "parser_collapse_suspect", "foreign_resort_centroid"}
)
OK_CLASSES = frozenset({"normal", "building_1_to_many"})


def cluster_caps(
    cluster: ClusterEvidence | None, policy: CollisionPolicyRow
) -> tuple[str | None, bool]:
    """-> (granularity cap, detected_blur). §3.8.4 detectors 1 and 2."""
    if cluster is None or cluster.listing_count < 2:
        return None, False
    if cluster.listing_count >= policy.threshold_n and cluster.distinct_streets >= policy.min_distinct_streets:
        # bazos's 276-listing / 99-street Olomouc pin lands here: admin-centroid class.
        return "obec", True
    if cluster.distinct_streets <= 1:
        # n >= 2 with one address is a real building, not a town pin — capped at building,
        # NOT lower. This is the rule that keeps bezrealitky blockable.
        return "building", False
    return None, True


def assess(
    position: Position,
    admin: AdminAssignment,
    ctx: ResolverContext,
    *,
    source: str,
    declared: DeclaredPrecision,
    cluster: ClusterEvidence | None,
    match_components: dict[str, str] | None = None,
    containment_radius_m: float | None = None,
) -> Precision:
    rank = ctx.granularity_rank
    granularity = position.granularity
    caps: list[str] = []

    # ---- 2. declared cap.
    if declared.label:
        capped = DECLARED_CAP.get(declared.label)
        if capped is not None:
            caps.append(f"declared:{declared.label}->{capped}")
            granularity = rank.coarser_of(granularity, capped)
    if declared.blurred and not declared.label:
        caps.append("blur_hint->street")
        granularity = rank.coarser_of(granularity, "street")

    # ---- 3. collision cap, evaluated against the stamped epoch.
    policy = ctx.collision_threshold(source, admin.obec_kod)
    collision_cap, detected = cluster_caps(cluster, policy)
    if collision_cap is not None:
        caps.append(f"collision:{cluster.classification if cluster else 'none'}->{collision_cap}")
        granularity = rank.coarser_of(granularity, collision_cap)

    blur_evidence = _blur_axis(declared.blur_evidence, detected or _class_is_blur(cluster))

    # The COORDINATE's own grade is a different question from the ADDRESS granularity
    # (03 §3.8.1): a declared-`gps` pin with no street text resolves at `obec` and its
    # coordinate is still address-grade — 03 §3.16.2 asserts exactly that pair. The
    # declared label therefore sets the radius lookup's rung, but only while the collision
    # analysis has NOT detected a collapse: a detected collapse always beats a declaration
    # ("cap, never certify").
    position_granularity = granularity
    declared_floor = DECLARED_CAP.get(declared.label or "")
    if (
        collision_cap is None
        and declared_floor is not None
        and position.position_source in PRECISE_SOURCES
        and rank.rank(declared_floor) > rank.rank(granularity)
    ):
        position_granularity = declared_floor

    # ---- radius: policy for the (source, granularity) pair, then the declared override,
    # then the collision-derived bound — combined by MAX, never by mean.
    radius, semantics = uncertainty.radius_for(
        ctx.uncertainty_policy,
        position_source=position.position_source,
        granularity=position_granularity,
        source=source,
        declared_radius_m=declared.radius_m,
        containment_radius_m=containment_radius_m,
        input_radii_m=(position.uncertainty_radius_m,),
    )
    if collision_cap == "obec" and containment_radius_m is not None:
        radius, semantics = uncertainty.combine((radius, semantics), (containment_radius_m, "geometric_bound"))

    match_confidence = position.match_confidence
    if collision_cap is not None or (declared.blurred and position.position_source != "registry_point"):
        match_confidence = _cap_confidence(match_confidence, "medium")

    quality = position_quality_class(
        position_source=position.position_source,
        uncertainty_radius_m=radius,
        classification=(cluster.classification if cluster else "normal"),
        has_geom=position.lat is not None,
        constants=ctx.constants,
    )

    return Precision(
        granularity=granularity,
        position_source=position.position_source,
        match_confidence=match_confidence,
        blur_evidence=blur_evidence,
        uncertainty_radius_m=radius,
        radius_semantics=semantics,
        position_quality_class=quality,
        match_components=dict(sorted((match_components or {}).items())),
        collision=_collision_block(cluster, policy),
        declared_caps=tuple(caps),
    )


def position_quality_class(
    *,
    position_source: str,
    uncertainty_radius_m: float,
    classification: str,
    has_geom: bool,
    constants,
) -> str:
    """03 §3.8.6, evaluated in the order the design states it."""
    if position_source == "none" or not has_geom:
        return "none"
    if (
        position_source in PRECISE_SOURCES
        and uncertainty_radius_m <= constants.precise_r95_m
        and classification in OK_CLASSES
    ):
        return "precise"
    if position_source == "admin_centroid" or classification in AREA_CLASSES:
        return "area"
    if position_source in APPROX_SOURCES and uncertainty_radius_m <= constants.approx_r95_m:
        return "approximate"
    return "area"


def _blur_axis(declared: str, detected: bool) -> str:
    has_declared = declared in ("declared", "both")
    if has_declared and detected:
        return "both"
    if has_declared:
        return "declared"
    if detected:
        return "detected"
    return "none"


def _class_is_blur(cluster: ClusterEvidence | None) -> bool:
    return cluster is not None and cluster.classification in AREA_CLASSES


def _collision_block(cluster: ClusterEvidence | None, policy: CollisionPolicyRow) -> dict:
    if cluster is None:
        return {
            "n_exact": 1, "n_25m": 1, "n_100m": 1, "heterogeneity": 0,
            "cluster_id": None, "classification": "normal", "threshold_n": policy.threshold_n,
        }
    return {
        "n_exact": cluster.listing_count,
        "n_25m": cluster.n_25m,
        "n_100m": cluster.n_100m,
        "heterogeneity": cluster.heterogeneity,
        "cluster_id": cluster.cluster_id,
        "classification": cluster.classification,
        "threshold_n": policy.threshold_n,
    }


def _cap_confidence(value: str, ceiling: str) -> str:
    order = ("low", "medium", "high", "exact")
    current = value if value in order else "low"
    return current if order.index(current) <= order.index(ceiling) else ceiling


def declared_vs_assigned_conflict(
    declared: DeclaredPrecision, precision: Precision, rank
) -> bool:
    """§3.11.1 `declared_precision_vs_assigned`: the portal declares `municipality` and the
    resolution nevertheless claims `address_point`."""
    if not declared.label:
        return False
    capped = DECLARED_CAP.get(declared.label)
    if capped is None:
        return False
    return rank.rank(precision.granularity) > rank.rank(capped)


def caps_from_claims(labels: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({DECLARED_CAP[label] for label in labels if label in DECLARED_CAP}))
