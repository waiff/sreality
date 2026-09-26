"""FILL — step 2 of 4: the hierarchy, joined off the bound ids, and the position it implies.

One rule, and it removes a whole class of bug: **administrative names and codes come from
the RÚIAN chain, never from a claim.** A portal that spells the town "Praha 6", "Praha-6"
or "Praha 6 - Vokovice" contributes a MATCH KEY to BIND and nothing else; what gets served
is the registry's own name for the entity BIND landed on. The nine portals' `locality`
semantics are incompatible with each other and this is where that stops mattering.

`admin_chain` returns the unit ITSELF ahead of its ancestors, so a bound address point with
a `cast_obce` costs exactly one chain read for kraj → okres → obec → část obce. Before
W2-a the quarter was filled only on the point-in-polygon branch (`cast_obce_for_point`, a
250 m KNN over 3 M address points), so every street and obec match lost its quarter
— 2026-09-11 audit, resolver-v4. Filling it off the bound entity fixes that by construction
and deletes the query.

FILL also places the row when BIND's pin election did not (W2-a3): **the portal pin when one
was admissible, else the finest bound unit's own registry point.** 29 % of towned rows
(8,706 of 29,892, measured 2026-09-12 08:05Z) carried `geom NULL` because the only position
the resolver published was a pin, and a listing bound by NAME has none — so a row that knows
its town would have vanished from W3's map, which re-sources lat/lng from `listing_location`
with no fallback. The point is the registry's, so the granularity, the confidence and the
radius are all unchanged: the LEVEL is what tells a reader how coarse the position is, and
`disputed` is never set by this path — a town centre is not a disagreement with anything.

Four fields may fall back to a claim when the registry has none: `street_name`,
`house_number_cp`, `house_number_co`, `psc`. Preserve-if-null, never overwrite — a claimed
value that DISAGREES with the registry is not silently replaced, the registry simply wins
where it has an answer (on a registry-bound row the official street form `nám. Budovatelů`
beats the portal's `Budovatelů`). The ONE thing the registry does not get to respell is an
OPERATOR correction, which is also the only survivorship rule that outlived the policy
table: `bind.operator_fields` is the whole of it.

`katastr_kod` (PR-B, v5.4) is ONE rule under the same contract: **the single KÚ of the BOUND
registry entity, else NULL** — a KÚ (or a ZSJ inside one) on the bound unit's chain; the one
KÚ of a one-KÚ obec, which every entity inside it lies in; an address point's own KÚ; a street
or část obce whose every RÚIAN door lies in one KÚ (the Q7 door rule). A portal pin never
decides it, whatever the portal labels it: 66,165 idnes `no_exact_address` and 13,176 sreality
`not_address` pins read as "precise" (`core.pin_is_precise`), so a pin-based KÚ would be a
guess. Nothing else is NULL-masked specially: a foreign row loses it with every other code in
`core`, and a disputed row keeps its bound entity's.
"""

from __future__ import annotations

from location_data.resolver.bind import Constraints, operator_fields
from location_data.resolver.types import (
    AddressPoint,
    AdminUnit,
    Binding,
    Fill,
    Position,
    RegistryView,
)

_LEVEL_FIELDS = {
    "kraj": ("kraj_kod", "kraj_name"),
    "okres": ("okres_kod", "okres_name"),
    "obec": ("obec_kod", "obec_name"),
    "cast_obce": ("cast_obce_kod", "cast_obce_name"),
    # A Prague/Brno městský obvod is the quarter for serving purposes when no ČástObce
    # sits on the chain; it never overwrites one that does.
    "momc": ("cast_obce_kod", "cast_obce_name"),
}

# The two chain levels no listing may be placed at. Nothing binds them — they are the tail
# of somebody's ancestry — and the walk below would otherwise answer "the centre of the
# Czech Republic" for a row whose own levels happened to carry no polygon, at an obec's 1 km
# radius. A row with no placeable ancestor keeps `geom NULL`, which is the true answer.
_UNPLACEABLE_LEVELS = frozenset({"stat", "region_soudrznosti"})


def fill(
    binding: Binding,
    constraints: Constraints,
    registry: RegistryView,
    *,
    operator: dict[str, str] | None = None,
) -> Fill:
    """-> the fifteen hierarchy/address values of the answer row, and its registry point."""
    operator = operator or {}
    values: dict[str, object] = {}
    chain = _chain(binding, registry)
    for unit in chain:
        mapping = _LEVEL_FIELDS.get(unit.level)
        if mapping is None:
            continue
        kod_field, name_field = mapping
        values.setdefault(kod_field, unit.code)
        values.setdefault(name_field, unit.name)

    point = (
        registry.address_point(binding.ruian_adm_kod)
        if binding.ruian_adm_kod is not None
        else None
    )
    lat, lon = _registry_point(point, chain)
    return Fill(
        kraj_kod=values.get("kraj_kod"),          # type: ignore[arg-type]
        okres_kod=values.get("okres_kod"),        # type: ignore[arg-type]
        obec_kod=values.get("obec_kod"),          # type: ignore[arg-type]
        cast_obce_kod=values.get("cast_obce_kod"),  # type: ignore[arg-type]
        ulice_kod=binding.ulice_kod,
        ruian_adm_kod=binding.ruian_adm_kod,
        kraj_name=values.get("kraj_name"),        # type: ignore[arg-type]
        okres_name=values.get("okres_name"),      # type: ignore[arg-type]
        obec_name=values.get("obec_name"),        # type: ignore[arg-type]
        cast_obce_name=values.get("cast_obce_name"),  # type: ignore[arg-type]
        # W18: the street is the REGISTER's or it is nothing. `binding.street_name` is set
        # only on a row that bound one (R0/R1 off the address point, R2/R3 off
        # `ruian_streets`), so an unbound claim text no longer reaches the answer row — the
        # 1,864 rows that carried a street name with `ulice_kod` NULL fall back to their
        # část obce or their town. One rule for all nine portals, and the deliberate cost of
        # it is coverage: a real street the mirror does not hold is dropped rather than
        # served as a name nothing can be joined to, filtered on, or de-duplicated by.
        street_name=(operator.get("street_name") or binding.street_name),
        house_number_cp=operator.get("house_number_cp") or _cp(point, constraints, binding),
        house_number_co=operator.get("house_number_co") or _co(point, constraints, binding),
        psc=operator.get("psc")
        or (point.psc if point is not None and point.psc else constraints.psc),
        lat=lat,
        lon=lon,
        katastr_kod=_katastr_kod(binding, chain, point, registry),
    )


def position(filled: Fill, binding: Binding) -> Position:
    """The position FILL publishes when BIND's pin election came back empty (W2-a3).

    It is a REGISTRY point, so nothing about it can contradict the row: CHECK's containment
    tests are asked of a portal pin only, because a point that came out of the mirror is
    inside its own town by construction. `origin` is `admin_centroid` for anything but an
    address point — GRADE reads it to decide that a pin corroborates an address, and a unit
    point corroborates nothing.
    """
    if filled.lat is None or filled.lon is None:
        return Position(lat=None, lon=None, origin="none")
    return Position(
        lat=filled.lat,
        lon=filled.lon,
        origin="registry_point" if binding.target_kind == "address_point" else "admin_centroid",
        source_claim_ids=binding.source_claim_ids,
    )


def _chain(binding: Binding, registry: RegistryView) -> tuple[AdminUnit, ...]:
    """The registry read FILL makes for every row (the only other is `part_katastr_kod`, for
    a část bound in a multi-KÚ obec). The finest bound unit first, so its own level lands on
    the row alongside every ancestor, and a one-KÚ obec carries its sole KÚ."""
    if binding.cast_obce_unit_id is not None:
        chain = tuple(registry.admin_chain(binding.cast_obce_unit_id))
        if chain:
            return chain
    if binding.admin_unit_id is not None:
        return tuple(registry.admin_chain(binding.admin_unit_id))
    if binding.obec_kod is not None:
        return tuple(registry.admin_chain_by_code("obec", binding.obec_kod))
    return ()


def _katastr_kod(
    binding: Binding,
    chain: tuple[AdminUnit, ...],
    point: AddressPoint | None,
    registry: RegistryView,
) -> int | None:
    """The single KÚ of the bound entity, else None. Each answer is the registry's own read
    of that entity — the chain, the address point, the street's doors, the část's doors —
    so no coordinate a portal supplied is ever asked where it lies."""
    for unit in chain:
        if unit.level == "katastralni_uzemi":
            return unit.code
    obec = next((unit for unit in chain if unit.level == "obec"), None)
    if obec is not None and obec.sole_katastr_kod is not None:
        return obec.sole_katastr_kod
    if point is not None:
        return point.katastr_kod
    if binding.target_kind == "street":
        return binding.street_katastr_kod
    if binding.target_kind == "admin_unit" and chain and chain[0].level == "cast_obce":
        return registry.part_katastr_kod(chain[0].unit_id)
    return None


def _registry_point(
    point: AddressPoint | None, chain: tuple[AdminUnit, ...]
) -> tuple[float | None, float | None]:
    """The FINEST bound entity that HAS a point: the address point, else the first unit on
    the chain carrying one.

    "Has one" is the whole subtlety, and it is why this walks instead of reading `chain[0]`.
    RÚIAN draws no polygon for a část obce or a městský obvod, so the two levels a Czech
    listing most often names by hand are exactly the two with no point of their own; a street
    has none either (`ruian_streets` carries no geometry, which is also why BIND's street
    candidate has no position). Each of them takes its TOWN's point rather than nothing —
    coarser than the granularity says, and still the honest answer to "where is this?".
    """
    if point is not None and point.lat is not None and point.lon is not None:
        return point.lat, point.lon
    for unit in chain:
        if unit.level in _UNPLACEABLE_LEVELS:
            continue
        if unit.lat is not None and unit.lon is not None:
            return unit.lat, unit.lon
    return None, None


def _cp(point, constraints: Constraints, binding: Binding) -> str | None:
    """The address point's, else the BIND's own, else the listing-wide claim.

    The bind's own is the middle rung and it exists for one reason: a street bound out of one
    segment of one line owns that segment's number and no other claim's (W18). Without it a
    listing carrying `Nad Bořislavkou` and a separate line reading `Livornská 5` published
    `Nad Bořislavkou 5` at `street_segment` grain."""
    if point is not None and point.cislo_domovni is not None:
        return str(point.cislo_domovni)
    if binding.house_number_cp is not None:
        return binding.house_number_cp
    return str(constraints.cislo_domovni) if constraints.cislo_domovni is not None else None


def _co(point, constraints: Constraints, binding: Binding) -> str | None:
    """The orientation number keeps its letter: `40a` is a different door from `40`."""
    if point is not None and point.cislo_orientacni is not None:
        return f"{point.cislo_orientacni}{point.znak_orientacniho or ''}"
    if binding.house_number_co is not None:
        return binding.house_number_co
    if constraints.cislo_orientacni is None:
        return None
    return f"{constraints.cislo_orientacni}{constraints.znak_orientacniho or ''}"
