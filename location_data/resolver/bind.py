"""BIND — step 1 of 4: the finest RÚIAN entity the claims justify, and the pin that goes
with it.

Match, don't parse. The gazetteer is closed and finite (3 020 222 address points), so
retrieval substitutes for parsing; parsing exists only to extract CONSTRAINTS that filter
and re-rank. A town name is bound by THE TOWN RULE (v5.5, `bind_towns`): looked up as an
obec, a část obce and a katastrální území, each match climbed to its obec, kept when that obec
carries the listing's PSČ, else when it is an obec within `TOWN_PIN_REACH_M` of the pin, and
only then narrowed by the okres/kraj claims and the pin (the geocode of an ambiguous town name
IS the town centroid, which is how Krásný Les went 100 km wrong — a Mapy geocode is never a
claim). Its regression tests are in `tests/location_data/test_resolver_bind.py`.

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

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from location_data.resolver.composite import (
    PART_LEVELS,
    CompositeBind,
    StreetBind,
    obec_of,
    resolve_locality,
    resolve_street,
)
from location_data.resolver.geo import distance_between
from location_data.resolver.normalize import TYP_CP, house_number
from location_data.resolver.types import (
    AddressPoint,
    AdminUnit,
    Binding,
    Claim,
    GranularityRank,
    NormalizedClaim,
    Position,
    RegistryView,
    ResolverContext,
    Street,
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
LOW_CONFIDENCE_QUALIFIERS = frozenset({"coordinate_tiebreak_imprecise", "pip_nearest_within_n_m"})
# Qualifiers that ARE independent fields agreeing with the entity, and therefore count
# toward GRADE's agreement tally. A coordinate tie-break is deliberately not one of them.
AGREEMENT_QUALIFIERS = frozenset({"psc", "okres", "kraj"})

# THE TOWN RULE (v5.5, D4). A town name is looked up at these three levels and every match
# climbs to its obec: "Černotín" in PSČ 334 43 is a část obce of Dnešice, "Zlaté Hory v
# Jeseníkách" a katastrální území of Zlaté Hory. A part or a KÚ binds its town only through the
# PSČ — or as the unique part a composite line names, whose town `_admits` tests like any other.
TOWN_NAME_LEVELS = ("obec", "cast_obce", "katastralni_uzemi")
# How far from the pin a town the PSČ does not vouch for may lie. The 28 verified true towns of
# the wrong-town sample all lie within 25.1 km of theirs; 18841980's "Hory" lay 329 km away.
TOWN_PIN_REACH_M = 40_000.0

_RUNG_BASE_SCORE = {"R0": 100.0, "R1": 90.0, "R2": 70.0, "R3": 60.0, "R4": 45.0,
                    "R6": 35.0, "R7": 25.0, "R8": 15.0, "R9": 5.0}

# The rungs whose entity was INFERRED rather than named: a PSČ lookup, a reverse geocode, the
# sliver fallback, a region with no town under it. Nothing "agreed" with them, so they
# contribute no field to GRADE's tally and land at `low`.
INFERENCE_RUNGS = frozenset({"R6", "R7", "R8", "R9"})

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
        # W1-c: the labels the nine slim contracts actually emit. bazos' maps-anchor title
        # ("Přibližná lokalita" -> `approximate_location`) and maxima's two non-point feature
        # geometries, which ARE the portal drawing its own imprecision.
        "approximate_location", "linestring", "circle",
    }
)
# `accurate` is mmreality's `/accurate: true` branch — the portal asserting the pin IS the
# address, the counterpart of its `regional` label above. It is deliberately NOT in
# `grade.DECLARED_CAP`: membership here RANKS the pin against a blurred sibling, which is
# what the flag is for, while a cap row would additionally CERTIFY a granularity the portal's
# own boolean does not predict.
PRECISE_DECLARED_LABELS = frozenset(
    {"gps", "address", "exact", "presna", "rooftop", "ruian", "accurate"})
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
    """What the claims say about WHERE the listing is, before any match is attempted. FILL
    reads one field of it, the PSČ, as the fallback the registry may not carry."""

    psc: str | None = None
    okres_keys: tuple[str, ...] = ()
    kraj_keys: tuple[str, ...] = ()
    obec_keys: tuple[str, ...] = ()
    # The same `obec_name` claims UNSPLIT and unfolded — what the portal actually wrote. The
    # match key folds every separator to a space, so the composite binder cannot read the
    # line off `obec_keys`: "Praha 4 - Podolí" and "Praha 4 Podolí" normalise identically.
    obec_lines: tuple[str, ...] = ()
    cast_obce_keys: tuple[str, ...] = ()
    # S1's match key, and the ONE input R3 (the trigram rung) is allowed to run on.
    street_key: str | None = None
    # EVERY street claim, verbatim: `composite.resolve_street` binds each EXACTLY inside the
    # anchoring obec — one matcher, one answer.
    street_lines: tuple[str, ...] = ()
    # The domovní číslo and its RÚIAN `typ_so`, TYPED from claim to SQL (D7): "č.ev. 13" is
    # a cottage's evidence number and may only ever match a `č.ev.` point (None: unmarked).
    cislo_domovni: int | None = None
    typ_so: str | None = None
    cislo_orientacni: int | None = None
    kod_adm: int | None = None
    pin: tuple[float, float] | None = None
    claim_ids: dict[str, tuple[int, ...]] = field(default_factory=dict)


def collect_constraints(
    claims: Sequence[Claim],
    normalized: dict[int, NormalizedClaim],
    *,
    pin_claim_id: int | None = None,
) -> Constraints:
    """`pin_claim_id` names the coordinate `elect_pin` chose. It matters: without it this
    took the FIRST coordinate by id while the position took the best-DECLARED one, so a
    listing carrying a blurred pin and a precise one reverse-geocoded its town from one and
    published `geom` from the other — and R7/R8 are pin-derived, so CHECK skips the
    containment test and the row ships clean with its town and its pin 300 km apart."""
    psc: str | None = None
    buckets: dict[str, list[str]] = {"okres": [], "kraj": [], "obec": [], "cast_obce": []}
    ids: dict[str, list[int]] = {}
    obec_lines: list[str] = []
    street_lines: list[str] = []
    street_key = None
    cp = co = kod_adm = typ = None
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
        elif t == "psc":
            value = slots.get("psc")
            if isinstance(value, str):
                psc = psc or value
                note("psc", claim.id)
        elif t == "street_name" and not rejected:
            raw = str(claim.value_text or "")
            if raw.strip():
                street_lines.append(raw)
                note("street", claim.id)
            if cp is None:
                cp, typ = house_number(slots)
            if co is None and slots.get("cislo_orientacni"):
                co = _as_int(str(slots["cislo_orientacni"]))
            street_key = street_key or key
        elif t == "house_number_cp":
            if cp is None:
                cp, typ = house_number(slots)
            note("house_number_cp", claim.id)
        elif t == "house_number_co":
            co = co if co is not None else _as_int(
                str(slots.get("cislo_orientacni") or slots.get("cislo_domovni") or "")
            )
            note("house_number_co", claim.id)
        elif t == "obec_name" and key and not rejected:
            buckets["obec"].append(key)
            obec_lines.append(str((norm.value_cf if norm else None) or claim.value_text or ""))
            note("obec_name", claim.id)
        elif t in ("cast_obce_name", "okres_name", "kraj_name") and key:
            buckets[t.removesuffix("_name")].append(key)
            note(t, claim.id)
        elif t == "coordinate" and claim.has_position:
            elected = claim.id == pin_claim_id if pin_claim_id is not None else pin is None
            if elected:
                pin = (float(claim.lat), float(claim.lon))  # type: ignore[arg-type]
                note("coordinate", claim.id)

    return Constraints(
        psc=psc,
        okres_keys=tuple(dict.fromkeys(buckets["okres"])),
        kraj_keys=tuple(dict.fromkeys(buckets["kraj"])),
        obec_keys=tuple(dict.fromkeys(buckets["obec"])),
        obec_lines=tuple(dict.fromkeys(line for line in obec_lines if line)),
        cast_obce_keys=tuple(dict.fromkeys(buckets["cast_obce"])),
        street_key=street_key,
        street_lines=tuple(dict.fromkeys(line for line in street_lines if line)),
        cislo_domovni=cp,
        typ_so=typ,
        cislo_orientacni=co,
        kod_adm=kod_adm,
        pin=pin,
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


def _as_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------- the town rule


def bind_towns(
    keys: Sequence[str],
    levels: Sequence[str],
    constraints: Constraints,
    *,
    registry: RegistryView,
    pin_is_precise: bool,
) -> tuple[list[AdminUnit], list[str]]:
    """THE TOWN RULE (v5.5, D4) for one set of names -> (the towns left, the qualifiers).
    1. Every match at `levels` climbs to its obec; the towns carrying the listing's PSČ stay.
    2. Else the OBEC-level matches, narrowed by okres/kraj first, then to those in reach of a
       pin worth more than the name: the PSČ vouched for none, or the portal declares the pin
       precise. An undeclared pin is often a placeholder (56 realitymix rows share the centre
       of Czechia): there the name stands, and CHECK flags `pin_outside_obec`.
    3. `narrow_towns` settles what is left: the pin's obec, else the nearest."""
    direct: dict[int, AdminUnit] = {}
    climbed: set[int] = set()
    for key in keys:
        for unit in registry.admin_units_by_name(key, levels=levels):
            if unit.level == "obec":
                direct.setdefault(unit.code, unit)
                continue
            # The ltree path names the obec for free; the chain is the fallback for a view
            # that does not fill it. A common part name must not cost a round trip per match.
            town = unit.obec_kod or getattr(obec_of(unit, registry), "code", None)
            if town is not None:
                climbed.add(town)
    codes = sorted(set(direct) | climbed)
    if constraints.psc:
        carrying = _carrying_psc(codes, direct, constraints.psc, registry)
        towns = [t for c in carrying if (t := direct.get(c) or _obec_by_code(c, registry))]
        if towns:
            return narrow_towns(towns, constraints, registry=registry,
                                pin_is_precise=pin_is_precise,
                                applied=["psc"] if len(codes) > 1 else [])
    return narrow_towns([t for _, t in sorted(direct.items())], constraints, registry=registry,
                        pin_is_precise=pin_is_precise, reach=_reach_of(constraints, pin_is_precise))


def narrow_towns(
    towns: Sequence[AdminUnit],
    constraints: Constraints,
    *,
    registry: RegistryView,
    pin_is_precise: bool,
    applied: list[str] | None = None,
    reach: tuple[float, float] | None = None,
) -> tuple[list[AdminUnit], list[str]]:
    """The okres/kraj claims, `reach` (step 2), then the pin — which REPLACES the lowest-id pick
    wherever there is one; without a pin a tie stays a tie and grades `low`. Only a
    declared-precise pin settles a tie at full confidence."""
    applied = list(applied or [])
    surviving = list(towns)
    for keys, attr, label in (
        (constraints.okres_keys, "okres_kod", "okres"),
        (constraints.kraj_keys, "kraj_kod", "kraj"),
    ):
        if not keys or len(surviving) <= 1:
            continue
        wanted = {u.code for key in keys for u in registry.admin_units_by_name(key, levels=(label,))}
        filtered = [u for u in surviving if getattr(u, attr) in wanted]
        if filtered:
            surviving = filtered
            applied.append(label)
    surviving = [u for u in surviving if _reaches(u, reach, registry)]
    pin = constraints.pin
    if pin is not None and len(surviving) > 1:
        covering = registry.containing_obec(*pin)
        inside = [u for u in surviving if covering is not None and u.code == covering.code]
        applied.append("coordinate_tiebreak" if inside and pin_is_precise
                       else "coordinate_tiebreak_imprecise")
        surviving = inside or [min(surviving, key=lambda u: (
            _town_distance(u, pin, registry), u.unit_id))]
    return surviving, applied


def _admits(
    town: AdminUnit, constraints: Constraints, registry: RegistryView, pin_is_precise: bool
) -> bool:
    """Steps 1-2 for the ONE town a composite line anchored on."""
    if constraints.psc and _carrying_psc([town.code], {town.code: town}, constraints.psc,
                                         registry):
        return True
    return _reaches(town, _reach_of(constraints, pin_is_precise), registry)


def _reach_of(constraints: Constraints, pin_is_precise: bool) -> tuple[float, float] | None:
    return constraints.pin if constraints.psc or pin_is_precise else None


def _carrying_psc(
    codes: Sequence[int], direct: dict[int, AdminUnit], psc: str, registry: RegistryView
) -> list[int]:
    """The towns whose delivery area includes the PSČ. A name-index obec row answers it off
    its own `psc_set`; a town climbed to from a part or a KÚ asks the address points."""
    served: set[int] | None = None
    out: list[int] = []
    for code in codes:
        if psc not in getattr(direct.get(code), "psc_set", ()):
            served = set(registry.obec_codes_for_psc(psc)) if served is None else served
            if code not in served:
                continue
        out.append(code)
    return out


def _obec_by_code(code: int, registry: RegistryView) -> AdminUnit | None:
    chain = registry.admin_chain_by_code("obec", code)
    return chain[0] if chain else None


def _reaches(town: AdminUnit, pin: tuple[float, float] | None, registry: RegistryView) -> bool:
    return pin is None or _town_distance(town, pin, registry) <= TOWN_PIN_REACH_M


def _town_distance(town: AdminUnit, pin: tuple[float, float], registry: RegistryView) -> float:
    """Pin to the town's own point — the chain's, because a name-index row carries none."""
    chain = registry.admin_chain(town.unit_id)
    point = (chain[0].lat, chain[0].lon) if chain and chain[0].lat is not None else None
    distance = distance_between(point, pin)  # type: ignore[arg-type]
    return math.inf if distance is None else distance


def constraining_towns(
    constraints: Constraints, registry: RegistryView, *, pin_is_precise: bool
) -> tuple[list[AdminUnit], list[str], str, bool, CompositeBind]:
    """-> (towns, qualifiers, rung, named, composite line) off the advert's town names, its
    composite line, its part names, then the PSČ — an INFERENCE, so R6 with no agreeing field.
    `named` (the advert's OWN town field gave it) is what Q3 reads. S1's town-as-street test
    asks this same function, so the two cannot disagree about the town."""
    composite = CompositeBind()
    towns, applied = bind_towns(constraints.obec_keys, TOWN_NAME_LEVELS, constraints,
                                registry=registry, pin_is_precise=pin_is_precise)
    if not towns and constraints.obec_lines:
        # W9: the line named no obec, so the REGISTER is asked what it DOES name — the whole
        # string at every level first, then its parts scoped by the anchoring town, and
        # nothing at all when that is ambiguous. "Praha 4 - Podolí" comes back as Praha +
        # Podolí, both spelled by RÚIAN. The anchor must pass the PSČ-or-reach test like any
        # named town (v5.5): 18841980's slug split to a "Hory" 329 km from its pin.
        found = first_composite_bind(constraints.obec_lines, registry)
        if found.obec is not None and _admits(found.obec, constraints, registry, pin_is_precise):
            composite, towns, applied = found, [found.obec], ["composite_locality"]
    if towns:
        return towns, applied, "R4", True, composite
    if constraints.cast_obce_keys:
        towns, applied = bind_towns(constraints.cast_obce_keys, PART_LEVELS, constraints,
                                    registry=registry, pin_is_precise=pin_is_precise)
        if towns:
            return towns, applied, "R4", False, composite
    by_psc = [chain[0] for k in (registry.obec_codes_for_psc(constraints.psc)
                                 if constraints.psc else ())
              if (chain := registry.admin_chain_by_code("obec", k))]
    towns, applied = narrow_towns(_dedupe_units(by_psc), constraints, registry=registry,
                                  pin_is_precise=pin_is_precise, applied=["psc"])
    return towns, applied, "R6", False, composite


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
    # The register row a street candidate came off, carried so the WINNER — and only the
    # winner — can be asked where it is (W18). One round trip per listing that binds a
    # street, never one per candidate.
    street: Street | None = None
    agreed: tuple[str, ...] = ()
    relaxations: tuple[str, ...] = ()
    source_claim_ids: tuple[int, ...] = ()


def bind(
    claims: Sequence[Claim],
    normalized: dict[int, NormalizedClaim],
    ctx: ResolverContext,
    *,
    pin_is_precise: bool = False,
    pin_blurred: bool = False,
    pin_claim_id: int | None = None,
) -> tuple[Binding, Constraints]:
    """BIND. -> (the winner, the constraints FILL still needs)."""
    registry = ctx.registry
    constraints = collect_constraints(claims, normalized, pin_claim_id=pin_claim_id)
    out: list[_Candidate] = []

    obec_units, obec_qualifiers, obec_rung, named, composite = constraining_towns(
        constraints, registry, pin_is_precise=pin_is_precise)
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

    # ---- R1 / R2: THE street claim (W18). `composite.resolve_street` matches every street
    # claim EXACTLY against the register inside the anchoring obec, in two tiers: a full-name
    # match wins outright, the type-word-tolerant fold is consulted only when nothing matched
    # exactly, and two distinct streets bind nothing at all. A bound street reaches R1 when
    # there is a house number and R2 otherwise. v5.6 deleted the split on separators: its one
    # user was bazos' whole headline, and the text reading names the street itself.
    line = (
        resolve_street(constraints.street_lines, constraining_obec_kods, registry)
        if constraining_obec_kods and constraints.street_lines
        else StreetBind()
    )
    if line.street is not None:
        # The street claim's own number first; a listing-wide `house_number_*` claim behind it
        # (the portals that state the street and the číslo in separate fields). The number
        # travels WITH its type, so a č.ev. can never join a č.p. of the same digits.
        cislo_domovni, typ_so = ((line.cislo_domovni, line.typ_so) if line.cislo_domovni
                                 else (constraints.cislo_domovni, constraints.typ_so))
        cislo_orientacni = line.cislo_orientacni or constraints.cislo_orientacni
        points = _typed(
            registry.address_points_by_number(
                obec_kod=line.street.obec_kod,
                street_name_norm=line.street.name_norm,
                cislo_domovni=cislo_domovni,
                typ_so=typ_so,
                cislo_orientacni=cislo_orientacni,
            )
            if cislo_domovni or cislo_orientacni
            else []
        )
        for point in points:
            agreed = ["house_number", "street", "obec"]
            if constraints.psc == point.psc:
                agreed.append("psc")
            out.append(
                _point_candidate(
                    point, rung="R1", agreed=tuple(agreed),
                    claim_ids=_ids(constraints, "street", "house_number_cp",
                                   "house_number_co")))
        if not points:
            out.append(_street_candidate(line.street, "R2", constraints))

    # ---- R3: the typo-tolerant rung, and the ONE place a street may be bound by similarity
    # rather than by identity. It runs only when nothing bound exactly. (W18 kept it off
    # bazos' whole headline with `claim_confidence: low`; W3's reading names the street
    # itself, and the pilot found no R3-only bind to refuse, so the gate is gone.)
    bound_exactly = any(c.target_kind == "street" or c.rung in ("R0", "R1") for c in out)
    if (
        constraining_obec_kods
        and constraints.street_key
        and not bound_exactly
        # A tie is not a typo. When the exact binder FAILED CLOSED on two register rows the
        # claim could mean, letting the fuzzy rung pick one of the very candidates just
        # refused would undo the refusal — so R3 runs only where nothing matched at all.
        and line.reason != "ambiguous_streets"
    ):
        fuzzy: list[tuple[float, Any]] = []
        for obec_kod in constraining_obec_kods:
            for street in registry.streets_in_obec(obec_kod):
                sim = trigram_similarity(constraints.street_key, street.name_norm)
                if sim >= _TRGM_THRESHOLD:
                    fuzzy.append((sim, street))
        if fuzzy:
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
                unit, rung=obec_rung, granularity="obec",
                claim_ids=_ids(constraints, "obec_name", "psc"),
                qualifiers=tuple(obec_qualifiers),
            )
        )
    parts: list[AdminUnit] = []
    if constraints.cast_obce_keys and constraining_obec_kods:
        for key in constraints.cast_obce_keys:
            for unit in registry.admin_units_by_name(key, levels=(*PART_LEVELS, "zsj")):
                if unit.obec_kod is None or unit.obec_kod in constraining_obec_kods:
                    parts.append(unit)
                    out.append(_admin_candidate(
                        unit, rung="R4", granularity="cast_obce_or_quarter",
                        claim_ids=_ids(constraints, "cast_obce_name"),
                        qualifiers=("obec_constrained",)))
    if composite.part is not None:
        # The part the composite line named, at the rung and with the qualifier a separately
        # CLAIMED část obce gets: a registry bind grades by the unit it landed on, not by the
        # shape of the string that pointed at it.
        parts.append(composite.part)
        out.append(_admin_candidate(
            composite.part, rung="R4", granularity="cast_obce_or_quarter",
            claim_ids=_ids(constraints, "obec_name"), qualifiers=("obec_constrained",)))

    # ---- R1 WITHOUT a street (D7): the house number inside the ONE bound část obce, where a
    # č.p. (and a č.ev.) is unique by law — 475 of 478 sampled sreality numbers are, against
    # 247 across the whole town. Only for a listing that names no street (809 of 814 of those
    # rows): a number written after a street the register could not bind is that street's,
    # often its č.o. The pin must not contradict the point — absent, declared blurred, or
    # within `REGISTRY_PIN_CONFLICT_M` — because `place()` moves the row there unflagged.
    cast_parts = {u.unit_id: u for u in parts if u.level == "cast_obce"}
    part = next(iter(cast_parts.values())) if len(cast_parts) == 1 else None
    town = obec_of(part, registry) if part is not None else None
    if (town is not None and constraints.cislo_domovni is not None
            and not constraints.street_lines and not any(
                c.target_kind == "address_point" for c in out)):
        points = _typed(registry.address_points_by_number(
            obec_kod=town.code, street_name_norm=None,
            cislo_domovni=constraints.cislo_domovni, typ_so=constraints.typ_so,
            cislo_orientacni=constraints.cislo_orientacni, cast_obce_unit_id=part.unit_id))
        if len(points) == 1 and _pin_agrees(points[0], constraints.pin, blurred=pin_blurred):
            out.append(_point_candidate(
                points[0], rung="R1", agreed=("house_number", "cast_obce", "obec"),
                claim_ids=_ids(constraints, "cast_obce_name", "house_number_cp",
                               "house_number_co", "street")))

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

    # ---- R9: the region alone, and the last thing the chain has to say. maxima and
    # mmreality emit an okres name with no town, and "we know the okres" is a real answer at
    # a real rung — strictly better than `undetermined`, which is what an unbound region used
    # to collapse to. Low confidence, hierarchy filled ABOVE it by the ordinary chain read.
    if not out:
        for keys, level in ((constraints.okres_keys, "okres"), (constraints.kraj_keys, "kraj")):
            for key in keys:
                for unit in registry.admin_units_by_name(key, levels=(level,)):
                    out.append(
                        _admin_candidate(
                            unit, rung="R9", granularity=level,
                            claim_ids=_ids(constraints, f"{level}_name"),
                            qualifiers=("region_only",),
                        )
                    )
            if out:
                break

    if not out:
        return Binding(target_kind="none", granularity="unknown", rung="none"), constraints

    ranked = _rank(out, ctx.granularity_rank)
    top = ranked[0]
    # W18: a bound street gets a POSITION, off its own address points. Asked HERE, after the
    # ranking, so the mirror answers it once per listing rather than once per candidate —
    # `_street_candidate` carries the register row for exactly this. A street with no address
    # points keeps the pre-W18 behaviour: a name, and no position of its own.
    street_point = (
        registry.street_point(top.street)
        if top.target_kind == "street" and top.street is not None
        else None
    )
    # Q3 (operator ruling 2026-09-30): a street bound EXACTLY (R2) in a town the advert's town
    # field names takes the REGISTER's part — the one část obce all its doors lie in — whatever
    # the advert claims. Across several parts: none (RÚIAN draws no část polygon to test a pin
    # against). A town only the PSČ or a part claim supplied lends its street no part.
    part_unit_id = top.cast_obce_unit_id
    if named and top.rung == "R2" and street_point and len(street_point.part_unit_ids) == 1:
        part_unit_id = street_point.part_unit_ids[0]
    # The margin compares LIKE WITH LIKE. Comparing the top candidate against the next row
    # whatever it is compared it against its own ANCESTOR: a quarter (45 + 5) ties the obec
    # that contains it (45 + 5 for the portal's own obec code), the gap is 0 and the answer
    # grades `low` — so supplying a RÚIAN obec code alongside the quarter made the row score
    # WORSE than naming the town in prose. One answer containing another is not two answers.
    rivals = [c for c in ranked[1:] if c.granularity == top.granularity]
    gap = (top.score - rivals[0].score) if rivals else None
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
            lat=top.lat if street_point is None else street_point.lat,
            lon=top.lon if street_point is None else street_point.lon,
            cast_obce_unit_id=part_unit_id,
            street_extent_m=None if street_point is None else street_point.extent_m,
            street_katastr_kod=None if street_point is None else street_point.katastr_kod,
            agreed=top.agreed,
            relaxations=top.relaxations,
            ambiguous=ambiguous,
            source_claim_ids=top.source_claim_ids,
        ),
        constraints,
    )


def first_composite_bind(lines: Sequence[str], registry: RegistryView) -> CompositeBind:
    """The first locality line the register can place. Lines are already deduped and in
    claim-id order, so this is deterministic; a line that binds nothing carries its reason
    forward for the caller that wants to say why."""
    unbound = CompositeBind()
    for line in lines:
        found = resolve_locality(line, registry)
        if found.bound:
            return found
        if unbound.reason == "no_match":
            unbound = found
    return unbound


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


def _typed(points: Sequence[AddressPoint]) -> list[AddressPoint]:
    """An unmarked number is a č.p. where the scope holds one, else a č.ev. (D7) — a marked
    one already came back as its own type only."""
    return [p for p in points if p.typ_so == TYP_CP] or list(points)


def _pin_agrees(
    point: AddressPoint, pin: tuple[float, float] | None, *, blurred: bool
) -> bool:
    if pin is None or blurred or point.lat is None or point.lon is None:
        return True
    return distance_between((point.lat, point.lon), pin) <= REGISTRY_PIN_CONFLICT_M  # type: ignore[operator]


def _street_candidate(
    street: Street, rung: str, constraints: Constraints, *,
    similarity: float | None = None, relaxations: tuple[str, ...] = (),
) -> _Candidate:
    # A street and nothing finer (D7): a number that joined no address point is not published
    # — the `street_segment` grain it used to buy turned "Praha 8" into č.p. 8. RÚIAN streets
    # carry no geometry in the mirror, so a street candidate has no position of its own.
    return _Candidate(
        rung=rung, score=_RUNG_BASE_SCORE[rung] + (10.0 * similarity if similarity else 0.0),
        target_kind="street", granularity="street", ulice_kod=street.code,
        obec_kod=street.obec_kod, street_name=street.name, street=street,
        agreed=("street", "obec") if rung == "R2" else ("obec",),
        relaxations=relaxations, source_claim_ids=_ids(constraints, "street"),
    )


def _admin_candidate(
    unit: AdminUnit, *, rung: str, granularity: str,
    claim_ids: tuple[int, ...], qualifiers: tuple[str, ...],
) -> _Candidate:
    agreed = ["obec"] if granularity == "obec" else [granularity, "obec"]
    agreed.extend(q for q in qualifiers if q in AGREEMENT_QUALIFIERS)
    if rung in INFERENCE_RUNGS:
        # The entity was INFERRED, not named — a PSČ lookup, a reverse geocode, the sliver
        # fallback, a bare region. Nothing agreed with it, so it contributes no field.
        agreed = []
    # No lat/lon: a `_Candidate`'s position is the position of a POINT it bound, and a unit
    # is not one. Where the unit sits is FILL's answer, off the chain (W2-a3).
    return _Candidate(
        rung=rung, score=_RUNG_BASE_SCORE[rung] + 5.0 * len(qualifiers),
        target_kind="admin_unit", granularity=granularity, admin_unit_id=unit.unit_id,
        obec_kod=unit.obec_kod if granularity != "obec" else unit.code,
        agreed=tuple(dict.fromkeys(agreed)),
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
    """The ONE precedence rule (W18), in order:

        registry ADDRESS POINT  >  bound STREET point  >  portal pin  >  (FILL's unit point)

    with the street beating the pin only when the pin cannot be trusted over it, which is
    exactly three states:

      * there is NO pin;
      * the pin is DECLARED blurred or approximate — bazos stamps every one of its pins
        "Přibližná lokalita", so a street the ad NAMES is strictly better evidence than a
        coordinate the portal itself says is fuzzy;
      * the pin lies farther than `max(REGISTRY_PIN_CONFLICT_M, the street's extent)` from
        the street's centroid. The extent is in the threshold because a street is not a
        point: Jiráskova in Mladá Boleslav spans 1,727 m, and a pin 800 m from its centre is
        still on it.

    An EXACT pin that loses to the street is the one case that is a DISAGREEMENT rather than
    a precedence — the ad's two statements about where it is do not fit — so the position is
    stamped `pin_overridden`, which CHECK turns into `disputed='pin_off_street'` and GRADE
    reads as a ceiling of `medium`. An exact pin that AGREES with the street keeps the
    position: the pin is the finer of two true answers.

    WHICH POINT WINS HERE DOES NOT DECIDE THE GRAIN, and deliberately not. A portal's
    declared precision is a statement about its own COORDINATE, so `grade.grade` applies the
    `DECLARED_CAP` ladder only to a grain the PIN established (R7/R8) — never to one a
    register bind established, whichever point this function elected. Keying it on the
    elected point would make a pin that AGREES with the bound street grade coarser than one
    that contradicts it.

    The registry-vs-pin cross-check FLAGS, it never silently picks: beyond
    `REGISTRY_PIN_CONFLICT_M` the registry point stays the position and GRADE caps the
    confidence. Reverse resolution (coordinate → street) is DERIVED, never a claim, so this
    function never invents an address from a pin.

    BIND places what the CLAIMS place. A row left unplaced here is placed by `fill.position`
    off the hierarchy chain (W2-a3) — the admin-centroid branch used to live here and read
    `AdminUnit.lat`, which meant it read `ruian_admin_units.definition_point`, a column the
    loader has never written: it looked like a fallback and was dead in production on every
    one of the 8,706 towned rows that shipped `geom NULL`.
    """
    pin = (pin_claim.lat, pin_claim.lon) if pin_claim else None
    if binding.target_kind == "address_point" and binding.lat is not None:
        return Position(
            lat=binding.lat, lon=binding.lon, origin="registry_point",
            registry_pin_distance_m=distance_between((binding.lat, binding.lon), pin),
            source_claim_ids=binding.source_claim_ids,
        )
    if binding.target_kind == "street" and binding.lat is not None:
        extent = binding.street_extent_m or 0.0
        distance = distance_between((binding.lat, binding.lon), pin)
        off_street = distance is not None and distance > max(REGISTRY_PIN_CONFLICT_M, extent)
        if pin is None or declared.blurred or off_street:
            return Position(
                lat=binding.lat, lon=binding.lon, origin="street_point",
                registry_pin_distance_m=distance, extent_m=binding.street_extent_m,
                pin_overridden=bool(off_street and not declared.blurred),
                source_claim_ids=binding.source_claim_ids,
            )
        # The pin AGREES with the street, so it stays the position — the finer of two true
        # answers. It carries neither the distance nor the extent: both exist to describe a
        # point the REGISTER placed, and grading a corroborated pin by them would cap a row
        # at `medium` for sitting 400 m along a 1.7 km street.
        return Position(
            lat=pin[0], lon=pin[1], origin="portal_pin", blurred=declared.blurred,
            source_claim_ids=(pin_claim.id,),  # type: ignore[union-attr]
        )
    if pin_claim is not None and pin is not None:
        return Position(
            lat=pin[0], lon=pin[1], origin="portal_pin", blurred=declared.blurred,
            source_claim_ids=(pin_claim.id,),
        )
    return Position(lat=None, lon=None, origin="none")
