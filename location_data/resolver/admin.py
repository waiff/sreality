"""S5 — admin hierarchy assignment (03 §3.7).

**Registry-first.** When S3 matched an address point, street or obec entity, the hierarchy
comes from the RÚIAN chain (`adresni-mista-vazby-cr.csv` pre-joins all 3 020 222 address
points), not from geometry: one join, authoritative, `admin_assignment_method='registry'`.

PIP is the FALLBACK, for coordinate-only rows, and every one of its outcomes is a POSITIVE
status rather than a silent NULL — `obec_id IS NULL` currently starves four subsystems at
once on ~18.3k rows. The sliver tolerance is the one named constant
(`location_constants.pip_sliver_tolerance_m`, 250 m — the value migration 289 already
chose), read from the row and never re-spelled here.

Two rules earn their own code paths:

* **PIP inherits the pin** (§3.7.3 rule 1): `admin_position_source` is the pin's own
  source, so a bad pin cannot silently relocate a listing.
* **A validated locality claim can BEAT the pin** (§3.7.3 rule 2): when the pin's
  uncertainty exceeds the distance to the nearest boundary, the claimed locality wins and
  the row records `admin_assignment_method='claimed'`. An obfuscated pin routinely crosses
  an obec boundary, and on bazos the two answers differ on 57.0 % of rows — which is also
  why `postal_town` stays a separate field and that disagreement is never an error.

**The `část obce` gap** (§3.7.4): ČástObce has NO polygon in RÚIAN — only a definition
point. Membership is therefore a CODE PREDICATE over the address-point set
(`cast_obce_for_point`), never a polygon test and never a faked hull.
"""

from __future__ import annotations

from dataclasses import dataclass

from location_data.resolver.types import (
    AdminAssignment,
    AdminUnit,
    Candidate,
    CandidateSet,
    Position,
    RegistryView,
    ResolverContext,
)

# Rungs that matched an ADDRESS-grade entity: the chain is the registry's own answer and
# there is nothing for a pin to arbitrate.
ADDRESS_RUNGS = ("R0", "R1", "R2", "R3", "R5")
# Rungs that matched only a NAME. With a pin present these two answers can genuinely
# disagree, and §3.7.3 rule 2 decides between them — that is where `claimed` comes from.
NAME_RUNGS = ("R4", "R6")

_LEVEL_FIELDS = {
    "obec": ("obec_kod", "obec_unit_id", "obec_name"),
    "cast_obce": ("cast_obce_kod", "cast_obce_unit_id", "cast_obce_name"),
    "momc": ("momc_kod", None, None),
    "katastralni_uzemi": ("ku_kod", None, None),
    "pou": ("pou_kod", None, None),
    "orp": ("orp_kod", None, None),
    "okres": ("okres_kod", "okres_unit_id", "okres_name"),
    "kraj": ("kraj_kod", "kraj_unit_id", "kraj_name"),
}


@dataclass(frozen=True, slots=True)
class ClaimedLocality:
    unit: AdminUnit | None
    claim_ids: tuple[int, ...] = ()


def assign(
    candidate_set: CandidateSet,
    position: Position,
    ctx: ResolverContext,
    *,
    country_status: str,
    claimed: ClaimedLocality | None = None,
    winner_rank: int | None = None,
) -> AdminAssignment:
    registry = ctx.registry
    constants = ctx.constants
    winner = _winner(candidate_set, winner_rank)

    # ---- registry-first (address-grade entities, and name matches with no pin to weigh).
    name_only = winner is not None and winner.rung in NAME_RUNGS
    if winner is not None and (
        winner.rung in ADDRESS_RUNGS or (name_only and position.lat is None)
    ):
        levels = _levels_for_candidate(winner, registry, position)
        if levels:
            assignment = _from_levels(
                levels,
                method="registry",
                admin_position_source=(
                    "registry_point" if winner.position_source == "registry_point"
                    else position.position_source
                ),
            )
            return _with_boundary_distance(assignment, position, registry)

    if position.lat is None or position.lon is None:
        return AdminAssignment(method="unresolved", position_source="none")

    # A name match plus a pin: the claim is a real candidate for the hierarchy, so it is
    # what §3.7.3 rule 2 weighs against PIP below.
    if name_only and (claimed is None or claimed.unit is None) and winner is not None:
        unit = (
            registry.admin_unit(winner.admin_unit_id)
            if winner.admin_unit_id is not None
            else None
        )
        if unit is not None:
            claimed = ClaimedLocality(unit, winner.source_claim_ids)

    if country_status == "foreign":
        return AdminAssignment(method="outside_country", position_source=position.position_source)

    # ---- PIP.
    covering = registry.containing_obec(position.lat, position.lon)
    if covering is not None:
        boundary_m = registry.distance_to_admin_boundary_m(
            covering.unit_id, position.lat, position.lon
        )
        # §3.7.3 rule 2: an uncertain pin does not get to overrule a validated claim.
        if (
            claimed is not None
            and claimed.unit is not None
            and claimed.unit.code != covering.code
            and boundary_m is not None
            and position.uncertainty_radius_m > boundary_m
        ):
            assignment = _from_levels(
                _chain_levels(claimed.unit, registry),
                method="claimed",
                admin_position_source=position.position_source,
            )
            return _with_boundary_distance(assignment, position, registry)
        agrees = claimed is not None and claimed.unit is not None and claimed.unit.code == covering.code
        assignment = _from_levels(
            _chain_levels(covering, registry),
            # The claim and the pin agree: the registry entity IS the answer, and the pin
            # merely corroborates it.
            method="registry" if agrees else "pip_containment",
            admin_position_source=position.position_source,
        )
        assignment = _with_cast_obce(assignment, position, registry)
        return _with_boundary_distance(assignment, position, registry, precomputed=boundary_m)

    nearest = registry.nearest_obec_within(
        position.lat, position.lon, constants.pip_sliver_tolerance_m
    )
    if nearest is not None:
        unit, distance = nearest
        assignment = _from_levels(
            _chain_levels(unit, registry),
            method="pip_nearest_within_n_m",
            admin_position_source=position.position_source,
        )
        assignment = _replace(assignment, sliver_distance_m=distance)
        return _with_boundary_distance(assignment, position, registry)

    in_cz = registry.in_czechia_polygon(position.lat, position.lon)
    if in_cz is True or (in_cz is None and constants.in_bbox(position.lat, position.lon)):
        # Inside CZ, no obec at any tolerance: keep the coordinate, name the state.
        return AdminAssignment(
            method="unresolved_sliver", position_source=position.position_source
        )
    if in_cz is False:
        return AdminAssignment(method="outside_country", position_source=position.position_source)
    return AdminAssignment(method="unresolved", position_source=position.position_source)


def _winner(candidate_set: CandidateSet, winner_rank: int | None) -> Candidate | None:
    if winner_rank is not None:
        for candidate in candidate_set.candidates:
            if candidate.rank == winner_rank and candidate.target_kind != "coordinate_only":
                return candidate
    for candidate in candidate_set.candidates:
        if candidate.target_kind != "coordinate_only" and candidate.rejected_reason is None:
            return candidate
    return None


def _levels_for_candidate(
    candidate: Candidate, registry: RegistryView, position: Position
) -> dict[str, AdminUnit]:
    if candidate.ruian_adm_kod is not None:
        point = registry.address_point(candidate.ruian_adm_kod)
        if point is not None:
            levels: dict[str, AdminUnit] = {}
            obec = registry.admin_unit(point.obec_unit_id)
            if obec is not None:
                levels.update(_chain_levels(obec, registry))
            if point.cast_obce_unit_id is not None:
                unit = registry.admin_unit(point.cast_obce_unit_id)
                if unit is not None:
                    levels["cast_obce"] = unit
            if point.momc_unit_id is not None:
                unit = registry.admin_unit(point.momc_unit_id)
                if unit is not None:
                    levels["momc"] = unit
            return levels
    if candidate.admin_unit_id is not None:
        unit = registry.admin_unit(candidate.admin_unit_id)
        if unit is not None:
            return _chain_levels(unit, registry)
    return {}


def _chain_levels(unit: AdminUnit, registry: RegistryView) -> dict[str, AdminUnit]:
    levels: dict[str, AdminUnit] = {unit.level: unit}
    for ancestor in registry.admin_chain(unit.unit_id):
        levels.setdefault(ancestor.level, ancestor)
    return levels


def _from_levels(
    levels: dict[str, AdminUnit], *, method: str, admin_position_source: str
) -> AdminAssignment:
    values: dict[str, object] = {"method": method, "position_source": admin_position_source}
    for level, unit in sorted(levels.items()):
        mapping = _LEVEL_FIELDS.get(level)
        if mapping is None:
            continue
        kod_field, unit_field, name_field = mapping
        values[kod_field] = unit.code
        if unit_field:
            values[unit_field] = unit.unit_id
        if name_field:
            values[name_field] = unit.name
    anchor = levels.get("obec") or next(iter(sorted(levels.values(), key=lambda u: u.level)), None)
    if anchor is not None:
        values["admin_path"] = anchor.path
        values["display_path"] = anchor.display_path
    return AdminAssignment(**values)  # type: ignore[arg-type]


def _with_cast_obce(
    assignment: AdminAssignment, position: Position, registry: RegistryView
) -> AdminAssignment:
    """The část-obce membership predicate: a CODE lookup over the address-point set. There
    is no ČástObce polygon in RÚIAN, so there is nothing to run `ST_Covers` against."""
    if assignment.cast_obce_kod is not None or position.lat is None or position.lon is None:
        return assignment
    unit = registry.cast_obce_for_point(position.lat, position.lon)
    if unit is None:
        return assignment
    return _replace(
        assignment,
        cast_obce_kod=unit.code,
        cast_obce_unit_id=unit.unit_id,
        cast_obce_name=unit.name,
    )


def _with_boundary_distance(
    assignment: AdminAssignment,
    position: Position,
    registry: RegistryView,
    *,
    precomputed: float | None = None,
) -> AdminAssignment:
    """`distance_to_nearest_boundary_m` (00 §7.1, REQUIRED): the membership verdict is a
    scalar comparison against this, or it degrades to per-row geometry in the hot path."""
    if precomputed is not None:
        return _replace(assignment, distance_to_nearest_boundary_m=precomputed)
    if assignment.obec_unit_id is None or position.lat is None or position.lon is None:
        return assignment
    distance = registry.distance_to_admin_boundary_m(
        assignment.obec_unit_id, position.lat, position.lon
    )
    if distance is None:
        return assignment
    return _replace(assignment, distance_to_nearest_boundary_m=distance)


def _replace(assignment: AdminAssignment, **changes) -> AdminAssignment:
    values = {
        "method": assignment.method,
        "position_source": assignment.position_source,
        "sliver_distance_m": assignment.sliver_distance_m,
        "distance_to_nearest_boundary_m": assignment.distance_to_nearest_boundary_m,
        "obec_kod": assignment.obec_kod, "obec_unit_id": assignment.obec_unit_id,
        "obec_name": assignment.obec_name, "cast_obce_kod": assignment.cast_obce_kod,
        "cast_obce_unit_id": assignment.cast_obce_unit_id,
        "cast_obce_name": assignment.cast_obce_name, "momc_kod": assignment.momc_kod,
        "ku_kod": assignment.ku_kod, "pou_kod": assignment.pou_kod,
        "orp_kod": assignment.orp_kod, "okres_kod": assignment.okres_kod,
        "okres_unit_id": assignment.okres_unit_id, "okres_name": assignment.okres_name,
        "kraj_kod": assignment.kraj_kod, "kraj_unit_id": assignment.kraj_unit_id,
        "kraj_name": assignment.kraj_name, "admin_path": assignment.admin_path,
        "display_path": assignment.display_path,
    }
    values.update(changes)
    return AdminAssignment(**values)  # type: ignore[arg-type]
