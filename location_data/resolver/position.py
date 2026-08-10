"""S4 — position assignment (03 §3.6).

Precedence (§3.6.1): `registry_point` > `portal_pin` > `portal_pin_blurred` >
`admin_centroid` > `carried_forward` > `none`. `derived_geocode` has no producer in the
free tier and ships unused, so enabling Mapbox-permanent later is a data change.

Three rules that are not negotiable:

* **The registry-vs-pin cross-check FLAGS, it never silently picks** (§3.6.2). Beyond
  `location_constants.registry_pin_conflict_m` (300 m) the resolver keeps the registry
  point as the default position, caps `match_confidence` at `medium` and emits a
  contradiction signal scaled by distance. The number is read from the constants row.
* **The losing position is PERSISTED as a `location_resolution_candidates` row** with its
  own geom, its own four axes and `distance_to_pin_m` on both rows. There is no
  `place_position` table (00 §11.2) and none is being asked for; the candidate row is what
  makes the cross-check reproducible from stored data.
* **Reverse resolution (coordinate → street) is DERIVED, never a claim** (§3.6.3). This
  module therefore never invents a street from a pin; ~11 of ~21 text-checkable
  resolver-derived streets in the corpus are wrong, two of them fully fabricated.

A coordinate whose claim carries `licence_class='ephemeral_display_only'` (the Mapy.cz
class) is stored as a candidate and can never win: `loc_res_licence` and `llc_licence`
make that structural, and this module refuses it before the constraint has to.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from location_data.resolver import uncertainty
from location_data.resolver.geo import distance_between
from location_data.resolver.types import (
    Candidate,
    CandidateSet,
    Claim,
    ContradictionSignal,
    Position,
    ResolverContext,
)

EPHEMERAL = "ephemeral_display_only"

# Portal-declared labels that mean "this pin is not address-grade" (03 §3.8.2). The portal
# contract maps its own vocabulary onto these; a label we do not know is NOT treated as
# blurred (cap, never certify — and never invent a cap either).
BLURRED_DECLARED_LABELS = frozenset(
    {
        "municipality", "obec", "ward", "quarter", "citypart", "street",
        "approximate", "priblizna", "estimated", "regional", "area", "polygon",
    }
)
PRECISE_DECLARED_LABELS = frozenset({"gps", "address", "exact", "presna", "rooftop", "ruian"})


@dataclass(frozen=True, slots=True)
class DeclaredPrecision:
    label: str | None
    blurred: bool
    radius_m: float | None
    blur_evidence: str
    claim_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PositionOutcome:
    position: Position
    candidates: tuple[Candidate, ...]
    chosen_rank: int | None
    chosen_rule: str
    signals: tuple[ContradictionSignal, ...]
    declared: DeclaredPrecision


def read_declared_precision(claims: Sequence[Claim]) -> DeclaredPrecision:
    label: str | None = None
    radius: float | None = None
    blurred = False
    declared_seen = False
    ids: list[int] = []
    for claim in sorted(claims, key=lambda c: c.id):
        if claim.claim_type not in (
            "precision_declaration", "blur_hint", "uncertainty_geometry", "map_zoom", "coordinate"
        ):
            continue
        if claim.claim_type == "blur_hint":
            blurred, declared_seen = True, True
            ids.append(claim.id)
            continue
        raw = (claim.declared_precision_label or claim.value_text or "").strip().lower()
        # Several portals hang the precision flag on the COORDINATE claim itself
        # (sreality `locality.inaccuracy_type`, mmreality `accurate`), not on a separate
        # precision_declaration row.
        if claim.claim_type == "coordinate" and claim.declared_precision_label:
            declared_seen = True
            ids.append(claim.id)
            label = label or raw
            if raw in BLURRED_DECLARED_LABELS:
                blurred = True
        if claim.claim_type in ("precision_declaration", "uncertainty_geometry"):
            declared_seen = True
            ids.append(claim.id)
            if raw:
                label = label or raw
                if raw in BLURRED_DECLARED_LABELS:
                    blurred = True
            if claim.declared_radius_m is not None:
                radius = claim.declared_radius_m if radius is None else max(radius, claim.declared_radius_m)
        if claim.blur_evidence in ("declared", "both"):
            blurred, declared_seen = True, True
            ids.append(claim.id)
    return DeclaredPrecision(
        label=label,
        blurred=blurred,
        radius_m=radius,
        blur_evidence="declared" if declared_seen and blurred else "none",
        claim_ids=tuple(sorted(set(ids))),
    )


def assign(
    claims: Sequence[Claim],
    candidate_set: CandidateSet,
    ctx: ResolverContext,
    *,
    source: str,
) -> PositionOutcome:
    constants = ctx.constants
    rank = ctx.granularity_rank
    declared = read_declared_precision(claims)
    extra: list[Candidate] = []
    signals: list[ContradictionSignal] = []

    pin_claim = next(
        (
            c
            for c in sorted(claims, key=lambda c: c.id)
            if c.claim_type == "coordinate" and c.has_position
        ),
        None,
    )
    pin = (pin_claim.lat, pin_claim.lon) if pin_claim else None
    pin_licence = pin_claim.licence_class if pin_claim else "portal"
    pin_admissible = pin_claim is not None and pin_licence != EPHEMERAL

    registry_candidate = next(
        (
            c
            for c in candidate_set.candidates
            if c.position_source == "registry_point"
            and c.lat is not None
            and rank.at_least(c.granularity, "building")
        ),
        None,
    )

    # ---- 1. registry point.
    if registry_candidate is not None:
        distance = distance_between((registry_candidate.lat, registry_candidate.lon), pin)  # type: ignore[arg-type]
        confidence = registry_candidate.match_confidence
        if distance is not None and distance > constants.registry_pin_conflict_m:
            confidence = _cap(confidence, "medium")
            signals.append(
                ContradictionSignal(
                    rule="pin_registry_distance",
                    field="coordinate",
                    severity="major" if distance > 3 * constants.registry_pin_conflict_m else "minor",
                    stored={"lat": registry_candidate.lat, "lon": registry_candidate.lon},
                    claimed={"lat": pin[0], "lon": pin[1]} if pin else None,
                    distance_m=distance,
                    evidence_claim_ids=((pin_claim.id,) if pin_claim else ()),
                    auto_action="downgraded_precision",
                )
            )
        if pin_admissible and pin is not None and pin_claim is not None:
            extra.append(
                _pin_candidate(
                    pin_claim, ctx, source=source, declared=declared, distance_to_pin_m=0.0,
                    rejected_reason="lost_to_registry_point",
                )
            )
        radius, semantics = uncertainty.radius_for(
            ctx.uncertainty_policy,
            position_source="registry_point",
            granularity=registry_candidate.granularity,
            source=source,
        )
        position = Position(
            lat=registry_candidate.lat, lon=registry_candidate.lon,
            position_source="registry_point", blur_evidence=declared.blur_evidence,
            licence_class="cc_by_ruian", granularity=registry_candidate.granularity,
            match_confidence=confidence, uncertainty_radius_m=radius,
            radius_semantics=semantics, winner_candidate_rank=registry_candidate.rank,
            source_claim_ids=registry_candidate.source_claim_ids,
            cross_check={"registry_pin_distance_m": distance} if distance is not None else {},
        )
        candidates = _merge(candidate_set.candidates, extra, distance, registry_candidate.rank)
        return PositionOutcome(position, candidates, registry_candidate.rank,
                               "registry_point_wins", tuple(signals), declared)

    # ---- 2/3. portal pin (address-grade) / portal pin (declared blurred).
    if pin_admissible and pin_claim is not None and pin is not None:
        blurred = declared.blurred
        position_source = "portal_pin_blurred" if blurred else "portal_pin"
        granularity = _pin_granularity(candidate_set, rank)
        radius, semantics = uncertainty.radius_for(
            ctx.uncertainty_policy,
            position_source=position_source,
            granularity=granularity,
            source=source,
            declared_radius_m=declared.radius_m,
        )
        pin_candidate = _pin_candidate(
            pin_claim, ctx, source=source, declared=declared, distance_to_pin_m=0.0,
            rejected_reason=None, granularity=granularity,
        )
        extra.append(pin_candidate)
        candidates = _merge(candidate_set.candidates, extra, None, None)
        winner = next(c for c in candidates if c.target_kind == "coordinate_only")
        position = Position(
            lat=pin[0], lon=pin[1], position_source=position_source,
            blur_evidence=declared.blur_evidence, licence_class=pin_licence,
            granularity=granularity,
            match_confidence=_cap(pin_claim.claim_confidence or "medium", "high"),
            uncertainty_radius_m=radius, radius_semantics=semantics,
            winner_candidate_rank=winner.rank, source_claim_ids=(pin_claim.id,),
        )
        return PositionOutcome(position, candidates, winner.rank,
                               "portal_pin_blurred" if blurred else "portal_pin",
                               tuple(signals), declared)

    if pin_claim is not None and not pin_admissible:
        # Stored, never a winner (00 §6.1 artifact 2/3).
        extra.append(
            _pin_candidate(
                pin_claim, ctx, source=source, declared=declared, distance_to_pin_m=0.0,
                rejected_reason="licence_ephemeral_inadmissible",
            )
        )

    # ---- 4. admin centroid.
    centroid = next(
        (c for c in candidate_set.candidates if c.position_source == "admin_centroid" and c.lat is not None),
        None,
    )
    if centroid is not None:
        candidates = _merge(candidate_set.candidates, extra, None, centroid.rank)
        position = Position(
            lat=centroid.lat, lon=centroid.lon, position_source="admin_centroid",
            blur_evidence=declared.blur_evidence, licence_class="cc_by_ruian",
            granularity=centroid.granularity, match_confidence=centroid.match_confidence,
            uncertainty_radius_m=centroid.uncertainty_radius_m,
            radius_semantics=centroid.radius_semantics, winner_candidate_rank=centroid.rank,
            source_claim_ids=centroid.source_claim_ids,
        )
        return PositionOutcome(position, candidates, centroid.rank, "admin_centroid",
                               tuple(signals), declared)

    # ---- 5. carried forward: the previous resolution's position, unchanged, with its own
    # bound taken as the max of its inputs (never a fresh constant).
    if ctx.previous_position is not None and ctx.previous_position.lat is not None:
        prev = ctx.previous_position
        radius, semantics = uncertainty.radius_for(
            ctx.uncertainty_policy, position_source="carried_forward",
            granularity=prev.granularity, source=source,
            input_radii_m=(prev.uncertainty_radius_m,),
        )
        candidates = _merge(candidate_set.candidates, extra, None, None)
        position = Position(
            lat=prev.lat, lon=prev.lon, position_source="carried_forward",
            blur_evidence=prev.blur_evidence, licence_class=prev.licence_class,
            granularity=prev.granularity, match_confidence=_cap(prev.match_confidence, "medium"),
            uncertainty_radius_m=radius, radius_semantics=semantics,
            source_claim_ids=prev.source_claim_ids,
        )
        return PositionOutcome(position, candidates, None, "carried_forward", tuple(signals), declared)

    # ---- 6. none. A legal, storable state — with the coarsest defensible bound, because a
    # NULL radius falls out of every containment filter instead of being badged.
    radius, semantics = uncertainty.radius_for(
        ctx.uncertainty_policy, position_source="none", granularity="unknown", source=source
    )
    candidates = _merge(candidate_set.candidates, extra, None, None)
    top = candidates[0] if candidates else None
    position = Position(
        lat=None, lon=None, position_source="none", blur_evidence=declared.blur_evidence,
        licence_class="portal", granularity=top.granularity if top else "unknown",
        match_confidence=top.match_confidence if top else "low",
        uncertainty_radius_m=radius, radius_semantics=semantics,
    )
    return PositionOutcome(position, candidates, top.rank if top else None, "no_position",
                           tuple(signals), declared)


def _pin_granularity(candidate_set: CandidateSet, rank) -> str:
    """A pin does not improve address identity: granularity still comes from the rung
    REACHED (§3.8.1). With no candidate at all the pin is an `unknown`-identity point."""
    if not candidate_set.candidates:
        return "unknown"
    return candidate_set.candidates[0].granularity


def _pin_candidate(
    pin_claim: Claim,
    ctx: ResolverContext,
    *,
    source: str,
    declared: DeclaredPrecision,
    distance_to_pin_m: float | None,
    rejected_reason: str | None,
    granularity: str = "unknown",
) -> Candidate:
    position_source = "portal_pin_blurred" if declared.blurred else "portal_pin"
    radius, semantics = uncertainty.radius_for(
        ctx.uncertainty_policy, position_source=position_source, granularity=granularity,
        source=source, declared_radius_m=declared.radius_m,
    )
    return Candidate(
        rung="S4", rank=0, score=0.0, target_kind="coordinate_only", granularity=granularity,
        position_source=position_source,
        match_confidence=pin_claim.claim_confidence or "medium",
        uncertainty_radius_m=radius, radius_semantics=semantics,
        licence_class=pin_claim.licence_class, blur_evidence=declared.blur_evidence,
        lat=pin_claim.lat, lon=pin_claim.lon, distance_to_pin_m=distance_to_pin_m,
        rejected_reason=rejected_reason, source_claim_ids=(pin_claim.id,),
        component_match={},
    )


def _merge(
    base: Sequence[Candidate],
    extra: Sequence[Candidate],
    registry_pin_distance_m: float | None,
    registry_rank: int | None,
) -> tuple[Candidate, ...]:
    """Re-rank the union so `rank` stays dense and unique (the table's UNIQUE
    (resolution_id, rank)), and stamp `distance_to_pin_m` on the registry row too."""
    out: list[Candidate] = []
    next_rank = len(base)
    for candidate in base:
        distance = (
            registry_pin_distance_m
            if registry_rank is not None and candidate.rank == registry_rank
            else candidate.distance_to_pin_m
        )
        out.append(_replace(candidate, rank=candidate.rank, distance_to_pin_m=distance))
    for candidate in extra:
        next_rank += 1
        out.append(_replace(candidate, rank=next_rank))
    return tuple(out)


def _replace(candidate: Candidate, **changes) -> Candidate:
    values = {
        "rung": candidate.rung, "rank": candidate.rank, "score": candidate.score,
        "target_kind": candidate.target_kind, "granularity": candidate.granularity,
        "position_source": candidate.position_source,
        "match_confidence": candidate.match_confidence,
        "uncertainty_radius_m": candidate.uncertainty_radius_m,
        "radius_semantics": candidate.radius_semantics, "licence_class": candidate.licence_class,
        "blur_evidence": candidate.blur_evidence, "lat": candidate.lat, "lon": candidate.lon,
        "ruian_adm_kod": candidate.ruian_adm_kod,
        "stavebni_objekt_kod": candidate.stavebni_objekt_kod,
        "parcela_id": candidate.parcela_id, "ulice_kod": candidate.ulice_kod,
        "ulice_id": candidate.ulice_id,
        "admin_unit_id": candidate.admin_unit_id, "component_match": candidate.component_match,
        "distance_to_pin_m": candidate.distance_to_pin_m,
        "rejected_reason": candidate.rejected_reason,
        "source_claim_ids": candidate.source_claim_ids, "relaxations": candidate.relaxations,
    }
    values.update(changes)
    return Candidate(**values)


def _cap(value: str | None, ceiling: str) -> str:
    order = ("low", "medium", "high", "exact")
    current = value if value in order else "low"
    return current if order.index(current) <= order.index(ceiling) else ceiling
