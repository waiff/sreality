"""Shared location/district filtering.

One `DistrictChip` shape + one SQL-clause builder for every surface that lets
the operator narrow by place: Browse (browse_stats, migration 182, and the
client's `districtsFilterClause`), Watchdog (`api.notifications`), and the
dedup Decision history + manual review Queue (`api.property_dedup`). A
resolved chip matches by STABLE ADMIN ID (`obec_id` / `okres_id` / `region_id`)
so an obec pick can't collide with its same-named okres; a 'locality'
(street/POI) chip narrows to its containing obec AND a `place_search_text`
ILIKE; a legacy chip (no level/id) falls back to ILIKE-by-name across
`district` / `place_search_text` / `okres` / `region`, AND'd with an optional
parent-municipality `context` narrow. INCLUDE chips are OR'd (match any);
EXCLUDE chips are NOT'd (subtract).

`district_where` takes one or more table aliases. A single alias reproduces
the exact clauses/param-names Watchdog has shipped since migration 067 (kept
byte-identical on purpose — `tests/api/test_notifications.py` pins the shape).
Multiple aliases OR each chip across all of them before applying the
include/exclude grouping — "does EITHER side of this pair touch this place" —
which is what a pair-grain dedup query needs (a candidate/decision references
two `properties` rows, not one).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_ID_COL = {"obec": "obec_id", "okres": "okres_id", "kraj": "region_id"}


class DistrictChip(BaseModel):
    """One entry in a location filter. Mirrors the frontend's `DistrictChip`
    (`frontend/src/lib/filters.ts`) and its URL encoding (the parallel
    `districts` / `districts_ctx` / `districts_excl` / `districts_lvl` /
    `districts_id` query params, `frontend/src/lib/filters.ts`)."""

    name: str
    context: str | None = None
    excluded: bool = False
    level: str | None = None
    id: int | None = None


def _keys_for(i: int, alias: str | None) -> tuple[str, str, str]:
    # `alias is None` is the legacy single-alias caller — keep the exact
    # `district_id_{i}` / `district_name_{i}` / `district_ctx_{i}` names the
    # Watchdog matcher has always used, so that caller's SQL/params are
    # byte-identical to before this was extracted. Extra aliases (the dedup
    # pair-grain callers) get an alias-qualified name instead, since two
    # aliases testing the same chip index would otherwise collide.
    if alias is None:
        return f"district_id_{i}", f"district_name_{i}", f"district_ctx_{i}"
    return (
        f"district_id_{alias}_{i}",
        f"district_name_{alias}_{i}",
        f"district_ctx_{alias}_{i}",
    )


def _chip_clause_sql(
    chip: DistrictChip,
    alias: str,
    id_key: str,
    name_key: str,
    ctx_key: str,
    params: dict[str, Any],
) -> str:
    if chip.level in _ID_COL and chip.id is not None:
        params[id_key] = chip.id
        return f"{alias}.{_ID_COL[chip.level]} = %({id_key})s"
    if chip.level == "locality":
        # Wildcards live in the parameter VALUE, not as inline SQL '%'
        # literals (psycopg treats a bare '%' as a malformed placeholder).
        params[name_key] = f"%{chip.name}%"
        place_match = f"{alias}.place_search_text ILIKE %({name_key})s"
        if chip.id is not None:
            params[id_key] = chip.id
            return f"({alias}.obec_id = %({id_key})s AND {place_match})"
        return place_match
    # Legacy / unresolved chip: name ILIKE across all name columns, AND'd
    # with an optional parent-municipality context narrow. Never bare
    # `locality` (bazos stores the street outside locality) — always
    # `place_search_text` (street + locality, migration 182).
    params[name_key] = f"%{chip.name}%"
    name_half = (
        f"({alias}.district ILIKE %({name_key})s "
        f"OR {alias}.place_search_text ILIKE %({name_key})s "
        f"OR {alias}.okres ILIKE %({name_key})s "
        f"OR {alias}.region ILIKE %({name_key})s)"
    )
    if chip.context:
        params[ctx_key] = f"%{chip.context}%"
        ctx_half = (
            f"({alias}.district ILIKE %({ctx_key})s "
            f"OR {alias}.place_search_text ILIKE %({ctx_key})s "
            f"OR {alias}.okres ILIKE %({ctx_key})s "
            f"OR {alias}.region ILIKE %({ctx_key})s)"
        )
        return f"({name_half} AND {ctx_half})"
    return name_half


_LOCATION_LEVELS = {"obec", "okres", "kraj", "locality"}


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
    endpoint (Browse's URL, the dedup Decision history + Queue routes below).
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
        if lvl in _LOCATION_LEVELS:
            chip.level = lvl
            raw_id = ids[i] if i < len(ids) else None
            chip.id = int(raw_id) if raw_id else None
        chips.append(chip)
    return chips


def district_where(
    chips: list[DistrictChip] | None,
    aliases: list[str],
) -> tuple[list[str], dict[str, Any]]:
    """Render `chips` as parameterised WHERE-clause fragments (AND them
    together with the rest of the caller's WHERE) plus their params dict.

    `aliases` names the `properties`/`listings`-shaped table(s) each chip is
    tested against — e.g. `["l"]` for Watchdog's single listing alias, or
    `["l", "r"]` for a dedup pair query's two `properties` sides. Every alias
    must expose `district`, `place_search_text`, `okres`, `region`, `obec_id`,
    `okres_id`, `region_id` columns (the `properties`/`properties_public` /
    Watchdog `listings l` shape)."""
    if not chips:
        return [], {}
    if not aliases:
        raise ValueError("district_where requires at least one alias")
    legacy = len(aliases) == 1
    params: dict[str, Any] = {}
    inc_clauses: list[str] = []
    exc_clauses: list[str] = []
    for i, chip in enumerate(chips):
        per_alias = [
            _chip_clause_sql(chip, alias, *_keys_for(i, None if legacy else alias), params)
            for alias in aliases
        ]
        clause = per_alias[0] if legacy else "(" + " OR ".join(per_alias) + ")"
        (exc_clauses if chip.excluded else inc_clauses).append(clause)
    where: list[str] = []
    if inc_clauses:
        where.append("(" + " OR ".join(inc_clauses) + ")")
    if exc_clauses:
        where.append("NOT (" + " OR ".join(exc_clauses) + ")")
    return where, params
