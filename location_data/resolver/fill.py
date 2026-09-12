"""FILL — step 2 of 4: the hierarchy, joined off the bound ids.

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

Four fields may fall back to a claim when the registry has none: `street_name`,
`house_number_cp`, `house_number_co`, `psc`. Preserve-if-null, never overwrite — a claimed
value that DISAGREES with the registry is not silently replaced, the registry simply wins
where it has an answer (on a registry-bound row the official street form `nám. Budovatelů`
beats the portal's `Budovatelů`). The ONE thing the registry does not get to respell is an
OPERATOR correction, which is also the only survivorship rule that outlived the policy
table: `bind.operator_fields` is the whole of it.
"""

from __future__ import annotations

from location_data.resolver.bind import Constraints, operator_fields
from location_data.resolver.types import AdminUnit, Binding, Fill, RegistryView

_LEVEL_FIELDS = {
    "kraj": ("kraj_kod", "kraj_name"),
    "okres": ("okres_kod", "okres_name"),
    "obec": ("obec_kod", "obec_name"),
    "cast_obce": ("cast_obce_kod", "cast_obce_name"),
    # A Prague/Brno městský obvod is the quarter for serving purposes when no ČástObce
    # sits on the chain; it never overwrites one that does.
    "momc": ("cast_obce_kod", "cast_obce_name"),
}


def fill(
    binding: Binding,
    constraints: Constraints,
    registry: RegistryView,
    *,
    operator: dict[str, str] | None = None,
) -> Fill:
    """-> the fourteen hierarchy/address values of the answer row."""
    operator = operator or {}
    values: dict[str, object] = {}
    for unit in _chain(binding, registry):
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
        street_name=(
            operator.get("street_name") or binding.street_name or constraints.street_verbatim
        ),
        house_number_cp=operator.get("house_number_cp") or _cp(point, constraints),
        house_number_co=operator.get("house_number_co") or _co(point, constraints),
        psc=operator.get("psc")
        or (point.psc if point is not None and point.psc else constraints.psc),
    )


def _chain(binding: Binding, registry: RegistryView) -> tuple[AdminUnit, ...]:
    """The ONE registry read FILL makes. The finest bound unit first, so its own level lands
    on the row alongside every ancestor."""
    if binding.cast_obce_unit_id is not None:
        chain = tuple(registry.admin_chain(binding.cast_obce_unit_id))
        if chain:
            return chain
    if binding.admin_unit_id is not None:
        return tuple(registry.admin_chain(binding.admin_unit_id))
    if binding.obec_kod is not None:
        return tuple(registry.admin_chain_by_code("obec", binding.obec_kod))
    return ()


def _cp(point, constraints: Constraints) -> str | None:
    if point is not None and point.cislo_domovni is not None:
        return str(point.cislo_domovni)
    return str(constraints.cislo_domovni) if constraints.cislo_domovni is not None else None


def _co(point, constraints: Constraints) -> str | None:
    """The orientation number keeps its letter: `40a` is a different door from `40`."""
    if point is not None and point.cislo_orientacni is not None:
        return f"{point.cislo_orientacni}{point.znak_orientacniho or ''}"
    if constraints.cislo_orientacni is None:
        return None
    return f"{constraints.cislo_orientacni}{constraints.znak_orientacniho or ''}"
