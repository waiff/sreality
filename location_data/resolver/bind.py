"""BIND — step 1 of 4: the finest RÚIAN entity the claims justify, and the pin that goes
with it.

Match, don't parse. The gazetteer is closed and finite (3 020 222 address points), so
retrieval substitutes for parsing; parsing exists only to extract CONSTRAINTS that filter
and re-rank. Homonym disambiguation resolves names LOCALLY and HIERARCHICALLY inside the
constraining parent, in descending discriminating power: PSČ, okres/kraj claims, cadastral
territory, `homonym_qualifier`, and only then the coordinate — as a tie-breaker among
already-qualified candidates, never as the primary disambiguator (the geocode of an
ambiguous town name IS the town centroid, which is how Krásný Les went 100 km wrong). Its
three named regression tests are in `tests/location_data/test_resolver_bind.py`.

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

from location_data.resolver.composite import (
    CompositeBind,
    StreetBind,
    resolve_locality,
    resolve_street,
)
from location_data.resolver.normalize import STREET_LINE_SEPARATOR
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
LOW_CONFIDENCE_QUALIFIERS = frozenset(
    {"coordinate_tiebreak_imprecise", "postal_town", "pip_nearest_within_n_m"}
)
# Qualifiers that ARE independent fields agreeing with the entity, and therefore count
# toward GRADE's agreement tally. A coordinate tie-break is deliberately not one of them.
AGREEMENT_QUALIFIERS = frozenset(
    {"obec_code", "psc", "okres", "kraj", "cadastral_territory", "homonym_qualifier"}
)

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
    """What the claims say about WHERE the listing is, before any match is attempted. It is
    also FILL's fallback source for the four fields the registry may not carry."""

    obec_kods: tuple[int, ...] = ()
    psc: str | None = None
    okres_keys: tuple[str, ...] = ()
    kraj_keys: tuple[str, ...] = ()
    obec_keys: tuple[str, ...] = ()
    # The same `obec_name` claims UNSPLIT and unfolded — what the portal actually wrote. The
    # match key folds every separator to a space, so the composite binder cannot read the
    # line off `obec_keys`: "Praha 4 - Podolí" and "Praha 4 Podolí" normalise identically.
    obec_lines: tuple[str, ...] = ()
    cast_obce_keys: tuple[str, ...] = ()
    katuz_keys: tuple[str, ...] = ()
    qualifiers: tuple[str, ...] = ()
    # S1's match key, and the ONE input R3 (the trigram rung) is allowed to run on. It is set
    # only for a claim that is one NAME and that the contract does not declare
    # `claim_confidence: low` — see `street_lines` (W18).
    street_key: str | None = None
    # EVERY street claim, verbatim. `composite.resolve_street` splits each on the portals' own
    # separators and binds the segments EXACTLY inside the anchoring obec, so a value that
    # carries no separator is simply one segment and takes the identical path — one matcher,
    # one answer, and no rung whose reach depends on whether the portal wrote a comma.
    street_lines: tuple[str, ...] = ()
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
    obec_kods: list[int] = []
    psc: str | None = None
    buckets: dict[str, list[str]] = {
        "okres": [], "kraj": [], "obec": [], "cast_obce": [], "katuz": [], "qualifier": [],
    }
    ids: dict[str, list[int]] = {}
    obec_lines: list[str] = []
    street_lines: list[str] = []
    street_key = None
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
            raw = str(claim.value_text or "")
            if raw.strip():
                street_lines.append(raw)
                note("street", claim.id)
            if STREET_LINE_SEPARATOR.search(raw):
                # A LINE's number slots belong to whatever segment ends the string
                # ("…, Mladá Boleslav"), not to the street, so they are not read here — the
                # binder takes them off the segment that actually bound (W18).
                continue
            if cp is None and slots.get("cislo_domovni"):
                cp = _as_int(str(slots["cislo_domovni"]))
            if co is None and slots.get("cislo_orientacni"):
                co = _as_int(str(slots["cislo_orientacni"]))
            znak = znak or _text_slot(slots, "znak_orientacniho")
            if key and claim.claim_confidence != "low":
                # R3's input. A claim the CONTRACT calls `low` is a headline, not an address
                # field, and a trigram run over prose is how "Byt Slunečná" binds Slunečná
                # while "Prodej domu Slunečná" (0.429 similarity) binds nothing — coverage
                # decided by title length, and a wrong street whenever the prose happens to
                # score. The contract declares the quality; the resolver obeys it, and no
                # portal is named here.
                street_key = street_key or key
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
            obec_lines.append(str((norm.value_cf if norm else None) or claim.value_text or ""))
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
        elif t == "coordinate" and claim.has_position:
            elected = claim.id == pin_claim_id if pin_claim_id is not None else pin is None
            if elected:
                pin = (float(claim.lat), float(claim.lon))  # type: ignore[arg-type]
                note("coordinate", claim.id)

    return Constraints(
        obec_kods=tuple(dict.fromkeys(obec_kods)),
        psc=psc,
        okres_keys=tuple(dict.fromkeys(buckets["okres"])),
        kraj_keys=tuple(dict.fromkeys(buckets["kraj"])),
        obec_keys=tuple(dict.fromkeys(buckets["obec"])),
        obec_lines=tuple(dict.fromkeys(line for line in obec_lines if line)),
        cast_obce_keys=tuple(dict.fromkeys(buckets["cast_obce"])),
        katuz_keys=tuple(dict.fromkeys(buckets["katuz"])),
        qualifiers=tuple(dict.fromkeys(buckets["qualifier"])),
        street_key=street_key,
        street_lines=tuple(dict.fromkeys(line for line in street_lines if line)),
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
    # The register row a street candidate came off, carried so the WINNER — and only the
    # winner — can be asked where it is (W18). One round trip per listing that binds a
    # street, never one per candidate.
    street: Street | None = None
    # The house number the LINE segment that produced this candidate carried, and only that
    # one. It reaches the answer row through `Binding` when this candidate WINS and never
    # otherwise: mutating `constraints` with it (the first cut) lent the number to whatever
    # street the ranking happened to pick, across claims.
    house_number_cp: str | None = None
    house_number_co: str | None = None
    agreed: tuple[str, ...] = ()
    relaxations: tuple[str, ...] = ()
    source_claim_ids: tuple[int, ...] = ()


def bind(
    claims: Sequence[Claim],
    normalized: dict[int, NormalizedClaim],
    ctx: ResolverContext,
    *,
    pin_is_precise: bool = False,
    pin_claim_id: int | None = None,
) -> tuple[Binding, Constraints]:
    """BIND. -> (the winner, the constraints FILL still needs)."""
    registry = ctx.registry
    constraints = collect_constraints(claims, normalized, pin_claim_id=pin_claim_id)
    out: list[_Candidate] = []

    # ---- constraining obec set (feeds R1-R3; also produces R4/R6 candidates below).
    obec_units: list[AdminUnit] = []
    obec_qualifiers: list[str] = []
    # Which rung the obec set came off. A set seeded from a PSČ is an INFERENCE — the obec
    # was not named, it was looked up — so it grades at R6 and contributes no agreeing field.
    # Counting "obec" and "psc" as two fields there was one fact counted twice, and it graded
    # a PSČ-only bind `high`.
    obec_rung = "R4"
    composite = CompositeBind()
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
        obec_rung = "R6"

    if not obec_units and not constraints.obec_kods and constraints.obec_lines:
        # W9: the line named no obec, so the REGISTER is asked what it DOES name — the whole
        # string at every level first, then its parts scoped by the anchoring town, and
        # nothing at all when that is ambiguous. "Praha 4 - Podolí" lands here and comes back
        # as Praha + Podolí, both spelled by RÚIAN. The town it anchors on joins the
        # constraining set, so a street claim on the same listing still reaches R1-R3.
        composite = first_composite_bind(constraints.obec_lines, registry)
        if composite.bound:
            obec_units = [composite.obec]  # type: ignore[list-item]
            obec_qualifiers = ["composite_locality"]
            obec_rung = "R4"

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

    # ---- R1 / R2: THE street claim, whatever shape the portal wrote it in (W18).
    #
    # One binder for all of them. `composite.resolve_street` splits every claim on the
    # portals' own separators — a value with no separator is one segment — and matches each
    # segment EXACTLY against the register inside the anchoring obec, in two tiers: a full-name
    # match wins outright, the type-word-tolerant fold is consulted only when nothing matched
    # exactly, and two distinct streets across the segments bind nothing at all.
    #
    # It was two paths for one round and that was the defect: whether a claim reached the
    # segment binder or the single-name one turned on whether the portal happened to write a
    # comma, so a comma-less headline ("Byt Slunečná") fell through to the trigram rung and
    # bound a street out of prose, while a longer one ("Prodej domu Slunečná", similarity
    # 0.429) bound nothing. Coverage decided by title length is not a rule.
    #
    # A bound street reaches R1 when there is a house number and R2 otherwise — what bound is a
    # register row either way, and the SHAPE of the string that pointed at it is not a grade.
    line = (
        resolve_street(constraints.street_lines, constraining_obec_kods, registry)
        if constraining_obec_kods and constraints.street_lines
        else StreetBind()
    )
    if line.street is not None:
        # The segment's own number first; a listing-wide `house_number_*` claim behind it
        # (the portals that state the street and the číslo in separate fields). A LINE's
        # number never reaches the constraints, so it cannot be lent to another claim.
        cislo_domovni = line.cislo_domovni or constraints.cislo_domovni
        cislo_orientacni = line.cislo_orientacni or constraints.cislo_orientacni
        points = (
            registry.address_points_by_number(
                obec_kod=line.street.obec_kod,
                street_name_norm=line.street.name_norm,
                cislo_domovni=cislo_domovni,
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
            out.append(_street_candidate(
                line.street, "R2", constraints,
                cislo_domovni=line.cislo_domovni,
                cislo_orientacni=line.cislo_orientacni,
                znak_orientacniho=line.znak_orientacniho))

    # ---- R3: the typo-tolerant rung, and the ONE place a street may be bound by similarity
    # rather than by identity. It runs only when nothing bound exactly AND the contract calls
    # this claim an address field — `constraints.street_key` is set for no other kind. A
    # trigram over a headline is how prose reaches a street it does not name.
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
                claim_ids=_ids(constraints, "obec_name", "obec_code", "psc"),
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

    if composite.part is not None:
        # The part the composite line named, at the rung and with the qualifier a separately
        # CLAIMED část obce gets: a registry bind grades by the unit it landed on, not by the
        # shape of the string that pointed at it.
        out.append(
            _admin_candidate(
                composite.part, rung="R4", granularity="cast_obce_or_quarter",
                claim_ids=_ids(constraints, "obec_name"),
                qualifiers=("obec_constrained",),
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
            cast_obce_unit_id=top.cast_obce_unit_id,
            street_extent_m=None if street_point is None else street_point.extent_m,
            house_number_cp=top.house_number_cp,
            house_number_co=top.house_number_co,
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


def _street_candidate(
    street: Street, rung: str, constraints: Constraints, *,
    similarity: float | None = None, relaxations: tuple[str, ...] = (),
    cislo_domovni: int | None = None, cislo_orientacni: int | None = None,
    znak_orientacniho: str | None = None,
) -> _Candidate:
    # A house-number claim we could not join to an address point still narrows the street to
    # a segment; without one it is a bare street. RÚIAN streets carry no geometry in the
    # mirror, so a street candidate has no position of its own.
    #
    # The number is the CANDIDATE's, passed in by the line binder from the segment that bound
    # this street, and it falls back to the listing-wide constraint only for a claim that was
    # one name (where S1 split the two out of that same claim). A line's number may never
    # reach a street bound from another claim.
    cp = cislo_domovni if cislo_domovni is not None else constraints.cislo_domovni
    co = cislo_orientacni if cislo_orientacni is not None else constraints.cislo_orientacni
    znak = znak_orientacniho if cislo_orientacni is not None else constraints.znak_orientacniho
    granularity = "street_segment" if cp else "street"
    return _Candidate(
        rung=rung, score=_RUNG_BASE_SCORE[rung] + (10.0 * similarity if similarity else 0.0),
        target_kind="street", granularity=granularity, ulice_kod=street.code,
        obec_kod=street.obec_kod, street_name=street.name, street=street,
        house_number_cp=None if cp is None else str(cp),
        house_number_co=None if co is None else f"{co}{znak or ''}",
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
