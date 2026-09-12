"""The ONE place predicate — a chip is a level plus a RÚIAN code (W3 S3).

Every surface that lets the operator narrow by place — Browse's list
(`districtsFilterClause`), Browse's Stats and map (`browse_stats_properties` /
`browse_map_cells`), the pipeline board's in-memory predicate and the Watchdog
matcher — used to carry its own copy of FIVE predicates: `obec_id.eq`,
`okres_id.eq`, `region_id.eq`, a `locality` pair (`obec_id.eq` AND an ILIKE on
`place_search_text`), and a legacy name fallback ILIKE-ing across
`district` / `place_search_text` / `okres` / `region` AND'd with an optional
parent `context` narrow. Six live copies, five predicates each.

W3 replaces all of it with one rule:

    <level>_id = any(<codes at that level>)

plain equality, per level, nothing else. `listing_location` (migration 501)
answers every listing with RÚIAN codes, and `browse_list.obec_id` /
`okres_id` / `region_id` already ARE those codes (`admin_boundaries.id` is the
RÚIAN code — migrations 083/141, and the column comments on 162/171 say so), so
the swap is value-identical for a resolved row. `cast_obce_id` is the fourth
level, new in migration 503.

THE SENTINEL, and why there is no "unmatched chip" branch anywhere. A chip that
carries no code — an old saved filter or preset written before migration 172,
or a name the RÚIAN name index cannot resolve — compiles to `NO_MATCH_CODE`
(-1) at the obec level. RÚIAN codes are positive, so the arm is false for every
row: an INCLUDE chip we cannot resolve contributes NOTHING to the cohort (fail
CLOSED — a widened watchdog is a notification storm), an EXCLUDE chip we cannot
resolve subtracts nothing. One rule, three compilers (here, `districtCodes.ts`,
and the two RPC bodies), no special case in any of them.

Stored blobs are NEVER migrated (presets store the full blob, and a watchdog's
`filter_spec` is the operator's own text): `upgrade_district_chips` resolves a
name-only chip to its codes ONCE at read time, in front of the matcher, and
logs one line for a chip it had to drop to the sentinel.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

LOG = logging.getLogger(__name__)

# level -> the code column on every place-filterable relation. Browse reads
# `browse_list` (codes from `listing_location` since migration 503); the
# Watchdog reads `properties_public`; both spell the four columns the same.
LEVEL_COLUMN: dict[str, str] = {
    "kraj": "region_id",
    "okres": "okres_id",
    "obec": "obec_id",
    "cast_obce": "cast_obce_id",
}
# Deterministic arm order, coarsest first — the compiled predicate must be
# byte-stable so the SPA, the API and the RPC bodies can be diffed.
LEVEL_ORDER: tuple[str, ...] = ("kraj", "okres", "obec", "cast_obce")

# A street / address / POI pick resolves to its CONTAINING obec (that is what
# `/maps/resolve` stamps on the chip), so it compiles as an obec code. The old
# `place_search_text ILIKE` half of that predicate is gone: a street chip now
# means its municipality, nothing narrower, until a street-grain code exists.
LEVEL_ALIASES: dict[str, str] = {"locality": "obec"}

# Positive RÚIAN codes only, so this matches no row at any level.
NO_MATCH_CODE = -1

CHIP_LEVELS: frozenset[str] = frozenset(LEVEL_COLUMN) | frozenset(LEVEL_ALIASES)


class DistrictChip(BaseModel):
    """One entry in a location filter. Mirrors the frontend's `DistrictChip`
    (`frontend/src/lib/filters.ts`) and its URL encoding (the parallel
    `districts` / `districts_ctx` / `districts_excl` / `districts_lvl` /
    `districts_id` query params).

    `context` is kept because it is part of the stored wire format and it
    disambiguates a name at read time (`upgrade_district_chips`); it is no
    longer a predicate of its own."""

    name: str
    context: str | None = None
    excluded: bool = False
    level: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class DistrictCodePlan:
    """The compiled chip set: codes per level, include and exclude.

    This dataclass IS the shared definition — `districtCodePlan` in
    `frontend/src/lib/districtCodes.ts` returns the same object for the same
    chips, and `tests/fixtures/district_chip_plan.json` is the table both are
    tested against."""

    include: dict[str, list[int]] = field(default_factory=dict)
    exclude: dict[str, list[int]] = field(default_factory=dict)
    # Names of chips that resolved to no code and fell back to the sentinel.
    unresolved: list[str] = field(default_factory=list)


def compiled_level(level: str | None) -> str | None:
    """The code level a chip's picked level filters on, or None if it has no code."""
    if level is None:
        return None
    return LEVEL_ALIASES.get(level, level if level in LEVEL_COLUMN else None)


def district_code_plan(chips: Sequence[DistrictChip] | None) -> DistrictCodePlan:
    include: dict[str, list[int]] = {}
    exclude: dict[str, list[int]] = {}
    unresolved: list[str] = []
    for chip in chips or []:
        level = compiled_level(chip.level)
        code = chip.id
        if level is None or code is None:
            level, code = "obec", NO_MATCH_CODE
            unresolved.append(chip.name)
        bucket = exclude if chip.excluded else include
        codes = bucket.setdefault(level, [])
        if code not in codes:
            codes.append(code)
    return DistrictCodePlan(include=include, exclude=exclude, unresolved=unresolved)


def _arms(plan_side: dict[str, list[int]], alias: str, prefix: str,
          params: dict[str, Any]) -> str:
    arms: list[str] = []
    for level in LEVEL_ORDER:
        codes = plan_side.get(level)
        if not codes:
            continue
        key = f"{prefix}_{level}"
        params[key] = list(codes)
        arms.append(f"{alias}.{LEVEL_COLUMN[level]} = ANY(%({key})s)")
    return " OR ".join(arms)


def district_where(
    chips: Sequence[DistrictChip] | None,
    alias: str,
) -> tuple[list[str], dict[str, Any]]:
    """Render `chips` as parameterised WHERE fragments (AND them together with
    the rest of the caller's WHERE) plus their params dict.

    `alias` names the relation each chip is tested against — `"l"` for the
    Watchdog's `properties_public` alias. It must expose `region_id`,
    `okres_id`, `obec_id` and `cast_obce_id`; no text column is read any more."""
    if not chips:
        return [], {}
    if not alias:
        raise ValueError("district_where requires an alias")
    plan = district_code_plan(chips)
    params: dict[str, Any] = {}
    where: list[str] = []
    inc = _arms(plan.include, alias, "district_codes", params)
    if inc:
        where.append(f"({inc})")
    exc = _arms(plan.exclude, alias, "district_codes_excl", params)
    if exc:
        where.append(f"NOT ({exc})")
    return where, params


# --------------------------------------------------------------------------
# The compatibility reader for stored chips.
# --------------------------------------------------------------------------

# (name, context) -> the (level, code) pairs the RÚIAN name index knows for it.
ChipNameResolver = Callable[
    [Sequence[tuple[str, str | None]]],
    dict[tuple[str, str | None], list[tuple[str, int]]],
]


def needs_upgrade(chips: Iterable[DistrictChip] | None) -> bool:
    return any(compiled_level(c.level) is None or c.id is None for c in chips or [])


def upgrade_district_chips(
    chips: Sequence[DistrictChip] | None,
    resolve: ChipNameResolver,
    *,
    origin: str = "stored filter",
) -> list[DistrictChip] | None:
    """Resolve name-only chips to level+code ONCE, at read time.

    A saved watchdog spec or a Browse preset from before migration 172 stores
    `{name, context}` and nothing else. The blob is never rewritten (rule: a
    preset stores the full blob) — this resolves it on the way into the
    predicate. One name can legitimately answer at several levels ("Jihlava" is
    an obec AND an okres); the old ILIKE matched both, so all of them are kept,
    which is the closest code-equality has to the retired behaviour. A name the
    index cannot place keeps its chip (so the operator still sees it) and falls
    through to the sentinel, and is logged once."""
    if not chips:
        return chips if chips is None else []
    if not needs_upgrade(chips):
        return list(chips)
    wanted = [
        (c.name, c.context)
        for c in chips
        if compiled_level(c.level) is None or c.id is None
    ]
    try:
        resolved = resolve(wanted)
    except Exception as exc:  # noqa: BLE001 — a name index hiccup must not drop a filter
        LOG.warning("district chips: name resolution failed for %s (%s)", origin, exc)
        resolved = {}
    out: list[DistrictChip] = []
    dropped: list[str] = []
    for chip in chips:
        if compiled_level(chip.level) is not None and chip.id is not None:
            out.append(chip)
            continue
        matches = resolved.get((chip.name, chip.context)) or []
        if not matches:
            dropped.append(chip.name)
            out.append(chip)
            continue
        for level, code in matches:
            out.append(
                DistrictChip(
                    name=chip.name,
                    context=chip.context,
                    excluded=chip.excluded,
                    level=level,
                    id=code,
                )
            )
    if dropped:
        LOG.warning(
            "district chips: %s has %d unresolvable name-only chip(s) %s — "
            "they match nothing (NO_MATCH_CODE)", origin, len(dropped), dropped,
        )
    return out


# --------------------------------------------------------------------------
# The wire format.
# --------------------------------------------------------------------------


def parse_district_chips_csv(
    names_raw: str | None,
    ctx_raw: str | None = None,
    excl_raw: str | None = None,
    lvl_raw: str | None = None,
    id_raw: str | None = None,
) -> list[DistrictChip] | None:
    """Parse the parallel `districts` / `districts_ctx` / `districts_excl` /
    `districts_lvl` / `districts_id` CSV query params into `DistrictChip`s —
    the exact wire format `frontend/src/lib/filters.ts` (`parseDistrictChips` /
    `districtChipsToCsvParams`) emits for every location-filterable GET
    endpoint (Browse's URL, the Watchdog routes).
    Returns `None` when `names_raw` is absent/empty, matching "no filter"."""
    if not names_raw:
        return None
    names = names_raw.split(",")
    ctxs = ctx_raw.split(",") if ctx_raw else []
    excls = excl_raw.split(",") if excl_raw else []
    lvls = lvl_raw.split(",") if lvl_raw else []
    ids = id_raw.split(",") if id_raw else []
    chips: list[DistrictChip] = []
    for i, name in enumerate(names):
        ctx = ctxs[i] if i < len(ctxs) else None
        chip = DistrictChip(name=name, context=(ctx or None))
        if i < len(excls) and excls[i] == "1":
            chip.excluded = True
        lvl = lvls[i] if i < len(lvls) else None
        if lvl in CHIP_LEVELS:
            chip.level = lvl
            raw_id = ids[i] if i < len(ids) else None
            chip.id = int(raw_id) if raw_id else None
        chips.append(chip)
    return chips
