"""BIND — step 1 of 4: the finest RÚIAN entity the claims justify, and the pin that goes
with it.

Match, don't parse. The gazetteer is closed and finite (3 020 222 address points), so
retrieval substitutes for parsing; parsing exists only to extract CONSTRAINTS that filter
and re-rank. A town name is bound by THE TOWN RULE (v5.5, `bind_towns`): looked up as an
obec, a část obce and a katastrální území, each match climbed to its obec, kept when that obec
carries the listing's PSČ, else when it is an obec within `TOWN_PIN_REACH_M` of the pin, and
only then narrowed by the okres/kraj claims and the pin (the geocode of an ambiguous town name
IS the town centroid, which is how Krásný Les went 100 km wrong — a Mapy geocode is never a
claim). Its regression tests are in `tests/location_data/test_resolver_bind.py`, whose
docstring carries why BIND returns ONE `Binding` (the losers are ranked, never stored) and why
ambiguity is a CONFIDENCE (`low`, served), never a queue.

**Which coordinate becomes the pin is a decision, not an accident of row order.** A listing
can carry several `coordinate` claims — a portal-declared exact pin, a blurred fallback, a
carousel/neighbour pin that is `subject_scoped=false`. The pin is chosen from the ADMISSIBLE
ones ordered by DECLARED QUALITY and only then by claim id.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

from location_data.resolver.composite import (
    MATCH_LEVELS,
    CompositeBind,
    StreetBind,
    match_streets,
    obec_of,
    part_of,
    places_named,
    resolve_locality,
    resolve_street,
    street_match_keys,
    town_code,
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
# Hand-written, not MATCH_LEVELS: a town name never matches a MOMC here ("Praha 4" anchors its
# town through the composite binder).
TOWN_NAME_LEVELS = ("obec", "cast_obce", "katastralni_uzemi")
# A part name is looked up at the match levels and as a KÚ (W5, ruling Q2): every match anchors
# its town, and `composite.part_of` decides the část it binds.
PART_NAME_LEVELS = (*MATCH_LEVELS, "katastralni_uzemi")
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
# The labels that declare the pin an AREA marker (W5, ruling Q3): bazos' "Přibližná lokalita"
# stands on a postal-area point (all 52,185 active bazos pins), so it never places a row the
# words bound. A subset of the blurred labels, so caps and street precedence are unchanged.
AREA_MARKER_LABELS = frozenset({"approximate_location"})
# `accurate` is mmreality's `/accurate: true` branch — the portal asserting the pin IS the
# address, the counterpart of its `regional` label above.
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
    # THE street claim, verbatim: `composite.resolve_street` binds it EXACTLY inside the
    # anchoring obec — one matcher, one answer.
    street_line: str | None = None
    # The domovní číslo and its RÚIAN `typ_so`, TYPED from claim to SQL (D7): "č.ev. 13" is
    # a cottage's evidence number and may only ever match a `č.ev.` point (None: unmarked).
    cislo_domovni: int | None = None
    typ_so: str | None = None
    cislo_orientacni: int | None = None
    kod_adm: int | None = None
    pin: tuple[float, float] | None = None
    claim_ids: dict[str, tuple[int, ...]] = field(default_factory=dict)


def collect_constraints(
    claims: Sequence[Claim], normalized: dict[int, NormalizedClaim],
) -> Constraints:
    """What the claims say, at most one claim per type (the read, W3): nothing here ranks two
    claims of one type. A house-number claim outranks the number written in the street line;
    the pin is THE coordinate."""
    one = {c.claim_type: c for c in claims}
    norms = {t: normalized.get(c.id) for t, c in one.items()}
    ids: dict[str, tuple[int, ...]] = {}

    def read(claim_type: str, kind: str) -> NormalizedClaim | None:
        """The type's normalized claim, noted as the evidence for `kind`."""
        ids[kind] = (one[claim_type].id,)
        return norms[claim_type]

    def slots(norm: NormalizedClaim | None) -> dict[str, Any]:
        return norm.typed_slots if norm else {}

    kod_adm = psc = street_key = street_line = None
    cp = co = typ = None
    pin: tuple[float, float] | None = None
    if (claim := one.get("address_point_id")) is not None and claim.value_text:
        read("address_point_id", "kod_adm")
        kod_adm = _as_int(claim.value_text)
    if "psc" in one and isinstance(value := slots(norms["psc"]).get("psc"), str):
        read("psc", "psc")
        psc = value
    if "house_number_cp" in one:
        cp, typ = house_number(slots(read("house_number_cp", "house_number_cp")))
    if "house_number_co" in one:
        numbers = slots(read("house_number_co", "house_number_co"))
        co = _as_int(str(numbers.get("cislo_orientacni") or numbers.get("cislo_domovni") or ""))
    street = norms.get("street_name")
    if "street_name" in one and not (street and street.rejections):
        line = str(one["street_name"].value_text or "")
        if line.strip():
            read("street_name", "street")
            street_line = line
        street_key = street.value_ascii if street else None
        if cp is None:
            cp, typ = house_number(slots(street))
        if co is None and slots(street).get("cislo_orientacni"):
            co = _as_int(str(slots(street)["cislo_orientacni"]))
    keys: dict[str, tuple[str, ...]] = {}
    obec_lines: tuple[str, ...] = ()
    for t in ("obec_name", "cast_obce_name", "okres_name", "kraj_name"):
        norm = norms.get(t)
        if not (norm and norm.value_ascii) or (t == "obec_name" and norm.rejections):
            continue
        read(t, t)
        keys[t] = (norm.value_ascii,)
        if t == "obec_name":
            line = str(norm.value_cf or one[t].value_text or "")
            obec_lines = (line,) if line else ()
    if (claim := one.get("coordinate")) is not None and claim.has_position:
        read("coordinate", "coordinate")
        pin = (float(claim.lat), float(claim.lon))  # type: ignore[arg-type]

    return Constraints(
        psc=psc,
        okres_keys=keys.get("okres_name", ()),
        kraj_keys=keys.get("kraj_name", ()),
        obec_keys=keys.get("obec_name", ()),
        obec_lines=obec_lines,
        cast_obce_keys=keys.get("cast_obce_name", ()),
        street_key=street_key,
        street_line=street_line,
        cislo_domovni=cp,
        typ_so=typ,
        cislo_orientacni=co,
        kod_adm=kod_adm,
        pin=pin,
        claim_ids=dict(sorted(ids.items())),
    )


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
    qualifier: str | None = None,
) -> tuple[list[AdminUnit], list[str], dict[int, tuple[AdminUnit, ...]]]:
    """THE TOWN RULE (v5.5, D4) for one set of names -> (the towns left, the qualifiers, and
    per town the units its name CLIMBED from — the village that is the part, 1a).
    1. Every match at `levels` climbs to its obec; the towns carrying the listing's PSČ stay.
    2. Else the OBEC-level matches, narrowed by okres/kraj first, then to those in reach of a
       pin worth more than the name: the PSČ vouched for none, or the portal declares the pin
       precise. An undeclared pin is often a placeholder (56 realitymix rows share the centre
       of Czechia): there the name stands, and CHECK flags `pin_outside_obec`.
    3. `narrow_towns` settles what is left: the pin's obec, else the nearest.
    `qualifier` (1b): the "u X" a retry stripped off the name. Where the base climbs to
    several obce and X is one of them, X is the town the advert named."""
    direct: dict[int, AdminUnit] = {}
    climbed: dict[int, list[AdminUnit]] = {}
    for key in keys:
        for unit in registry.admin_units_by_name(key, levels=levels):
            if unit.level == "obec":
                direct.setdefault(unit.code, unit)
                continue
            # The ltree path names the obec for free; the chain is the fallback for a view
            # that does not fill it. A common part name must not cost a round trip per match.
            if (town := town_code(unit, registry)) is not None:
                climbed.setdefault(town, []).append(unit)
    named_directly = set(direct)
    codes = sorted(set(direct) | set(climbed))
    if qualifier:
        # The base keeps only the obce the qualifier names, and those ARE named; when it names
        # none, only the PSČ may settle the base — "Vranovice" alone is not "Vranovice u Břas".
        named = {u.code for u in registry.admin_units_by_name(qualifier, levels=("obec",))}
        kept = [c for c in codes if c in named]
        codes = kept or codes
        direct = {c: t for c in kept if (t := direct.get(c) or _obec_by_code(c, registry))}
    towns: list[AdminUnit] = []
    if constraints.psc:
        carrying = _carrying_psc(codes, direct, constraints.psc, registry)
        towns = [t for c in carrying if (t := direct.get(c) or _obec_by_code(c, registry))]
        if towns:
            towns, applied = narrow_towns(towns, constraints, registry=registry,
                                          pin_is_precise=pin_is_precise,
                                          applied=["psc"] if len(codes) > 1 else [])
    if not towns:
        towns, applied = narrow_towns(
            [t for _, t in sorted(direct.items())], constraints, registry=registry,
            pin_is_precise=pin_is_precise, reach=_reach_of(constraints, pin_is_precise))
    return towns, applied, {t.code: tuple(climbed[t.code]) for t in towns
                            if t.code in climbed and t.code not in named_directly}


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


class Towns(NamedTuple):
    """What `constraining_towns` says. `named` (the advert's OWN town field gave the towns) is
    what Q3 reads; `climbed` holds, per town, the part/KÚ units its name climbed from (1a)."""

    units: list[AdminUnit]
    applied: list[str]
    rung: str
    named: bool
    composite: CompositeBind
    climbed: dict[int, tuple[AdminUnit, ...]]


def constraining_towns(
    constraints: Constraints, registry: RegistryView, *, pin_is_precise: bool
) -> Towns:
    """The towns off the advert's town names, its composite line, the names with their "u X"
    stripped, its part names, then the PSČ — an INFERENCE, so R6 with no agreeing field. S1's
    town-as-street test asks this same function on the same claims, so the two agree about the
    town — except where 1f read a street line that S1 then refuses as a place, which BIND's
    second pass no longer sees."""
    composite = CompositeBind()
    towns, applied, climbed = bind_towns(constraints.obec_keys, TOWN_NAME_LEVELS, constraints,
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
    for key in () if towns else constraints.obec_keys:
        # 1b: "Boudy u Jesenice" is no register name — the village is Boudy, and Jesenice is
        # which of its three. The connector is the single word "u": "nad", "pod" and "v" are
        # parts of real names ("Ústí nad Labem"), never a qualifier to drop. A key that IS a
        # register name ("Chrášťany u Hulína", a KÚ) was refused by the town rule, not unknown.
        base, connector, qualifier = key.rpartition(" u ")
        if (connector and base and qualifier
                and not registry.admin_units_by_name(key, levels=TOWN_NAME_LEVELS)):
            towns, applied, climbed = bind_towns(
                (base,), TOWN_NAME_LEVELS, constraints, registry=registry,
                pin_is_precise=pin_is_precise, qualifier=qualifier)
            if towns:
                applied = [*applied, "qualifier_stripped"]
                break
    if towns:
        return Towns(towns, applied, "R4", True, composite, climbed)
    if constraints.cast_obce_keys:
        towns, applied, _ = bind_towns(constraints.cast_obce_keys, PART_NAME_LEVELS, constraints,
                                       registry=registry, pin_is_precise=pin_is_precise)
        if towns:
            return Towns(towns, applied, "R4", False, composite, {})
    by_psc = _dedupe_units([chain[0] for k in (registry.obec_codes_for_psc(constraints.psc)
                                               if constraints.psc else ())
                            if (chain := registry.admin_chain_by_code("obec", k))])
    line = constraints.street_line
    if line and len(by_psc) > 1:
        # 1f: a PSČ shared by five towns, and a street the advert names in exactly one of them
        # (19120281: 68601, "Na Splávku" in Uherské Hradiště). The pin is the post office's. A
        # line that is also a PLACE of one of them ("Mařatice") settles nothing — asked only
        # of a line some town holds.
        kods = {t.code for t in by_psc}
        holding = {t.code: t for t in by_psc if _holds_street(t, line, registry)}
        if holding and places_named(street_match_keys(line)[1], kods, registry):
            holding = {}
        if len(holding) == 1:
            return Towns(list(holding.values()), ["psc", "street_in_town"], "R6", False,
                         composite, {})
    towns, applied = narrow_towns(by_psc, constraints, registry=registry,
                                  pin_is_precise=pin_is_precise, applied=["psc"])
    return Towns(towns, applied, "R6", False, composite, {})


def _holds_street(town: AdminUnit, line: str, registry: RegistryView) -> bool:
    """A street of the town reached on the EXACT tier — the tolerant fold settles no town."""
    found, tier = match_streets(line, (town.code,), registry)
    return bool(found) and tier == "exact"


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
    place_line: str | None = None,
) -> tuple[Binding, Constraints]:
    """BIND. -> (the winner, the constraints FILL still needs). `place_line` is the street
    claim S1 refused as a place name (`town_as_street`): 1c asks which part it names."""
    registry = ctx.registry
    constraints = collect_constraints(claims, normalized)
    out: list[_Candidate] = []

    towns = constraining_towns(constraints, registry, pin_is_precise=pin_is_precise)
    obec_units = towns.units
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

    # THE street claim (W18). `composite.resolve_street` matches it EXACTLY against the
    # register inside the anchoring obec, in two tiers: a full-name match wins outright, the
    # type-word-tolerant fold is consulted only when nothing matched exactly, and a line that
    # reaches two distinct streets binds nothing at all. v5.6 deleted the split on separators: its
    # one user was bazos' whole headline, and the text reading names the street itself.
    line = (
        resolve_street(constraints.street_line, constraining_obec_kods, registry,
                       place_line=place_line)
        if constraining_obec_kods and (constraints.street_line or place_line)
        else StreetBind()
    )

    # ---- R4: the část obce / quarter, from four places. The advert's part claim and the part
    # a composite line named (the rung and qualifier a CLAIMED část gets: a registry bind grades
    # by the unit it landed on, not by the string that pointed at it); since v5.7 the village a
    # town name climbed from (1a: "Hracholusky" is a část of Úlice) and the part a street line
    # names (1c). Both inherit how their town was chosen (a tie-break's low confidence); the
    # climbed part is published for ONE surviving town only and counts the town claim's
    # agreements, since it is one claim. A unit named by several sources is ONE candidate. A
    # part claim matches like a town name: a KÚ binds the část it spells, a MOMC none (W5).
    said: list[_Part] = []
    for key in constraints.cast_obce_keys if constraining_obec_kods else ():
        for unit in registry.admin_units_by_name(key, levels=PART_NAME_LEVELS):
            town = town_code(unit, registry)
            if town in constraining_obec_kods and (claimed := part_of(unit, town, registry)):
                said.append(_Part(claimed, _ids(constraints, "cast_obce_name"),
                                  ("obec_constrained",)))
    if towns.composite.part is not None:
        said.append(_Part(towns.composite.part, _ids(constraints, "obec_name"),
                          ("obec_constrained",)))
    found: list[_Part] = []
    weak = tuple(q for q in towns.applied if q in LOW_CONFIDENCE_QUALIFIERS)
    if len(obec_units) == 1:
        town = obec_units[0]
        if climbed := climbed_part(town, towns.climbed.get(town.code, ()), registry):
            found.append(_Part(
                climbed, _ids(constraints, "obec_name"),
                ("obec_constrained", "climbed_from_town_name", *weak),
                agreed=_admin_agreed("obec", towns.rung, tuple(towns.applied))))
    if line.named_part is not None:
        found.append(_Part(line.named_part, _ids(constraints, "street"),
                           ("obec_constrained", "street_names_part", *weak)))
    parts: list[AdminUnit] = []
    for named in _merge_parts(*_reconcile_parts(said, found)):
        parts.append(named.unit)
        out.append(_admin_candidate(named.unit, rung="R4", granularity="cast_obce_or_quarter",
                                    claim_ids=named.claim_ids, qualifiers=named.qualifiers,
                                    agreed=named.agreed))
    part = parts[0] if len(parts) == 1 else None

    # 1d: one register name, several streets. With ONE bound part, the street with a door in
    # it; without one, the one a claim WITH diacritics is spelled like (`composite`, rule 3).
    # 18625515 ("Hanusova", part Michle) is the first; an accent-free claim is never the second.
    # The two contradicting ("Hanušova" in Michle, where only Hanusova has doors), or a bound
    # part holding no candidate, is a tie neither may break: no street.
    relaxed: tuple[str, ...] = ()
    if line.reason == "ambiguous_streets":
        inside = [c for c in line.candidates if part is not None and c.street is not None
                  and part.unit_id in getattr(registry.street_point(c.street), "part_unit_ids", ())]
        spelled = [c for c in line.candidates if c.spelled]
        if len(inside) == 1 and (len(spelled) != 1 or spelled[0].street == inside[0].street):
            line = replace(inside[0], named_part=line.named_part)
            relaxed = ("street_part_tiebreak",)
        elif len(spelled) == 1 and len(inside) != 1 and (part is None or inside):
            line = replace(spelled[0], named_part=line.named_part)

    # ---- R1 / R2: a bound street reaches R1 when there is a house number and R2 otherwise.
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
                    point, rung="R1", agreed=tuple(agreed), relaxations=relaxed,
                    claim_ids=_ids(constraints, "street", "house_number_cp",
                                   "house_number_co")))
        if not points:
            out.append(_street_candidate(line.street, "R2", constraints, relaxations=relaxed))

    # ---- R3: the typo-tolerant rung, and the ONE place a street may be bound by similarity
    # rather than by identity. It runs only when nothing bound exactly. (W18 kept it off
    # bazos' whole headline with `claim_confidence: low`; W3's reading names the street
    # itself. The pilot never reviewed R3; an offline register check (09-30) of its 42 bazos streets
    # found 40 exact and one near-miss that is right ("náměstí Fr. Rasche"), so the gate went.)
    bound_exactly = any(c.target_kind == "street" or c.rung in ("R0", "R1") for c in out)
    if (
        constraining_obec_kods
        and constraints.street_key
        and not bound_exactly
        # A tie is not a typo. When the exact binder FAILED CLOSED on two register rows the
        # claim could mean, letting the fuzzy rung pick one of the very candidates just
        # refused would undo the refusal — so R3 runs only where nothing matched at all. A
        # line that names a part of the town is never a street, fuzzily or otherwise (1c).
        and line.reason not in ("ambiguous_streets", "street_names_part")
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

    # ---- R4: the obec by name (the point-set level).
    for unit in obec_units:
        out.append(
            _admin_candidate(
                unit, rung=towns.rung, granularity="obec",
                claim_ids=_ids(constraints, "obec_name", "psc"),
                qualifiers=tuple(towns.applied),
            )
        )

    # ---- R1 WITHOUT a street (D7): the house number inside the ONE bound část obce, where a
    # č.p. (and a č.ev.) is unique by law — 475 of 478 sampled sreality numbers are, against
    # 247 across the whole town. Only for a listing that names no street (809 of 814 of those
    # rows): a number written after a street the register could not bind is that street's,
    # often its č.o. The pin must not contradict the point — absent, declared blurred, or
    # within `REGISTRY_PIN_CONFLICT_M` — because `place()` moves the row there unflagged.
    town = obec_of(part, registry) if part is not None else None
    if (town is not None and constraints.cislo_domovni is not None
            and not constraints.street_line and not any(
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
        return Binding(target_kind="none", rung="none"), constraints

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
    # the advert claims. Otherwise (several parts, or a town only the PSČ or a part claim
    # supplied) it keeps the claimed část where it has doors (W5, ruling Q2), else none.
    part_unit_id = top.cast_obce_unit_id
    if top.rung == "R2" and street_point:
        doors = street_point.part_unit_ids
        if towns.named and len(doors) == 1:
            part_unit_id = doors[0]
        elif part is not None and part.unit_id in doors:
            part_unit_id = part.unit_id
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


def climbed_part(
    town: AdminUnit, climbed: Sequence[AdminUnit], registry: RegistryView
) -> AdminUnit | None:
    """1a: the ONE část obce a town name climbed from (`part_of`: the part itself, or the část a
    KÚ's qualifier-free name spells). None for two, and for the town's eponymous main part, which
    adds nothing and overstates the precision (18827725: "Nové Hrady u Skutče" is Nové Hrady's
    own KÚ). A claimed part keeps no such rule: 38,095 rows are bound to their main část."""
    found = {p.unit_id: p for unit in climbed if (p := part_of(unit, town.code, registry))}
    part = next(iter(found.values())) if len(found) == 1 else None
    return None if part is None or part.name_norm == town.name_norm else part


@dataclass(frozen=True, slots=True)
class _Part:
    """One source's word for a part: the unit, the claims behind it, its qualifiers, and the
    agreeing fields when they are not the default (a climbed part is ONE claim, the town's)."""
    unit: AdminUnit
    claim_ids: tuple[int, ...]
    qualifiers: tuple[str, ...]
    agreed: tuple[str, ...] | None = None


def _reconcile_parts(said: list[_Part], found: list[_Part]) -> tuple[list[_Part], list[_Part]]:
    """The climbed and street-named parts fail closed as two streets do: two of them in one
    town, or one a claimed part contradicts, publish no part at all (19009248: "Dlouhé"
    against "Rzy")."""
    peers = {p.unit.unit_id for p in said}
    if (len({p.unit.unit_id for p in found}) != len({p.unit.obec_kod for p in found})
            or peers and any(p.unit.unit_id not in peers for p in found)):
        return [], []
    return said, found


def _merge_parts(said: list[_Part], found: list[_Part]) -> list[_Part]:
    """ONE candidate per unit: two sources naming one část are one answer, not a tie."""
    merged: dict[int, _Part] = {}
    for part in (*said, *found):
        known = merged.get(part.unit.unit_id)
        merged[part.unit.unit_id] = part if known is None else _Part(
            part.unit, tuple(sorted({*known.claim_ids, *part.claim_ids})),
            tuple(dict.fromkeys((*known.qualifiers, *part.qualifiers))),
            None if known.agreed is None or part.agreed is None else known.agreed)
    return list(merged.values())


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
    point: AddressPoint, *, rung: str, agreed: tuple[str, ...], claim_ids: tuple[int, ...],
    relaxations: tuple[str, ...] = (),
) -> _Candidate:
    return _Candidate(
        rung=rung, score=_RUNG_BASE_SCORE[rung] + 2.0 * len(agreed),
        target_kind="address_point", granularity="address_point",
        ruian_adm_kod=point.kod_adm, ulice_kod=point.ulice_kod, obec_kod=point.obec_kod,
        street_name=point.street_name, lat=point.lat, lon=point.lon,
        cast_obce_unit_id=point.cast_obce_unit_id,
        agreed=agreed, relaxations=relaxations, source_claim_ids=claim_ids,
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
    agreed: tuple[str, ...] | None = None,
) -> _Candidate:
    # No lat/lon: a `_Candidate`'s position is the position of a POINT it bound, and a unit
    # is not one. Where the unit sits is FILL's answer, off the chain (W2-a3).
    return _Candidate(
        rung=rung, score=_RUNG_BASE_SCORE[rung] + 5.0 * len(qualifiers),
        target_kind="admin_unit", granularity=granularity, admin_unit_id=unit.unit_id,
        obec_kod=unit.obec_kod if granularity != "obec" else unit.code,
        agreed=_admin_agreed(granularity, rung, qualifiers) if agreed is None else agreed,
        relaxations=qualifiers, source_claim_ids=claim_ids,
    )


def _admin_agreed(granularity: str, rung: str, qualifiers: tuple[str, ...]) -> tuple[str, ...]:
    if rung in INFERENCE_RUNGS:
        # The entity was INFERRED, not named — a PSČ lookup, a reverse geocode, the sliver
        # fallback, a bare region. Nothing agreed with it, so it contributes no field.
        return ()
    agreed = ["obec"] if granularity == "obec" else [granularity, "obec"]
    agreed.extend(q for q in qualifiers if q in AGREEMENT_QUALIFIERS)
    return tuple(dict.fromkeys(agreed))


# ------------------------------------------------------------------ the pin and its declaration


@dataclass(frozen=True, slots=True)
class DeclaredPrecision:
    label: str | None
    blurred: bool
    claim_ids: tuple[int, ...]

    @property
    def area(self) -> bool:
        """The pin is an AREA marker (W5): it never places a row the words bound."""
        return self.label in AREA_MARKER_LABELS


def the_pin(claims: Sequence[Claim]) -> Claim | None:
    """THE coordinate claim with a position (the read returns at most one per type, W3)."""
    return next((c for c in claims if c.claim_type == "coordinate" and c.has_position), None)


def read_declared_precision(claims: Sequence[Claim]) -> DeclaredPrecision:
    """The listing's declaration: the precision declaration's label, else the label a portal
    hangs on the coordinate itself. Either one stating blur blurs the pin."""
    one = {c.claim_type: c for c in claims}
    label: str | None = None
    blurred = False
    ids: list[int] = []
    for claim_type in ("precision_declaration", "coordinate"):
        claim = one.get(claim_type)
        if claim is None:
            continue
        labelled = (claim.declared_precision_label or "").strip().lower()
        spoken = (claim.value_text or "").strip().lower()
        raw = labelled or (spoken if spoken in KNOWN_DECLARED_LABELS else "")
        if claim_type == "precision_declaration" or claim.declared_precision_label:
            ids.append(claim.id)
            if raw:
                label = label or raw
                blurred = blurred or raw in BLURRED_DECLARED_LABELS
        if claim.blur_evidence in ("declared", "both"):
            blurred = True
            ids.append(claim.id)
    return DeclaredPrecision(label=label, blurred=blurred, claim_ids=tuple(sorted(set(ids))))


def pin_inside_bound_obec(
    binding: Binding, pin_claim: Claim | None, registry: RegistryView
) -> bool | None:
    """The ONE containment question `place()` and CHECK both read: does the pin lie inside the
    obec the CLAIMS bound? None when it is not asked — no pin, no bound town (a region-only
    bind), or a town the pin itself established (R7/R8, where the comparison is circular). The
    drain pre-answers `containing_obec` for every claim coordinate in the slice, so this is one
    cached read per listing."""
    pin = pin_claim.point if pin_claim is not None else None
    if pin is None or binding.obec_kod is None or binding.pin_derived:
        return None
    covering = registry.containing_obec(*pin)
    return covering is not None and covering.code == binding.obec_kod


def place(
    binding: Binding,
    pin_claim: Claim | None,
    *,
    declared: DeclaredPrecision,
    pin_inside_bound_obec: bool | None = None,
) -> Position:
    """The ONE precedence rule (W18): registry ADDRESS POINT > bound STREET point > portal pin >
    FILL's unit point (an unplaced `Position` here). One truth (v5.10, ruling Q3): where the words
    bound the row, a pin may place it only inside the bound town (`pin_inside_bound_obec`, None =
    not asked) and never as an AREA marker; without words (R7/R8) it names its town and stays. A
    pin that may not place the row is still evidence the branches measure and CHECK flags. The
    street beats a placeable pin that is declared blurred or farther than
    `max(REGISTRY_PIN_CONFLICT_M, extent)` from its centroid (a street is not a point); an EXACT
    pin losing that way is `pin_overridden`. The grain is not decided here (`grade.grain_of`)."""
    pin = pin_claim.point if pin_claim is not None else None
    words = binding.bound and not binding.pin_derived
    may_place = pin is not None and (
        not words or (pin_inside_bound_obec is True and not declared.area))
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
        if not may_place or declared.blurred or off_street:
            return Position(
                lat=binding.lat, lon=binding.lon, origin="street_point",
                registry_pin_distance_m=distance, extent_m=binding.street_extent_m,
                pin_overridden=bool(off_street and not declared.blurred),
                source_claim_ids=binding.source_claim_ids,
            )
    # A pin that places the row carries neither the distance nor the extent: both describe a
    # point the REGISTER placed, and grading a corroborated pin by them would cap a row at
    # `medium` for sitting 400 m along a 1.7 km street.
    if may_place:
        return Position(
            lat=pin[0], lon=pin[1], origin="portal_pin", blurred=declared.blurred,
            source_claim_ids=(pin_claim.id,),  # type: ignore[union-attr]
        )
    return Position(lat=None, lon=None, origin="none")
