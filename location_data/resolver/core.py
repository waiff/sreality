"""The pure resolver: BIND → FILL → GRADE → CHECK, one `listing_location` row.

    resolve(claims, ctx, resolver_version=…, registry_version=…) -> Resolution

Nine stages became four, and the identity shrank with them. A resolution used to be keyed
on FIVE version inputs — `claim_set_hash`, `resolver_version`, `registry_version_id`,
`policy_version`, `collision_epoch_id` — because the policy tables and the pin-collision
epoch were corpus-wide inputs a recompute had to be able to invalidate. Both are gone
(policy is code, the collision engine is deleted), so the answer row carries THREE:
`claim_set_hash` says the claims moved, `resolver_version` says a rule moved,
`registry_version` says the mirror moved. Those three are exactly what the drain's sweep
compares to decide a row is stale.

Purity, mechanically: no wall clock (`as_of` is not even needed any more — nothing dates a
policy row), no network, no randomness. `tests/location_data/test_resolver_purity.py`
enforces it by AST scan.

An EMPTY claim set is a legal input and yields an `undetermined` row — granularity
`unknown`, no position, no hierarchy — so a listing the engine has nothing to go on still
gets a row that says so. Coverage is `count(listing_location) = count(active listings)` by
construction (rule 25).
"""

from __future__ import annotations

from collections.abc import Sequence

from location_data.resolver import bind as step_bind
from location_data.resolver import check as step_check
from location_data.resolver import fill as step_fill
from location_data.resolver import grade as step_grade
from location_data.resolver import normalize as step_normalize
from location_data.resolver import serialize
from location_data.resolver.types import Claim, Resolution, ResolverContext


def resolve(
    claims: Sequence[Claim],
    ctx: ResolverContext,
    *,
    resolver_version: str,
    registry_version: str,
    listing_id: int | None = None,
    source: str | None = None,
) -> Resolution:
    ordered = sorted(claims, key=lambda c: c.id)
    listing_id = ordered[0].listing_id if ordered else int(listing_id or 0)
    source = ordered[0].source if ordered else (source or "unknown")

    # ---- normalize. Two passes: the town-as-street rejection needs the constraining obec,
    # and the constraining obec is read off the first pass. Deterministic either way.
    first_pass = step_normalize.normalize_all(ordered)
    prelim = step_bind.collect_constraints(_admissible(ordered, first_pass), first_pass)
    obec_kods = _constraining_obec_kods(prelim, ctx)
    normalized = step_normalize.normalize_all(
        ordered,
        is_place_name=lambda key: _is_place_name(key, ctx),
        street_exists=lambda key: _street_exists(key, obec_kods, ctx),
    )
    # The ONE admissibility gate, evaluated once and handed to every step below: a
    # `subject_scoped=false` extraction (the remax carousel class) is stored evidence and may
    # not rank a candidate, drive the hierarchy or fill a NULL.
    admissible = _admissible(ordered, normalized)

    # ---- 1. BIND.
    pin_claim = step_bind.elect_pin(admissible)
    declared = step_bind.read_declared_precision(
        admissible, coordinate_claim_id=(pin_claim.id if pin_claim else None)
    )
    pin_is_precise = bool(declared.label) and not declared.blurred
    binding, constraints = step_bind.bind(
        admissible, normalized, ctx, pin_is_precise=pin_is_precise
    )
    position = step_bind.place(binding, pin_claim, declared=declared)

    # ---- 2. FILL.
    filled = step_fill.fill(
        binding, constraints, ctx.registry,
        operator=step_bind.operator_fields(admissible),
    )

    # ---- 3. GRADE.
    graded = step_grade.grade(
        binding, position, declared=declared, rank=ctx.granularity_rank
    )

    # ---- 4. CHECK.
    verdict = step_check.check(
        ordered, normalized, filled, position, graded.granularity,
        registry=ctx.registry, rank=ctx.granularity_rank,
    )
    granularity = verdict.granularity or graded.granularity
    czech = verdict.country_status == "cz"

    return Resolution(
        listing_id=listing_id,
        source=source,
        lat=position.lat,
        lon=position.lon,
        country_code=verdict.country_code,
        kraj_name=filled.kraj_name if czech else None,
        okres_name=filled.okres_name if czech else None,
        obec_name=filled.obec_name if czech else None,
        cast_obce_name=filled.cast_obce_name if czech else None,
        street_name=filled.street_name if czech else None,
        house_number_cp=filled.house_number_cp if czech else None,
        house_number_co=filled.house_number_co if czech else None,
        psc=filled.psc if czech else None,
        kraj_kod=filled.kraj_kod if czech else None,
        okres_kod=filled.okres_kod if czech else None,
        obec_kod=filled.obec_kod if czech else None,
        cast_obce_kod=filled.cast_obce_kod if czech else None,
        ulice_kod=filled.ulice_kod if czech else None,
        ruian_adm_kod=filled.ruian_adm_kod if czech else None,
        match_confidence=("low" if not czech else graded.match_confidence),
        granularity=granularity,
        uncertainty_radius_m=step_grade.radius_m(granularity),
        country_status=verdict.country_status,
        disputed=verdict.disputed,
        # The pin-collision epoch was deleted with the engine that classified it, so nothing
        # measures pin sharing today. 0 is the honest reading of "not measured"; a producer
        # comes back with the dedup rebuild or not at all.
        pin_shared_by_n=0,
        resolver_version=resolver_version,
        claim_set_hash=serialize.claim_set_hash(ordered),
        registry_version=registry_version,
    )


def _admissible(
    claims: Sequence[Claim], normalized: dict[int, object]
) -> tuple[Claim, ...]:
    return tuple(
        c for c in claims if step_bind.admissible(c, normalized.get(c.id)) is None  # type: ignore[arg-type]
    )


def _constraining_obec_kods(
    constraints: step_bind.Constraints, ctx: ResolverContext
) -> tuple[int, ...]:
    if constraints.obec_kods:
        return constraints.obec_kods
    codes: list[int] = []
    for key in constraints.obec_keys:
        codes.extend(u.code for u in ctx.registry.admin_units_by_name(key, levels=("obec",)))
    if not codes and constraints.psc:
        codes.extend(ctx.registry.obec_codes_for_psc(constraints.psc))
    return tuple(sorted(set(codes)))


def _is_place_name(key: str, ctx: ResolverContext) -> bool:
    return bool(
        ctx.registry.admin_units_by_name(key, levels=("obec", "cast_obce", "momc", "zsj"))
    )


def _street_exists(key: str, obec_kods: Sequence[int], ctx: ResolverContext) -> bool:
    for obec_kod in obec_kods:
        if any(s.name_norm == key for s in ctx.registry.streets_in_obec(obec_kod)):
            return True
    return False
