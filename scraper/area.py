"""Single source of truth for the headline `area_m2` + its `area_basis`.

`area_m2` is the dwelling's interior area, comparable across portals. Each
parser only maps its source's labels onto the typed measures (usable / floor /
total) and the plot onto `estate_area`; the precedence that picks the headline
lives HERE so it can never diverge per portal:

  usable -> floor -> total -> fallback(unknown)

Land (`pozemek`) has no interior, so its `area_m2` is always NULL and the plot
is read from `estate_area` instead — this is the one rule that keeps the "Area"
filter from ever comparing an apartment interior against a parcel.
"""

from __future__ import annotations

_DWELLING_FALLBACK_BLOCKED: frozenset[str] = frozenset({"pozemek"})


def derive_headline_area(
    *,
    category_main: str | None,
    usable: float | None,
    floor: float | None = None,
    total: float | None = None,
    fallback: float | None = None,
) -> tuple[float | None, str | None]:
    """Return (area_m2, area_basis) for one listing. See module docstring."""
    if category_main in _DWELLING_FALLBACK_BLOCKED:
        return None, None
    if usable is not None:
        return usable, "usable"
    if floor is not None:
        return floor, "floor"
    if total is not None:
        return total, "total"
    if fallback is not None:
        return fallback, "unknown"
    return None, None
