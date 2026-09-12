"""BIND — step 1 of 4: the finest RÚIAN entity the claims justify, and the pin that goes
with it.

Match, don't parse. The gazetteer is closed and finite (3 020 222 address points), so
retrieval substitutes for parsing; parsing exists only to extract CONSTRAINTS that filter
and re-rank. Homonym disambiguation resolves names LOCALLY and HIERARCHICALLY inside the
constraining parent, in descending discriminating power: PSČ, okres/kraj claims, cadastral
territory, `homonym_qualifier`, and only then the coordinate — as a tie-breaker among
already-qualified candidates, never as the primary disambiguator (the geocode of an
ambiguous town name IS the town centroid, which is how Krásný Les went 100 km wrong). Its
three named regression tests are in `tests/location_data/test_resolver_homonyms.py`.

Two things changed in W2-a and both are deletions:

* **The candidate set is no longer STORED.** `location_resolution_candidates` is gone, so
  BIND returns ONE `Binding` — the winner — plus the evidence GRADE needs to score it. The
  ranking still happens; only the persistence of the losers does not.
* **Ambiguity is a CONFIDENCE, not a queue.** "Three equally good candidates" used to be a
  first-class `ambiguous` status routed to an operator who does not exist. It now grades
  `low` and is served, because an honest low-confidence answer beats no answer — and the
  2026-09-11 audit measured the alternative: 24 601 bazos rows served an arbitrary
  id-ordered winner while the row was labelled ambiguous.

The parcel rung is deleted: it was unreachable (no portal states a cadastral parcel in a
form that joins) and it was the only reader of `ruian_parcels`.

**Which coordinate becomes the pin is a decision, not an accident of row order.** A listing
can carry several `coordinate` claims — a portal-declared exact pin, a blurred fallback, a
carousel/neighbour pin that is `subject_scoped=false`. The pin is chosen from the ADMISSIBLE
ones ordered by DECLARED QUALITY and only then by claim id.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from location_data.resolver.geo import distance_between
from location_data.resolver.normalize import normalize_match_key
from location_data.resolver.types import (
    AdminUnit,
    Binding,
    Claim,
    GranularityRank,
    NormalizedClaim,
    Position,
    RegistryView,
    ResolverContext,
)

EPHEMERAL = "ephemeral_display_only"

# Invariant 5 (03 §3.9.1): portal-proprietary identifiers are claims, never fields.
PORTAL_PROPRIETARY_FIELDS = frozenset({"portal_admin_id", "portal_street_id", "osm_relation_id"})

# pg_trgm's own similarity, reimplemented deterministically so the resolver can rank a
# typo-tolerant match with no database (the DB implementation uses the same definition).
_TRGM_THRESHOLD = 0.45
_TRGM_MARGIN = 0.10
# `ambiguous` when the top two scores are this close.
AMBIGUITY_MARGIN = 5.0

# Qualifiers that settle a tie by evidence weaker than a validated name: the answer is
# served, but never above `low` confidence.
LOW_CONFIDENCE_QUALIFIERS = frozenset(
    {"coordinate_tiebreak_imprecise", "postal_town", "pip_nearest_within_n_m"}
)
# Qualifiers that ARE independent fields agreeing with the entity, and therefore count
# toward GRADE's agreement tally. A coordinate tie-break is deliberately not one of them.
AGREEMENT_QUALIFIERS = frozenset(
    {"obec_code", "psc", "okres", "kraj", "cadastral_territory", "homonym_qualifier"}
)

_RUNG_BASE_SCORE = {"R0": 100.0, "R1": 90.0, "R2": 70.0, "R3": 60.0, "R4": 45.0,
                    "R6": 35.0, "R7": 25.0, "R8": 15.0}

# The sliver tolerance, in metres. It was `location_constants.pip_sliver_tolerance_m` (250 m,
# the value migration 289 had already chosen for the legacy admin-geo trigger) and moves here
# as a code constant because W2-b drops that table. A pin this far outside every obec polygon
# is a boundary artifact — a rounded coordinate, a simplified polygon edge, a river bank — not
# a listing in the sea.
PIP_SLIVER_TOLERANCE_M = 250.0

# Beyond this the registry point and the portal pin are telling different stories: the
# registry point stays the position and GRADE caps the confidence.
REGISTRY_PIN_CONFLICT_M = 300.0

# Portal-declared labels that mean "this pin is not address-grade". The portal contract maps
# its own vocabulary onto these; a label we do not know is NOT treated as blurred (cap,
# never certify — and never invent a cap either).
BLURRED_DECLARED_LABELS = frozenset(
    {
        "municipality", "obec", "ward", "quarter", "citypart", "street",
        "approximate", "priblizna", "estimated", "regional", "area", "polygon",
    }
)
PRECISE_DECLARED_LABELS = frozenset({"gps", "address", "exact", "presna", "rooftop", "ruian"})
# A `precision_declaration` with no `declared_precision_label` may still carry the portal's
# vocabulary in `value_text` (sreality's `inaccuracy_type`). Anything else in `value_text` —
# our own `coords.source` stamp ('page', 'carry_forward'), a map-legend sentence — is NOT a
# declared precision and must never become the listing's label (audit 2026-09-11: it set
# `pin_is_precise` on five portals, including for a map VIEW CENTRE).
KNOWN_DECLARED_LABELS = BLURRED_DECLARED_LABELS | PRECISE_DECLARED_LABELS | frozenset(
    {"no_exact_address"}
)


def admissible(claim: Claim, norm: NormalizedClaim | None) -> str | None:
    """-> rejection reason, or None. The ONE admissibility gate, evaluated once in
    `core.resolve` and handed to BIND (03 §3.2 rule 4 / §3.9.1 invariants 5 and 6).

    A `subject_scoped=false` extraction — the remax carousel class — may not rank a
    candidate, drive the hierarchy or fill a NULL, however plausible its value looks.
    """
    if claim.subject_scoped is False:
        return "not_subject_scoped"
    if claim.licence_class == EPHEMERAL:
        return "licence_ephemeral"
    if claim.claim_type in PORTAL_PROPRIETARY_FIELDS:
        return "portal_proprietary_identifier"
    if norm is not None and norm.rejected:
        return f"normalization:{norm.rejections[0]}"
    return None


def trigram_similarity(a: str, b: str) -> float:
    """`pg_trgm.similarity`: |A ∩ B| / |A ∪ B| over the padded trigram sets."""
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _trigrams(value: str) -> frozenset[str]:
    grams: set[str] = set()
    for word in (w for w in value.split() if w):
        padded = f"  {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return frozenset(grams)


# --------------------------------------------------------------------------- constraints


@dataclass(frozen=True, slots=True)
class Constraints:
    """What the claims say about WHERE the listing is, before any match is attempted. It is
    also FILL's fallback source for the four fields the registry may not carry."""

    obec_kods: tuple[int, ...] = ()
    psc: str | None = None
    okres_keys: tuple[str, ...] = ()
    kraj_keys: tuple[str, ...] = ()
    obec_keys: tuple[str, ...] = ()
    cast_obce_keys: tuple[str, ...] = ()
    katuz_keys: tuple[str, ...] = ()
    qualifiers: tuple[str, ...] = ()
    street_key: str | None = None
    street_verbatim: str | None = None
    cislo_domovni: int | None = None
    cislo_orientacni: int | None = None
    znak_orientacniho: str | None = None
    kod_adm: int | None = None
    pin: tuple[float, float] | None = None
    # The Czech Post town from a `postal_town` claim, PSČ prefix stripped. NOT admin-bearing
    # (it names a post office, not an obec) — it is consulted only to break a tie between
    # obce that already share the listing's PSČ.
    postal_town_key: str | None = None
    claim_ids: dict[str, tuple[int, ...]] = field(default_factory=dict)


def collect_constraints(
    claims: Sequence[Claim], normalized: dict[int, NormalizedClaim]
) -> Constraints:
    obec_kods: list[int] = []
    psc: str | None = None
    buckets: dict[str, list[str]] = {
        "okres": [], "kraj": [], "obec": [], "cast_obce": [], "katuz": [], "qualifier": [],
    }
    ids: dict[str, list[int]] = {}
    street_key = street_verbatim = None
    cp = co = kod_adm = None
    znak: str | None = None
    pin: tuple[float, float] | None = None
    postal_town_key: str | None = None

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
            znak = znak or _text_slot(slots, "znak_orientacniho")
        elif t == "house_number_cp":
            cp = cp if cp is not None else _as_int(str(slots.get("cislo_domovni") or ""))
            note("house_number_cp", claim.id)
        elif t == "house_number_co":
            co = co if co is not None else _as_int(
                str(slots.get("cislo_orientacni") or slots.get("cislo_domovni") or "")
            )
            znak = znak or _text_slot(slots, "znak_orientacniho")
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
        elif t == "homonym_qualifier" and key:
            buckets["qualifier"].append(key)
            note("homonym_qualifier", claim.id)
        elif t == "postal_town" and claim.value_text and postal_town_key is None:
            postal_town_key = _postal_town_key(claim.value_text)
            if postal_town_key:
                note("postal_town", claim.id)
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
        qualifiers=tuple(dict.fromkeys(buckets["qualifier"])),
        street_key=street_key,
        street_verbatim=street_verbatim,
        cislo_domovni=cp,
        cislo_orientacni=co,
        znak_orientacniho=znak,
        kod_adm=kod_adm,
        pin=pin,
        postal_town_key=postal_town_key,
        claim_ids={k: tuple(v) for k, v in sorted(ids.items())},
    )


# The four address fields an operator may correct by hand. The survivorship evaluator and
# its policy table are gone, and with them every ranking rule they expressed — except this
# one, which has a live producer (`location_data/operator_corrections.py`) and a standing
# rule behind it: operator curation wins, and the registry does not get to respell it.
OPERATOR_METHOD = "operator_manual"
OPERATOR_FIELDS = ("street_name", "house_number_cp", "house_number_co", "psc")


def operator_fields(claims: Sequence[Claim]) -> dict[str, str]:
    """-> {field: value} for the hand-entered claims among these, lowest claim id first."""
    out: dict[str, str] = {}
    for claim in sorted(claims, key=lambda c: c.id):
        if claim.extraction_method != OPERATOR_METHOD or not claim.value_text:
            continue
        if claim.claim_type in OPERATOR_FIELDS:
            out.setdefault(claim.claim_type, claim.value_text)
    return out


_PSC_PREFIX_RE = re.compile(r"^\s*\d{3}\s?\d{2}\s+")


def _postal_town_key(value: str) -> str | None:
    key = normalize_match_key(_PSC_PREFIX_RE.sub("", value))
    return key or None


def _text_slot(slots: dict[str, Any], name: str) -> str | None:
    value = slots.get(name)
    return str(value) if value else None


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
    """The qualifier ladder. Returns (surviving units, applied qualifiers)."""
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
            filtered = [u for u in surviving if any(q in u.name_norm for q in constraints.qualifiers)]
        if filtered:
            surviving = filtered
            applied.append("homonym_qualifier")

    # The coordinate is a TIE-BREAKER among already-qualified candidates, and only when the
    # pin's own quality is precise. Never the primary disambiguator.
    if constraints.pin and pin_is_precise and len(surviving) > 1:
        covering = registry.containing_obec(*constraints.pin)
        if covering is not None:
            filtered = [u for u in surviving if u.code == covering.code]
            if filtered:
                surviving = filtered
                applied.append("coordinate_tiebreak")

    # A tie that survived every qualifier is answered by the pin's containing obec even when
    # the pin is NOT precise, at `low` confidence. Six obce share PSČ 674 01; ranking them by
    # admin_unit_id served Kožichovice for a pin inside Třebíč on 24,601 bazos rows (audit
    # 2026-09-11). An honest low-confidence answer beats an arbitrary one. The Krásný Les
    # hazard cannot reach this branch: a Mapy geocode is class E and never becomes a claim.
    if constraints.pin and len(surviving) > 1:
        covering = registry.containing_obec(*constraints.pin)
        if covering is not None:
            filtered = [u for u in surviving if u.code == covering.code]
            if filtered:
                surviving = filtered
                applied.append("coordinate_tiebreak_imprecise")

    # Last resort: the Czech Post town named next to the PSČ ("674 01 Třebíč"). Not an admin
    # fact, but among obce that share that PSČ the one carrying the post town's own name is
    # the most probable, and the answer is still capped at `low`.
    if constraints.postal_town_key and len(surviving) > 1:
        filtered = [u for u in surviving if u.name_norm == constraints.postal_town_key]
        if filtered:
            surviving = filtered
            applied.append("postal_town")

    return surviving, applied


# ------------------------------------------------------------------------- the ladder


@dataclass(frozen=True, slots=True)
class _Candidate:
    rung: str
    score: float
    target_kind: str
    granularity: str
    ruian_adm_kod: int | None = None
    ulice_kod: int | None = None
    admin_unit_id: int | None = None
    obec_kod: int | None = None
    street_name: str | None = None
    lat: float | None = None
    lon: float | None = None
    cast_obce_unit_id: int | None = None
    agreed: tuple[str, ...] = ()
    relaxations: tuple[str, ...] = ()
    source_claim_ids: tuple[int, ...] = ()


def bind(
    claims: Sequence[Claim],
    normalized: dict[int, NormalizedClaim],
    ctx: ResolverContext,
    *,
    pin_is_precise: bool = False,
) -> tuple[Binding, Constraints]:
    """BIND. -> (the winner, the constraints FILL still needs)."""
    registry = ctx.registry
    constraints = collect_constraints(claims, normalized)
    out: list[_Candidate] = []

    # ---- constraining obec set (feeds R1-R3; also produces R4/R6 candidates below).
    obec_units: list[AdminUnit] = []
    obec_qualifiers: list[str] = []
    if constraints.obec_kods:
        obec_units = [
            chain[0]
            for k in constraints.obec_kods
            if (chain := registry.admin_chain_by_code("obec", k))
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
        # A PSČ-only set goes through the same qualifier ladder a name-matched set does —
        # several obce share one PSČ, and until 2026-09-11 this branch skipped the ladder.
        by_psc = [
            chain[0]
            for k in registry.obec_codes_for_psc(constraints.psc)
            if (chain := registry.admin_chain_by_code("obec", k))
        ]
        obec_units, applied = qualify_obec_candidates(
            _dedupe_units(by_psc), constraints, registry=registry, pin_is_precise=pin_is_precise
        )
        obec_qualifiers = list(dict.fromkeys(["psc", *applied]))

    constraining_obec_kods = tuple(sorted({u.code for u in obec_units}))

    # ---- R0: a portal-supplied registry key. The prize (bezrealitky `ruianId`).
    if constraints.kod_adm is not None:
        point = registry.address_point(constraints.kod_adm)
        if point is not None:
            out.append(
                _point_candidate(
                    point, rung="R0", agreed=("registry_key", "house_number", "street", "obec"),
                    claim_ids=constraints.claim_ids.get("kod_adm", ()),
                )
            )

    # ---- R1: obec + street + čp/čo.
    if constraining_obec_kods and constraints.street_key and (
        constraints.cislo_domovni or constraints.cislo_orientacni
    ):
        for obec_kod in constraining_obec_kods:
            for point in registry.address_points_by_number(
                obec_kod=obec_kod,
                street_name_norm=constraints.street_key,
                cislo_domovni=constraints.cislo_domovni,
                cislo_orientacni=constraints.cislo_orientacni,
            ):
                agreed = ["house_number", "street", "obec"]
                if constraints.psc == point.psc:
                    agreed.append("psc")
                out.append(
                    _point_candidate(
                        point, rung="R1", agreed=tuple(agreed),
                        claim_ids=_ids(constraints, "street", "house_number_cp", "house_number_co"),
                    )
                )

    # ---- R2 / R3: street inside the constraining obec, exact then typo-tolerant.
    if constraining_obec_kods and constraints.street_key:
        exact_hits = 0
        fuzzy: list[tuple[float, Any]] = []
        for obec_kod in constraining_obec_kods:
            streets = registry.streets_in_obec(obec_kod)
            for street in streets:
                if street.name_norm == constraints.street_key:
                    out.append(_street_candidate(street, "R2", constraints))
                    exact_hits += 1
            if exact_hits:
                continue
            for street in streets:
                sim = trigram_similarity(constraints.street_key, street.name_norm)
                if sim >= _TRGM_THRESHOLD:
                    fuzzy.append((sim, street))
        if not exact_hits and fuzzy:
            fuzzy.sort(key=lambda t: (-t[0], t[1].name_norm))
            best = fuzzy[0][0]
            runner_up = fuzzy[1][0] if len(fuzzy) > 1 else 0.0
            if best - runner_up >= _TRGM_MARGIN or len(fuzzy) == 1:
                out.append(
                    _street_candidate(
                        fuzzy[0][1], "R3", constraints, similarity=best,
                        relaxations=("street_unaccent_fuzzy",),
                    )
                )

    # ---- R4: obec / část obce / quarter by name (the point-set level).
    for unit in obec_units:
        out.append(
            _admin_candidate(
                unit, rung="R4", granularity="obec",
                claim_ids=_ids(constraints, "obec_name", "obec_code"),
                qualifiers=tuple(obec_qualifiers),
            )
        )
    if constraints.cast_obce_keys and constraining_obec_kods:
        for key in constraints.cast_obce_keys:
            for unit in registry.admin_units_by_name(
                key, levels=("cast_obce", "momc", "spravni_obvod", "zsj")
            ):
                if unit.obec_kod is not None and unit.obec_kod not in constraining_obec_kods:
                    continue
                out.append(
                    _admin_candidate(
                        unit, rung="R4", granularity="cast_obce_or_quarter",
                        claim_ids=_ids(constraints, "cast_obce_name"),
                        qualifiers=("obec_constrained",),
                    )
                )

    # ---- R6: PSČ alone.
    if not obec_units and constraints.psc:
        for code in registry.obec_codes_for_psc(constraints.psc):
            chain = registry.admin_chain_by_code("obec", code)
            if chain:
                out.append(
                    _admin_candidate(
                        chain[0], rung="R6", granularity="obec",
                        claim_ids=_ids(constraints, "psc"), qualifiers=("psc",),
                    )
                )

    # ---- R7: coordinate only. DERIVED, never a claim (§3.6.3) — it can only produce an
    # admin-level candidate, never a street or house number.
    #
    # ---- R8: the SLIVER fallback, and the last rung there is. A pin inside no obec polygon
    # at all but within `PIP_SLIVER_TOLERANCE_M` of one is a boundary artifact, not a listing
    # with no town: the honest answer is that obec at LOW confidence. Rule 25's guarantee is a
    # town for every Czech listing, and this is the rung that keeps a border pin from being
    # the exception. It is NOT a dispute — `disputed` stays NULL, because nothing about the
    # row contradicts anything else about it; the pin is simply at the edge.
    if not out and constraints.pin is not None:
        covering = registry.containing_obec(*constraints.pin)
        if covering is not None:
            out.append(
                _admin_candidate(
                    covering, rung="R7", granularity="obec",
                    claim_ids=_ids(constraints, "coordinate"), qualifiers=("reverse_derived",),
                )
            )
        else:
            nearest = registry.nearest_obec_within(
                *constraints.pin, PIP_SLIVER_TOLERANCE_M
            )
            if nearest is not None:
                out.append(
                    _admin_candidate(
                        nearest[0], rung="R8", granularity="obec",
                        claim_ids=_ids(constraints, "coordinate"),
                        qualifiers=("pip_nearest_within_n_m",),
                    )
                )

    if not out:
        return Binding(target_kind="none", granularity="unknown", rung="none"), constraints

    ranked = _rank(out, ctx.granularity_rank)
    top = ranked[0]
    gap = (top.score - ranked[1].score) if len(ranked) > 1 else None
    at_top = [c for c in ranked if c.granularity == top.granularity and c.rung == top.rung]
    ambiguous = (gap is not None and gap < AMBIGUITY_MARGIN) or len(at_top) > 1
    return (
        Binding(
            target_kind=top.target_kind,
            granularity=top.granularity,
            rung=top.rung,
            ruian_adm_kod=top.ruian_adm_kod,
            ulice_kod=top.ulice_kod,
            admin_unit_id=top.admin_unit_id,
            obec_kod=top.obec_kod,
            street_name=top.street_name,
            lat=top.lat,
            lon=top.lon,
            cast_obce_unit_id=top.cast_obce_unit_id,
            agreed=top.agreed,
            relaxations=top.relaxations,
            ambiguous=ambiguous,
            source_claim_ids=top.source_claim_ids,
        ),
        constraints,
    )


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


def _rank(candidates: Sequence[_Candidate], rank: GranularityRank) -> list[_Candidate]:
    return sorted(
        candidates,
        key=lambda c: (
            -c.score,
            -rank.rank(c.granularity),
            c.ruian_adm_kod or 0,
            c.admin_unit_id or 0,
            c.ulice_kod or 0,
        ),
    )


def _point_candidate(
    point, *, rung: str, agreed: tuple[str, ...], claim_ids: tuple[int, ...]
) -> _Candidate:
    return _Candidate(
        rung=rung, score=_RUNG_BASE_SCORE[rung] + 2.0 * len(agreed),
        target_kind="address_point", granularity="address_point",
        ruian_adm_kod=point.kod_adm, ulice_kod=point.ulice_kod, obec_kod=point.obec_kod,
        street_name=point.street_name, lat=point.lat, lon=point.lon,
        cast_obce_unit_id=point.cast_obce_unit_id,
        agreed=agreed, source_claim_ids=claim_ids,
    )


def _street_candidate(
    street, rung: str, constraints: Constraints, *,
    similarity: float | None = None, relaxations: tuple[str, ...] = (),
) -> _Candidate:
    # A house-number claim we could not join to an address point still narrows the street to
    # a segment; without one it is a bare street. RÚIAN streets carry no geometry in the
    # mirror, so a street candidate has no position of its own.
    granularity = "street_segment" if constraints.cislo_domovni else "street"
    return _Candidate(
        rung=rung, score=_RUNG_BASE_SCORE[rung] + (10.0 * similarity if similarity else 0.0),
        target_kind="street", granularity=granularity, ulice_kod=street.code,
        obec_kod=street.obec_kod, street_name=street.name,
        agreed=("street", "obec") if rung == "R2" else ("obec",),
        relaxations=relaxations, source_claim_ids=_ids(constraints, "street"),
    )


def _admin_candidate(
    unit: AdminUnit, *, rung: str, granularity: str,
    claim_ids: tuple[int, ...], qualifiers: tuple[str, ...],
) -> _Candidate:
    agreed = ["obec"] if granularity == "obec" else ["cast_obce", "obec"]
    agreed.extend(q for q in qualifiers if q in AGREEMENT_QUALIFIERS)
    if rung in ("R6", "R7", "R8"):
        # PSČ alone, a bare reverse-geocode or the sliver fallback: the obec is an
        # INFERENCE, not a field that agreed with anything, so it may not count toward the
        # agreement tally.
        agreed = []
    return _Candidate(
        rung=rung, score=_RUNG_BASE_SCORE[rung] + 5.0 * len(qualifiers),
        target_kind="admin_unit", granularity=granularity, admin_unit_id=unit.unit_id,
        obec_kod=unit.obec_kod if granularity != "obec" else unit.code,
        lat=unit.lat, lon=unit.lon, agreed=tuple(dict.fromkeys(agreed)),
        relaxations=qualifiers, source_claim_ids=claim_ids,
    )


# ------------------------------------------------------------------ the pin election


@dataclass(frozen=True, slots=True)
class DeclaredPrecision:
    label: str | None
    blurred: bool
    claim_ids: tuple[int, ...]


def declared_rank(claim: Claim) -> int:
    """0 declared-precise, 1 undeclared, 2 declared-blurred — the pin's own ordering."""
    raw = (claim.declared_precision_label or "").strip().lower()
    if raw in PRECISE_DECLARED_LABELS:
        return 0
    if raw in BLURRED_DECLARED_LABELS or claim.blur_evidence in ("declared", "both"):
        return 2
    return 1


def read_declared_precision(
    claims: Sequence[Claim], *, coordinate_claim_id: int | None = None
) -> DeclaredPrecision:
    """The listing-wide declaration. `coordinate_claim_id`, once the pin is picked, narrows
    the coordinate-borne half to THAT pin: a blurred sibling must not blur the winner."""
    label: str | None = None
    blurred = False
    ids: list[int] = []
    for claim in sorted(claims, key=lambda c: c.id):
        if claim.claim_type not in (
            "precision_declaration", "blur_hint", "uncertainty_geometry", "map_zoom", "coordinate"
        ):
            continue
        if (
            claim.claim_type == "coordinate"
            and coordinate_claim_id is not None
            and claim.id != coordinate_claim_id
        ):
            continue
        if claim.claim_type == "blur_hint":
            blurred = True
            ids.append(claim.id)
            continue
        labelled = (claim.declared_precision_label or "").strip().lower()
        spoken = (claim.value_text or "").strip().lower()
        raw = labelled or (spoken if spoken in KNOWN_DECLARED_LABELS else "")
        # Several portals hang the precision flag on the COORDINATE claim itself (sreality
        # `locality.inaccuracy_type`, mmreality `accurate`), not on a separate row.
        if claim.claim_type == "coordinate" and claim.declared_precision_label:
            ids.append(claim.id)
            label = label or raw
            blurred = blurred or raw in BLURRED_DECLARED_LABELS
        if claim.claim_type in ("precision_declaration", "uncertainty_geometry"):
            ids.append(claim.id)
            if raw:
                label = label or raw
                blurred = blurred or raw in BLURRED_DECLARED_LABELS
        if claim.blur_evidence in ("declared", "both"):
            blurred = True
            ids.append(claim.id)
    return DeclaredPrecision(label=label, blurred=blurred, claim_ids=tuple(sorted(set(ids))))


def elect_pin(claims: Sequence[Claim]) -> Claim | None:
    """WHICH coordinate becomes the pin, ordered by DECLARED QUALITY and only then by claim
    id. A blurred coordinate that merely arrived first used to become the position."""
    eligible = sorted(
        (c for c in claims if c.claim_type == "coordinate" and c.has_position),
        key=lambda c: (declared_rank(c), c.id),
    )
    return eligible[0] if eligible else None


def place(binding: Binding, pin_claim: Claim | None, *, declared: DeclaredPrecision) -> Position:
    """Precedence: registry point > portal pin > admin centroid > none.

    The registry-vs-pin cross-check FLAGS, it never silently picks: beyond
    `REGISTRY_PIN_CONFLICT_M` the registry point stays the position and GRADE caps the
    confidence. Reverse resolution (coordinate → street) is DERIVED, never a claim, so this
    function never invents an address from a pin.
    """
    pin = (pin_claim.lat, pin_claim.lon) if pin_claim else None
    if binding.target_kind == "address_point" and binding.lat is not None:
        return Position(
            lat=binding.lat, lon=binding.lon, origin="registry_point",
            registry_pin_distance_m=distance_between((binding.lat, binding.lon), pin),
            source_claim_ids=binding.source_claim_ids,
        )
    if pin_claim is not None and pin is not None:
        return Position(
            lat=pin[0], lon=pin[1], origin="portal_pin", blurred=declared.blurred,
            source_claim_ids=(pin_claim.id,),
        )
    if binding.lat is not None and binding.lon is not None:
        return Position(
            lat=binding.lat, lon=binding.lon, origin="admin_centroid",
            source_claim_ids=binding.source_claim_ids,
        )
    return Position(lat=None, lon=None, origin="none")
