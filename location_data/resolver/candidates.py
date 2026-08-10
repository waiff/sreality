"""S3 — candidate generation against the RÚIAN gazetteer (03 §3.5). Match, don't parse.

The gazetteer is closed and finite (3 020 222 address points), so retrieval substitutes
for parsing; parsing exists only to extract CONSTRAINTS that filter and re-rank.

Two properties are load-bearing and are the reason this module returns a set rather than
an answer:

* **The candidate set is stored COMPLETE** (D2b). Collapsing to one row at ingest destroys
  the information needed to detect ambiguity, let an operator arbitrate, and re-rank later.
* **`ambiguous` is a first-class status, not a low score.** "Three equally good candidates"
  routes to the operator queue; "one weak match" does not.

Homonym disambiguation (§3.5.3) resolves names LOCALLY and HIERARCHICALLY inside the
constraining parent, in descending discriminating power: PSČ, okres/kraj claims, cadastral
territory, `homonym_qualifier`, and only then the coordinate — as a tie-breaker among
already-qualified candidates, never as the primary disambiguator. Its three named
regression tests (Krásný Les, Bílovec, Bořislav 40) are in
`tests/location_data/test_resolver_homonyms.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from location_data.resolver import uncertainty
from location_data.resolver.normalize import normalize_match_key
from location_data.resolver.types import (
    AdminUnit,
    Candidate,
    CandidateSet,
    Claim,
    GranularityRank,
    NormalizedClaim,
    RegistryView,
    ResolverContext,
)

# pg_trgm's own similarity, reimplemented deterministically so the resolver can rank a
# typo-tolerant match with no database (the DB implementation uses the same definition).
_TRGM_THRESHOLD = 0.45
_TRGM_MARGIN = 0.10
# `ambiguous` when the top two scores are this close (03 §3.5.2).
AMBIGUITY_MARGIN = 5.0

_RUNG_BASE_SCORE = {
    "R0": 100.0,
    "R1": 90.0,
    "R2": 70.0,
    "R3": 60.0,
    "R5": 55.0,
    "R4": 45.0,
    "R6": 35.0,
    "R7": 25.0,
}


def trigram_similarity(a: str, b: str) -> float:
    """`pg_trgm.similarity`: |A ∩ B| / |A ∪ B| over the padded trigram sets."""
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _trigrams(value: str) -> frozenset[str]:
    words = [w for w in value.split() if w]
    grams: set[str] = set()
    for word in words:
        padded = f"  {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return frozenset(grams)


@dataclass(frozen=True, slots=True)
class Constraints:
    """What S1's sidecar says about WHERE the listing is, before any match is attempted."""

    obec_kods: tuple[int, ...] = ()
    psc: str | None = None
    okres_keys: tuple[str, ...] = ()
    kraj_keys: tuple[str, ...] = ()
    obec_keys: tuple[str, ...] = ()
    cast_obce_keys: tuple[str, ...] = ()
    katuz_keys: tuple[str, ...] = ()
    parcel_labels: tuple[str, ...] = ()
    qualifiers: tuple[str, ...] = ()
    street_key: str | None = None
    street_verbatim: str | None = None
    cislo_domovni: int | None = None
    cislo_orientacni: int | None = None
    kod_adm: int | None = None
    stavebni_objekt_kod: int | None = None
    pin: tuple[float, float] | None = None
    claim_ids: dict[str, tuple[int, ...]] = field(default_factory=dict)


def collect_constraints(
    claims: Sequence[Claim], normalized: dict[int, NormalizedClaim]
) -> Constraints:
    obec_kods: list[int] = []
    psc: str | None = None
    buckets: dict[str, list[str]] = {
        "okres": [], "kraj": [], "obec": [], "cast_obce": [], "katuz": [],
        "parcel": [], "qualifier": [],
    }
    ids: dict[str, list[int]] = {}
    street_key = street_verbatim = None
    cp = co = kod_adm = so_kod = None
    pin: tuple[float, float] | None = None

    def note(kind: str, claim_id: int) -> None:
        ids.setdefault(kind, []).append(claim_id)

    for claim in sorted(claims, key=lambda c: c.id):
        norm = normalized.get(claim.id)
        key = norm.value_ascii if norm else None
        slots = norm.typed_slots if norm else {}
        rejected = bool(norm and norm.rejections)
        t = claim.claim_type
        if t == "address_point_id" and claim.value_text:
            kod_adm = _as_int(claim.value_text)
            note("kod_adm", claim.id)
        elif t == "building_id" and claim.value_text:
            so_kod = _as_int(claim.value_text)
            note("building_id", claim.id)
        elif t == "obec_code" and claim.value_text and "." not in claim.value_text:
            code = _as_int(claim.value_text)
            if code is not None:
                obec_kods.append(code)
                note("obec_code", claim.id)
        elif t == "psc":
            value = slots.get("psc")
            if isinstance(value, str):
                psc = psc or value
                note("psc", claim.id)
        elif t == "street_name" and not rejected:
            if key:
                street_key = street_key or key
                street_verbatim = street_verbatim or str(slots.get("street") or claim.value_text)
                note("street", claim.id)
            if cp is None and slots.get("cislo_domovni"):
                cp = _as_int(str(slots["cislo_domovni"]))
            if co is None and slots.get("cislo_orientacni"):
                co = _as_int(str(slots["cislo_orientacni"]))
        elif t == "house_number_cp":
            cp = cp if cp is not None else _as_int(str(slots.get("cislo_domovni") or ""))
            note("house_number_cp", claim.id)
        elif t == "house_number_co":
            co = co if co is not None else _as_int(
                str(slots.get("cislo_orientacni") or slots.get("cislo_domovni") or "")
            )
            note("house_number_co", claim.id)
        elif t == "obec_name" and key and not rejected:
            buckets["obec"].append(key)
            note("obec_name", claim.id)
        elif t in ("cast_obce_name", "quarter_name", "mestsky_obvod_name") and key:
            buckets["cast_obce"].append(key)
            note("cast_obce_name", claim.id)
        elif t == "okres_name" and key:
            buckets["okres"].append(key)
            note("okres_name", claim.id)
        elif t == "kraj_name" and key:
            buckets["kraj"].append(key)
            note("kraj_name", claim.id)
        elif t == "cadastral_territory_name" and key:
            buckets["katuz"].append(key)
            note("cadastral_territory_name", claim.id)
        elif t == "parcel_number" and claim.value_text:
            buckets["parcel"].append(normalize_match_key(claim.value_text))
            note("parcel_number", claim.id)
        elif t == "homonym_qualifier" and key:
            buckets["qualifier"].append(key)
            note("homonym_qualifier", claim.id)
        elif t == "coordinate" and claim.has_position and pin is None:
            pin = (float(claim.lat), float(claim.lon))  # type: ignore[arg-type]
            note("coordinate", claim.id)

    return Constraints(
        obec_kods=tuple(dict.fromkeys(obec_kods)),
        psc=psc,
        okres_keys=tuple(dict.fromkeys(buckets["okres"])),
        kraj_keys=tuple(dict.fromkeys(buckets["kraj"])),
        obec_keys=tuple(dict.fromkeys(buckets["obec"])),
        cast_obce_keys=tuple(dict.fromkeys(buckets["cast_obce"])),
        katuz_keys=tuple(dict.fromkeys(buckets["katuz"])),
        parcel_labels=tuple(dict.fromkeys(buckets["parcel"])),
        qualifiers=tuple(dict.fromkeys(buckets["qualifier"])),
        street_key=street_key,
        street_verbatim=street_verbatim,
        cislo_domovni=cp,
        cislo_orientacni=co,
        kod_adm=kod_adm,
        stavebni_objekt_kod=so_kod,
        pin=pin,
        claim_ids={k: tuple(v) for k, v in sorted(ids.items())},
    )


def _as_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------- obec resolution


def qualify_obec_candidates(
    units: Sequence[AdminUnit],
    constraints: Constraints,
    *,
    registry: RegistryView,
    pin_is_precise: bool,
) -> tuple[list[AdminUnit], list[str]]:
    """§3.5.3's qualifier ladder. Returns (surviving units, applied qualifiers)."""
    applied: list[str] = []
    surviving = list(units)

    if constraints.psc and len(surviving) > 1:
        by_psc = [u for u in surviving if constraints.psc in u.psc_set]
        if not by_psc:
            obec_kods = set(registry.obec_codes_for_psc(constraints.psc))
            by_psc = [u for u in surviving if u.code in obec_kods]
        if by_psc:
            surviving = by_psc
            applied.append("psc")

    for keys, attr, label in (
        (constraints.okres_keys, "okres_kod", "okres"),
        (constraints.kraj_keys, "kraj_kod", "kraj"),
    ):
        if not keys or len(surviving) <= 1:
            continue
        wanted: set[int] = set()
        for key in keys:
            for unit in registry.admin_units_by_name(key, levels=(label,)):
                wanted.add(unit.code)
        if not wanted:
            continue
        filtered = [u for u in surviving if getattr(u, attr) in wanted]
        if filtered:
            surviving = filtered
            applied.append(label)

    if constraints.katuz_keys and len(surviving) > 1:
        wanted = set()
        for key in constraints.katuz_keys:
            for ku in registry.admin_units_by_name(key, levels=("katastralni_uzemi",)):
                for ancestor in registry.admin_chain(ku.unit_id):
                    if ancestor.level == "obec":
                        wanted.add(ancestor.code)
        if wanted:
            filtered = [u for u in surviving if u.code in wanted]
            if filtered:
                surviving = filtered
                applied.append("cadastral_territory")

    if constraints.qualifiers and len(surviving) > 1:
        filtered = [
            u
            for u in surviving
            if u.qualifier and any(q in normalize_match_key(u.qualifier) for q in constraints.qualifiers)
        ]
        if not filtered:
            filtered = [
                u
                for u in surviving
                if any(q in u.name_norm for q in constraints.qualifiers)
            ]
        if filtered:
            surviving = filtered
            applied.append("homonym_qualifier")

    # The coordinate is a TIE-BREAKER among already-qualified candidates, and only when the
    # pin's own quality is precise. Never the primary disambiguator: the geocode of an
    # ambiguous town name IS the town centroid, which is how Krásný Les went 100 km wrong.
    if constraints.pin and pin_is_precise and len(surviving) > 1:
        covering = registry.containing_obec(*constraints.pin)
        if covering is not None:
            filtered = [u for u in surviving if u.code == covering.code]
            if filtered:
                surviving = filtered
                applied.append("coordinate_tiebreak")

    return surviving, applied


# ------------------------------------------------------------------------- the ladder


def generate(
    claims: Sequence[Claim],
    normalized: dict[int, NormalizedClaim],
    ctx: ResolverContext,
    *,
    source: str,
    pin_is_precise: bool = False,
) -> tuple[CandidateSet, Constraints]:
    registry = ctx.registry
    rank = ctx.granularity_rank
    constraints = collect_constraints(claims, normalized)
    trace: list[dict[str, Any]] = []
    out: list[Candidate] = []

    def stop(rung: str, reason: str, produced: int = 0) -> None:
        trace.append({"rung": rung, "produced": produced, "reason": reason})

    # ---- constraining obec set (feeds R1-R3; also produces R4/R6 candidates below).
    obec_units: list[AdminUnit] = []
    obec_qualifiers: list[str] = []
    if constraints.obec_kods:
        obec_units = [
            u for k in constraints.obec_kods if (u := registry.admin_unit_by_code("obec", k))
        ]
        obec_qualifiers = ["obec_code"]
    elif constraints.obec_keys:
        found: list[AdminUnit] = []
        for key in constraints.obec_keys:
            found.extend(registry.admin_units_by_name(key, levels=("obec",)))
        obec_units, obec_qualifiers = qualify_obec_candidates(
            _dedupe_units(found), constraints, registry=registry, pin_is_precise=pin_is_precise
        )
    elif constraints.psc:
        obec_units = [
            u
            for k in registry.obec_codes_for_psc(constraints.psc)
            if (u := registry.admin_unit_by_code("obec", k))
        ]
        obec_qualifiers = ["psc"]

    constraining_obec_kods = tuple(sorted({u.code for u in obec_units}))

    # ---- R0: a portal-supplied registry key. The prize (bezrealitky `ruianId`).
    if constraints.kod_adm is not None:
        point = registry.address_point(constraints.kod_adm)
        if point is not None:
            out.append(
                _address_point_candidate(
                    point, ctx, source=source, rung="R0",
                    components={"house_number": "matched", "street": "matched", "obec": "matched"},
                    claim_ids=constraints.claim_ids.get("kod_adm", ()),
                    match_confidence="exact",
                )
            )
            stop("R0", "matched", 1)
        else:
            stop("R0", "kod_adm_not_in_mirror")
    else:
        stop("R0", "no_registry_key_claim")

    # ---- R1: obec + street + čp/čo.
    if constraining_obec_kods and constraints.street_key and (
        constraints.cislo_domovni or constraints.cislo_orientacni
    ):
        matched = 0
        for obec_kod in constraining_obec_kods:
            for point in registry.address_points_by_number(
                obec_kod=obec_kod,
                street_name_norm=constraints.street_key,
                cislo_domovni=constraints.cislo_domovni,
                cislo_orientacni=constraints.cislo_orientacni,
            ):
                out.append(
                    _address_point_candidate(
                        point, ctx, source=source, rung="R1",
                        components={
                            "house_number": "matched",
                            "street": "matched",
                            "obec": "matched",
                            "psc": "matched" if constraints.psc == point.psc else "unmatched",
                        },
                        claim_ids=_ids(constraints, "street", "house_number_cp", "house_number_co"),
                        match_confidence="high",
                    )
                )
                matched += 1
        stop("R1", "matched" if matched else "no_address_point_for_number", matched)
    else:
        stop("R1", "insufficient_constraints")

    # ---- R2 / R3: street inside the constraining obec, exact then typo-tolerant.
    if constraining_obec_kods and constraints.street_key:
        exact_hits = 0
        fuzzy: list[tuple[float, Any]] = []
        for obec_kod in constraining_obec_kods:
            streets = registry.streets_in_obec(obec_kod)
            for street in streets:
                if street.name_norm == constraints.street_key:
                    out.append(_street_candidate(street, ctx, source=source, rung="R2",
                                                 constraints=constraints, rank_table=rank))
                    exact_hits += 1
            if exact_hits:
                continue
            for street in streets:
                sim = trigram_similarity(constraints.street_key, street.name_norm)
                if sim >= _TRGM_THRESHOLD:
                    fuzzy.append((sim, street))
        stop("R2", "matched" if exact_hits else "no_exact_street", exact_hits)
        if exact_hits:
            stop("R3", "skipped_exact_match_exists")
        elif fuzzy:
            fuzzy.sort(key=lambda t: (-t[0], t[1].name_norm))
            best = fuzzy[0][0]
            runner_up = fuzzy[1][0] if len(fuzzy) > 1 else 0.0
            if best - runner_up >= _TRGM_MARGIN or len(fuzzy) == 1:
                out.append(
                    _street_candidate(
                        fuzzy[0][1], ctx, source=source, rung="R3", constraints=constraints,
                        rank_table=rank, similarity=best, relaxations=("street_unaccent_fuzzy",),
                    )
                )
                stop("R3", "matched_fuzzy", 1)
            else:
                stop("R3", "fuzzy_margin_too_small")
        else:
            stop("R3", "no_fuzzy_street_above_threshold")
    else:
        stop("R2", "no_street_or_no_obec")
        stop("R3", "no_street_or_no_obec")

    # ---- R5: cadastral claim (k.ú. name + parcel number).
    produced = 0
    for katuz in constraints.katuz_keys:
        for label in constraints.parcel_labels or ("",):
            if not label:
                continue
            for parcel in registry.parcels(katuz_name_norm=katuz, parcel_label_norm=label):
                out.append(_parcel_candidate(parcel, ctx, source=source, constraints=constraints))
                produced += 1
    stop("R5", "matched" if produced else "no_parcel_match", produced)

    # ---- R4: obec / část obce / quarter by name (the point-set level).
    produced = 0
    for unit in obec_units:
        out.append(
            _admin_candidate(
                unit, ctx, source=source, rung="R4", granularity="obec",
                claim_ids=_ids(constraints, "obec_name", "obec_code"),
                qualifiers=tuple(obec_qualifiers),
            )
        )
        produced += 1
    if constraints.cast_obce_keys and constraining_obec_kods:
        for key in constraints.cast_obce_keys:
            for unit in registry.admin_units_by_name(
                key, levels=("cast_obce", "momc", "spravni_obvod", "zsj")
            ):
                if unit.obec_kod is not None and unit.obec_kod not in constraining_obec_kods:
                    continue
                out.append(
                    _admin_candidate(
                        unit, ctx, source=source, rung="R4",
                        granularity="cast_obce_or_quarter",
                        claim_ids=_ids(constraints, "cast_obce_name"),
                        qualifiers=("obec_constrained",),
                    )
                )
                produced += 1
    stop("R4", "matched" if produced else "no_admin_name_match", produced)

    # ---- R6: PSČ alone.
    if not obec_units and constraints.psc:
        codes = registry.obec_codes_for_psc(constraints.psc)
        produced = 0
        for code in codes:
            unit = registry.admin_unit_by_code("obec", code)
            if unit is None:
                continue
            out.append(
                _admin_candidate(
                    unit, ctx, source=source, rung="R6", granularity="obec",
                    claim_ids=_ids(constraints, "psc"), qualifiers=("psc",),
                )
            )
            produced += 1
        stop("R6", "matched" if produced else "psc_not_in_mirror", produced)
    else:
        stop("R6", "not_needed" if obec_units else "no_psc")

    # ---- R7: coordinate only. DERIVED, never a claim (§3.6.3) — it can only produce an
    # admin-level candidate, never a street or house number.
    if not out and constraints.pin is not None:
        covering = registry.containing_obec(*constraints.pin)
        if covering is not None:
            out.append(
                _admin_candidate(
                    covering, ctx, source=source, rung="R7", granularity="obec",
                    claim_ids=_ids(constraints, "coordinate"), qualifiers=("reverse_derived",),
                )
            )
            stop("R7", "matched", 1)
        else:
            stop("R7", "point_outside_every_obec")
    else:
        stop("R7", "not_needed" if out else "no_coordinate")

    if not out:
        stop("R8", "nothing_usable")
        return (
            CandidateSet((), "unmatched", "unknown", _constraining(constraining_obec_kods, constraints), tuple(trace)),
            constraints,
        )

    ranked = _rank(out, rank)
    top = ranked[0]
    gap = (top.score - ranked[1].score) if len(ranked) > 1 else None
    at_top = [c for c in ranked if c.granularity == top.granularity and c.rung == top.rung]
    ambiguous = (gap is not None and gap < AMBIGUITY_MARGIN) or len(at_top) > 1
    status = "ambiguous" if ambiguous else "resolved"
    return (
        CandidateSet(
            candidates=tuple(ranked),
            ambiguity_status=status,
            top_granularity=top.granularity,
            constraining=_constraining(constraining_obec_kods, constraints),
            rung_trace=tuple(trace),
            runner_up_score_gap=gap,
        ),
        constraints,
    )


def _constraining(obec_kods: tuple[int, ...], constraints: Constraints) -> dict[str, Any]:
    return {
        "obec_kods": list(obec_kods),
        "psc": constraints.psc,
        "street": constraints.street_key,
    }


def _dedupe_units(units: Sequence[AdminUnit]) -> list[AdminUnit]:
    seen: dict[int, AdminUnit] = {}
    for unit in units:
        seen.setdefault(unit.unit_id, unit)
    return [seen[k] for k in sorted(seen)]


def _ids(constraints: Constraints, *kinds: str) -> tuple[int, ...]:
    out: list[int] = []
    for kind in kinds:
        out.extend(constraints.claim_ids.get(kind, ()))
    return tuple(sorted(set(out)))


def _rank(candidates: Sequence[Candidate], rank: GranularityRank) -> list[Candidate]:
    ordered = sorted(
        candidates,
        key=lambda c: (
            -c.score,
            -rank.rank(c.granularity),
            c.ruian_adm_kod or 0,
            c.admin_unit_id or 0,
            c.ulice_kod or 0,
        ),
    )
    return [_with_rank(c, i) for i, c in enumerate(ordered, start=1)]


def _with_rank(candidate: Candidate, rank_value: int) -> Candidate:
    return Candidate(
        rung=candidate.rung, rank=rank_value, score=candidate.score,
        target_kind=candidate.target_kind, granularity=candidate.granularity,
        position_source=candidate.position_source, match_confidence=candidate.match_confidence,
        uncertainty_radius_m=candidate.uncertainty_radius_m,
        radius_semantics=candidate.radius_semantics, licence_class=candidate.licence_class,
        blur_evidence=candidate.blur_evidence, lat=candidate.lat, lon=candidate.lon,
        ruian_adm_kod=candidate.ruian_adm_kod,
        stavebni_objekt_kod=candidate.stavebni_objekt_kod, parcela_id=candidate.parcela_id,
        ulice_kod=candidate.ulice_kod, ulice_id=candidate.ulice_id,
        admin_unit_id=candidate.admin_unit_id,
        component_match=candidate.component_match, distance_to_pin_m=candidate.distance_to_pin_m,
        rejected_reason=candidate.rejected_reason, source_claim_ids=candidate.source_claim_ids,
        relaxations=candidate.relaxations,
    )


def _address_point_candidate(
    point,
    ctx: ResolverContext,
    *,
    source: str,
    rung: str,
    components: dict[str, str],
    claim_ids: tuple[int, ...],
    match_confidence: str,
) -> Candidate:
    granularity = "address_point"
    radius, semantics = uncertainty.radius_for(
        ctx.uncertainty_policy,
        position_source="registry_point",
        granularity=granularity,
        source=source,
    )
    return Candidate(
        rung=rung, rank=0, score=_RUNG_BASE_SCORE[rung] + _component_bonus(components),
        target_kind="address_point", granularity=granularity, position_source="registry_point",
        match_confidence=match_confidence, uncertainty_radius_m=radius,
        radius_semantics=semantics, licence_class="cc_by_ruian", lat=point.lat, lon=point.lon,
        ruian_adm_kod=point.kod_adm, stavebni_objekt_kod=point.stavebni_objekt_code,
        component_match=dict(sorted(components.items())), source_claim_ids=claim_ids,
    )


def _street_candidate(
    street,
    ctx: ResolverContext,
    *,
    source: str,
    rung: str,
    constraints: Constraints,
    rank_table: GranularityRank,
    similarity: float | None = None,
    relaxations: tuple[str, ...] = (),
) -> Candidate:
    # A house-number claim we could not join to an address point still narrows the street
    # to a segment (01 §2's `street_segment` rung); without one it is a bare street.
    granularity = "street_segment" if constraints.cislo_domovni else "street"
    # RÚIAN streets carry no geometry in the mirror, so a street candidate has no position
    # of its own; S4 assigns the position from the pin or the admin centroid.
    radius, semantics = uncertainty.radius_for(
        ctx.uncertainty_policy, position_source="none", granularity=granularity, source=source
    )
    score = _RUNG_BASE_SCORE[rung] + (10.0 * similarity if similarity is not None else 0.0)
    return Candidate(
        rung=rung, rank=0, score=score, target_kind="street", granularity=granularity,
        position_source="none", match_confidence="high" if rung == "R2" else "medium",
        uncertainty_radius_m=radius, radius_semantics=semantics, licence_class="cc_by_ruian",
        ulice_kod=street.code, ulice_id=street.street_id,
        admin_unit_id=street.obec_unit_id,
        component_match={"street": "matched" if rung == "R2" else "plausible", "obec": "matched"},
        source_claim_ids=_ids(constraints, "street"), relaxations=relaxations,
    )


def _parcel_candidate(parcel, ctx: ResolverContext, *, source: str, constraints: Constraints) -> Candidate:
    radius, semantics = uncertainty.radius_for(
        ctx.uncertainty_policy,
        position_source="registry_point" if parcel.lat is not None else "none",
        granularity="parcel",
        source=source,
    )
    return Candidate(
        rung="R5", rank=0, score=_RUNG_BASE_SCORE["R5"], target_kind="parcel",
        granularity="parcel",
        position_source="registry_point" if parcel.lat is not None else "none",
        match_confidence="high", uncertainty_radius_m=radius, radius_semantics=semantics,
        licence_class="cc_by_ruian", lat=parcel.lat, lon=parcel.lon, parcela_id=parcel.parcel_id,
        component_match={"parcel": "matched"},
        source_claim_ids=_ids(constraints, "cadastral_territory_name", "parcel_number"),
    )


def _admin_candidate(
    unit: AdminUnit,
    ctx: ResolverContext,
    *,
    source: str,
    rung: str,
    granularity: str,
    claim_ids: tuple[int, ...],
    qualifiers: tuple[str, ...],
) -> Candidate:
    has_point = unit.lat is not None and unit.lon is not None
    position_source = "admin_centroid" if has_point else "none"
    radius, semantics = uncertainty.radius_for(
        ctx.uncertainty_policy,
        position_source=position_source,
        granularity=granularity,
        source=source,
        containment_radius_m=unit.containment_radius_m,
    )
    bonus = 5.0 * len(qualifiers)
    return Candidate(
        rung=rung, rank=0, score=_RUNG_BASE_SCORE[rung] + bonus, target_kind="admin_unit",
        granularity=granularity, position_source=position_source,
        match_confidence="high" if qualifiers else "medium", uncertainty_radius_m=radius,
        radius_semantics=semantics, licence_class="cc_by_ruian", lat=unit.lat, lon=unit.lon,
        admin_unit_id=unit.unit_id, component_match={"obec": "matched"},
        source_claim_ids=claim_ids, relaxations=qualifiers,
    )


def _component_bonus(components: dict[str, str]) -> float:
    return sum(2.0 for v in components.values() if v == "matched")
