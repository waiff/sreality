"""The pure resolver: S1 -> S7, one `location_resolutions` row (03 §3.0-§3.9).

    resolve(claims, ctx, resolver_version=…, registry_version_id=…, policy_version=…,
            collision_epoch_id=…) -> Resolution

FIVE version inputs sit in the resolution's identity (00 §10.3): `claim_set_hash`,
`resolver_version`, `registry_version_id`, `policy_version`, `collision_epoch_id`. The
fifth is not decoration — pin-collision evidence is a function of every OTHER active
listing of that source, so without a version for it a recompute that reclassifies a
cluster cannot invalidate the resolutions that consumed the old classification, and stale
precision keeps serving map pins and geo blocks.

Purity, mechanically: no wall clock (`as_of = max(observed_at)`), no network, no
randomness, and a canonical serialization + content hash stamped on the row so the replay
gate can compare bytes rather than fields.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from location_data.resolver import admin as s5
from location_data.resolver import candidates as s3
from location_data.resolver import country as s2
from location_data.resolver import normalize as s1
from location_data.resolver import position as s4
from location_data.resolver import precision as s6
from location_data.resolver import serialize
from location_data.resolver import survivorship as s7
from location_data.resolver.types import (
    Candidate,
    CandidateSet,
    Claim,
    ClusterEvidence,
    ContradictionSignal,
    FieldWinner,
    Resolution,
    ResolverContext,
)

EPHEMERAL = "ephemeral_display_only"

# The fields S7 arbitrates. `postal_town` is here and is NEVER reconciled against
# `obec_name`: on bazos the two disagree on 57.0 % of rows and neither is wrong.
SURVIVORSHIP_FIELDS: tuple[str, ...] = (
    "street_name",
    "house_number_cp",
    "house_number_co",
    "evidencni",
    "psc",
    "obec_name",
    "cast_obce_name",
    "okres_name",
    "kraj_name",
    "postal_town",
    "development_name",
    "cadastral_territory_name",
    "parcel_number",
)


def resolve(
    claims: Sequence[Claim],
    ctx: ResolverContext,
    *,
    resolver_version: str,
    registry_version_id: int,
    policy_version: str,
    collision_epoch_id: int,
    incumbent: dict[str, Any] | None = None,
) -> Resolution:
    ordered = sorted(claims, key=lambda c: c.id)
    listing_id = ordered[0].listing_id if ordered else 0
    source = ordered[0].source if ordered else "unknown"
    as_of = serialize.as_of(ordered)
    claim_set_hash = serialize.claim_set_hash(ordered)
    signals: list[ContradictionSignal] = []

    # ---- S1. Two passes: the town-as-street rejection needs the constraining obec, and
    # the constraining obec is read off the first pass. Deterministic either way.
    first_pass = s1.normalize_all(ordered)
    prelim = s3.collect_constraints(_admissible(ordered, first_pass), first_pass)
    obec_kods = _constraining_obec_kods(prelim, ctx)
    normalized = s1.normalize_all(
        ordered,
        is_place_name=lambda key: _is_place_name(key, ctx),
        street_exists=lambda key: _street_exists(key, obec_kods, ctx),
    )
    # ---- ADMISSIBILITY, evaluated ONCE (§3.2 rule 4, §3.9.1 invariants 5 and 6). A
    # `subject_scoped=false` extraction — the remax carousel class — is stored evidence and
    # opens `street_from_excluded_block_vs_served` in S9, but it may not rank a candidate,
    # drive the admin chain or fill a NULL. S7 applied this filter already; S3/S4 did not,
    # so the poison won there and then flowed back out through the preserve-if-null fill.
    admissible = _admissible(ordered, normalized)
    admissible_ids = frozenset(c.id for c in admissible)

    # ---- S2. Runs before everything else; `foreign` skips CZ resolution but keeps the pin.
    declared = s4.read_declared_precision(admissible)
    pin = prelim.pin
    country = s2.determine_country(
        ordered, normalized, registry=ctx.registry, constants=ctx.constants, pin=pin
    )
    if country.status == "disputed":
        signals.append(
            ContradictionSignal(
                rule="country_dispute",
                field="country",
                severity="major",
                claimed=[c.get("code") for c in country.conflicting],
                evidence_claim_ids=country.driving_claim_ids,
            )
        )

    # ---- S3.
    pin_is_precise = bool(declared.label) and not declared.blurred
    if country.status == "foreign":
        candidate_set = CandidateSet((), "unmatched", "unknown", {}, ())
        constraints = prelim
    else:
        candidate_set, constraints = s3.generate(
            admissible, normalized, ctx, source=source, pin_is_precise=pin_is_precise
        )

    # ---- S4. Sees every claim (a refused coordinate is still stored as a candidate) but
    # may only WIN with an admissible one.
    outcome = s4.assign(
        ordered, candidate_set, ctx, source=source, admissible_claim_ids=admissible_ids
    )
    signals.extend(outcome.signals)
    declared = outcome.declared  # narrowed to the coordinate claim that actually won
    position = outcome.position
    if position.licence_class == EPHEMERAL:  # structurally impossible; assert it anyway
        raise ValueError("an ephemeral_display_only coordinate reached the resolution winner")

    # ---- S5.
    claimed = _claimed_locality(constraints, ctx)
    admin_assignment = s5.assign(
        candidate_set,
        position,
        ctx,
        country_status=country.status,
        claimed=claimed,
        winner_rank=outcome.chosen_rank,
    )

    # ---- S6.
    cluster = _cluster_for(position, ctx, source=source)
    containment = _containment_radius(admin_assignment, ctx)
    winner_candidate = _candidate_at(outcome.candidates, outcome.chosen_rank)
    precision = s6.assess(
        position,
        admin_assignment,
        ctx,
        source=source,
        declared=declared,
        cluster=cluster,
        match_components=(winner_candidate.component_match if winner_candidate else {}),
        containment_radius_m=containment,
    )
    # Tested against the rung S6 was HANDED, not against the rung it returned: S6 applies
    # the declared cap itself, so comparing the post-cap value could never fire.
    if s6.declared_vs_assigned_conflict(declared, position.granularity, ctx.granularity_rank):
        signals.append(
            ContradictionSignal(
                rule="declared_precision_vs_assigned",
                field="precision_declaration",
                severity="minor",
                stored=precision.granularity,
                claimed=declared.label,
                evidence_claim_ids=declared.claim_ids,
                auto_action="downgraded_precision",
            )
        )

    # ---- S7.
    field_ctx = s7.FieldContext(
        as_of=as_of,
        incumbent=dict(incumbent or {}),
        rank=ctx.granularity_rank,
        claim_granularity={c.id: precision.granularity for c in ordered},
        validate=lambda field, value: _gazetteer_validate(field, value, admin_assignment, ctx),
        derived_values={},
    )
    survivorship = s7.evaluate(
        SURVIVORSHIP_FIELDS, ordered, normalized, ctx.field_policy, field_ctx
    )
    signals.extend(survivorship.signals)
    fields = _fill_from_registry(survivorship.fields, winner_candidate, ctx)

    status = _status(ordered, country, candidate_set, position)
    resolution = Resolution(
        listing_id=listing_id,
        source=source,
        status=status,
        as_of=as_of,
        claim_set_hash=claim_set_hash,
        content_hash="",
        resolver_version=resolver_version,
        registry_version_id=registry_version_id,
        policy_version=policy_version,
        collision_epoch_id=collision_epoch_id,
        country=country,
        position=position,
        admin=admin_assignment,
        precision=precision,
        candidates=outcome.candidates,
        chosen_rank=outcome.chosen_rank,
        chosen_rule=outcome.chosen_rule,
        runner_up_score_gap=candidate_set.runner_up_score_gap,
        fields=fields,
        input_claim_ids=tuple(c.id for c in ordered),
        position_licence_class=position.licence_class,
        contradiction_signals=tuple(
            sorted(signals, key=lambda s: (s.rule, s.field, s.severity))
        ),
        rung_trace=candidate_set.rung_trace,
        # S7's own "this field had claims and still produced no winner" signal. Discarding
        # it hid the fact that five survivorship fields had no v1 policy row at all.
        survivorship_blocked=survivorship.blocked,
    )
    return _stamp_content_hash(resolution)


def content_payload(resolution: Resolution) -> dict[str, Any]:
    """What the content hash covers: everything the resolver decided, and nothing about
    WHEN it ran. `resolved_at` is deliberately absent — it is the one field that differs
    between two byte-identical replays."""
    return {
        "listing_id": resolution.listing_id,
        "source": resolution.source,
        "status": resolution.status,
        "as_of": resolution.as_of,
        "claim_set_hash": resolution.claim_set_hash,
        "resolver_version": resolution.resolver_version,
        "registry_version_id": resolution.registry_version_id,
        "policy_version": resolution.policy_version,
        "collision_epoch_id": resolution.collision_epoch_id,
        "country": resolution.country,
        "position": resolution.position,
        "admin": resolution.admin,
        "precision": resolution.precision,
        "candidates": list(resolution.candidates),
        "chosen_rank": resolution.chosen_rank,
        "chosen_rule": resolution.chosen_rule,
        "runner_up_score_gap": resolution.runner_up_score_gap,
        "fields": {k: v for k, v in sorted(resolution.fields.items())},
        "input_claim_ids": list(resolution.input_claim_ids),
        "position_licence_class": resolution.position_licence_class,
        "contradiction_signals": list(resolution.contradiction_signals),
        "rung_trace": list(resolution.rung_trace),
        "survivorship_blocked": list(resolution.survivorship_blocked),
    }


def _stamp_content_hash(resolution: Resolution) -> Resolution:
    payload = content_payload(resolution)
    digest = serialize.digest(payload)
    values = {
        name: getattr(resolution, name) for name in resolution.__dataclass_fields__  # type: ignore[attr-defined]
    }
    values["content_hash"] = digest
    return Resolution(**values)


def _fill_from_registry(
    fields: dict[str, Any], winner: Candidate | None, ctx: ResolverContext
) -> dict[str, Any]:
    """PRESERVE-IF-NULL fill from the matched address point (03 §3.9.3: registry wins čp/čo
    and PSČ when R0/R1 matched). Only NULLs are filled — a claimed value that DISAGREES is
    S9's `house_number_disagreement`, not a silent overwrite here."""
    if winner is None or winner.ruian_adm_kod is None:
        return dict(fields)
    point = ctx.registry.address_point(winner.ruian_adm_kod)
    if point is None:
        return dict(fields)
    out = dict(fields)
    for name, value in (
        ("house_number_cp", point.cislo_domovni),
        ("house_number_co", point.cislo_orientacni),
        ("psc", point.psc),
    ):
        if name in out or value is None:
            continue
        out[name] = FieldWinner(
            field=name,
            value=str(value),
            source_claim_ids=(),
            rule="registry:address_point",
            method="registry_derived",
            granularity=winner.granularity,
            confidence="exact",
        )
    return out


def _status(
    claims: Sequence[Claim], country, candidate_set: CandidateSet, position
) -> str:
    if not claims:
        return "no_input"
    if country.status == "foreign":
        return "skipped_foreign"
    if candidate_set.ambiguity_status == "ambiguous":
        return "ambiguous"
    if not candidate_set.candidates and position.lat is None:
        return "unmatched"
    if not candidate_set.candidates:
        # A pin with no address identity is a resolved row at `unknown` granularity, not a
        # failure: it still carries a position, its axes and its collision evidence.
        return "resolved"
    return "resolved"


def _admissible(
    claims: Sequence[Claim], normalized: dict[int, Any]
) -> tuple[Claim, ...]:
    """The ONE admissibility gate, shared by S3, S4 and S7 (03 §3.2 rule 4 / §3.9.1)."""
    return tuple(c for c in claims if s7.admissible(c, normalized.get(c.id)) is None)


def _constraining_obec_kods(constraints: s3.Constraints, ctx: ResolverContext) -> tuple[int, ...]:
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


def _claimed_locality(constraints: s3.Constraints, ctx: ResolverContext) -> s5.ClaimedLocality:
    for key in constraints.obec_keys:
        units = ctx.registry.admin_units_by_name(key, levels=("obec",))
        if len(units) == 1:
            return s5.ClaimedLocality(units[0], constraints.claim_ids.get("obec_name", ()))
    return s5.ClaimedLocality(None, ())


def _cluster_for(position, ctx: ResolverContext, *, source: str) -> ClusterEvidence | None:
    if ctx.collision is None or position.lat is None or position.lon is None:
        return None
    return ctx.collision.for_point(source, position.lat, position.lon)


def _containment_radius(admin_assignment, ctx: ResolverContext) -> float | None:
    if admin_assignment.obec_unit_id is None:
        return None
    unit = ctx.registry.admin_unit(admin_assignment.obec_unit_id)
    return unit.containment_radius_m if unit else None


def _candidate_at(candidates: Sequence[Candidate], rank: int | None) -> Candidate | None:
    if rank is None:
        return None
    return next((c for c in candidates if c.rank == rank), None)


def _gazetteer_validate(field: str, value: str, admin_assignment, ctx: ResolverContext) -> bool:
    """Invariant 3, applied to text-derived claims only: the value must exist in the
    gazetteer within the constraining unit. A failure DOWNGRADES and routes to S9 — the
    value may be the correct one and the constraint wrong (the Bílovec case)."""
    if not value:
        return False
    if field == "street_name":
        if admin_assignment.obec_kod is None:
            return True
        return any(
            s.name_norm == value for s in ctx.registry.streets_in_obec(admin_assignment.obec_kod)
        )
    if field in ("obec_name", "cast_obce_name", "okres_name", "kraj_name"):
        return bool(ctx.registry.admin_units_by_name(value))
    return True
