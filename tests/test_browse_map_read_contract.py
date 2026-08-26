"""The map's cohort and the Stats cohort are built from ONE argument object (W6b).

W6b gives the Browse map a server-side RPC — `browse_map_cells`, migration 439 — that
takes the SAME named parameters as `browse_stats_properties`. The SPA builds that ~74-key
object ONCE (`buildBrowseStatsArgs` in frontend/src/lib/queries.ts) and both readers call
it, because two inline literals of 74 keys each is the single richest opportunity in this
codebase for the map and the Stats tab to describe different sets of properties with no
error anywhere.

Three drifts are possible and all three are silent. This file is the net for each.

  1. A key in the builder that the RPC has no parameter for. PostgREST answers a `.rpc()`
     with an unknown argument with `PGRST202` ("Could not find the function ... in the
     schema cache") — the WHOLE map read 404s, and reads fail silently app-wide (main.tsx
     wires onError on the MutationCache only, never the QueryCache). So the symptom of
     adding a Browse filter and forgetting the migration is an empty map, not an error.

  2. A parameter the map RPC carries and the Stats RPC does not, or vice versa. Divergence
     here is the count-vs-list class migration 351 was written to close.

  3. A prefilter id space the RPC does not carry. `applyPrefilters` emits `.in()` on THREE
     spaces — listing_id, obec_id AND property_id. `browse_stats_properties` carries only
     the last two, because the LEGACY city-quality path reaches it as `city_index_rules`
     instead; the map resolves that path client-side into a listing_id allowlist. An RPC
     modelled on the Stats parameter list alone drops it silently, and the listing_id
     space is live whenever `?cityQualityLegacy=1` sits in localStorage.

Offline; runs in the normal `pytest -q` lane. Nothing here needs a database — the
behaviour of the shipped SQL is tests/test_browse_map_cells_live.py's subject.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
QUERIES_TS = REPO / "frontend" / "src" / "lib" / "queries.ts"
MAP_LEGACY_TS = REPO / "frontend" / "src" / "lib" / "mapLegacy.ts"
MIGRATIONS = REPO / "migrations"

# The two parameters browse_map_cells has on purpose and browse_stats_properties does not.
_MAP_ONLY_PARAMS = {"listing_ids_filter", "point_budget"}


def _ts() -> str:
    return QUERIES_TS.read_text(encoding="utf-8")


def _balanced_block(src: str, open_at: int) -> str:
    """The substring from `open_at` (which must index a `{`) to its matching `}`."""
    assert src[open_at] == "{", src[open_at: open_at + 40]
    depth, i = 0, open_at
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at: i + 1]
        i += 1
    raise AssertionError("unbalanced braces in queries.ts")


def _strip_ts_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", src)


def _builder_keys() -> set[str]:
    """The snake_case keys `buildBrowseStatsArgs` puts on the wire, including the two it
    spreads in from `resolved`."""
    src = _ts()
    at = src.index("export const buildBrowseStatsArgs")
    block = _balanced_block(src, src.index("{", src.index("return {", at)))
    body = _strip_ts_comments(block)
    keys = set(re.findall(r"^\s*([a-z][a-z0-9_]*)\s*:", body, flags=re.MULTILINE))
    assert "...resolved" in body, (
        "buildBrowseStatsArgs no longer spreads `resolved` — the two id allowlists the "
        "caller resolves over the network have stopped reaching the RPC."
    )
    # `...resolved`'s own keys are declared on the interface, not in the literal.
    iface = _balanced_block(src, src.index("{", src.index("interface BrowseStatsResolvedFilters")))
    keys |= set(re.findall(r"^\s*([a-z][a-z0-9_]*)\s*:", _strip_ts_comments(iface),
                           flags=re.MULTILINE))
    assert len(keys) > 60, f"only parsed {len(keys)} builder keys — the parse broke"
    return keys


def _rpc_call_block(fn: str) -> str:
    """The object literal passed to `supabase.rpc('<fn>', {...})`, comments stripped."""
    src = _ts()
    at = src.index(f"supabase.rpc('{fn}'")
    return _strip_ts_comments(_balanced_block(src, src.index("{", at)))


def _latest_migration_defining(func: str) -> Path:
    pat = re.compile(rf"create or replace function (?:public\.)?{func}\s*\(", re.IGNORECASE)
    hits = [p for p in MIGRATIONS.glob("*.sql") if pat.search(p.read_text(encoding="utf-8"))]
    assert hits, f"no migration defines {func}"
    return max(hits, key=lambda p: int(p.name.split("_", 1)[0]))


def _sql_params(func: str) -> set[str]:
    """Parameter names of the LATEST definition of `func`, read from the migration."""
    sql = _latest_migration_defining(func).read_text(encoding="utf-8")
    at = re.search(rf"create or replace function (?:public\.)?{func}\s*\(",
                   sql, re.IGNORECASE).end()
    depth, i = 1, at
    while depth:
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
        i += 1
    args = re.sub(r"--[^\n]*", "", sql[at: i - 1])
    names = set(re.findall(r"(?:^|,)\s*([a-z][a-z0-9_]*)\s+[a-z]", args))
    assert len(names) > 60, f"only parsed {len(names)} params for {func} — the parse broke"
    return names


def test_every_builder_key_is_a_parameter_of_both_rpcs() -> None:
    """One builder, two RPCs, zero unknown arguments.

    RED by: adding a filter to `buildBrowseStatsArgs` and shipping it without adding the
    parameter to migration 439's browse_map_cells (or to browse_stats_properties). The
    live symptom is a PGRST202 on the whole read — an EMPTY MAP, because the SPA swallows
    query errors — which is exactly the class of failure that reaches production green.
    """
    keys = _builder_keys()
    for func in ("browse_stats_properties", "browse_map_cells"):
        missing = sorted(keys - _sql_params(func))
        assert not missing, (
            f"buildBrowseStatsArgs sends {missing} but public.{func} has no such "
            f"parameter ({_latest_migration_defining(func).name} is its latest definition)."
        )


def test_the_two_rpcs_take_the_same_cohort_parameters() -> None:
    """browse_map_cells = browse_stats_properties + listing_ids_filter + point_budget.

    RED by: adding a cohort parameter to one RPC and not the other. The map and the Stats
    tab would then answer for different cohorts under the same filters, and the only
    symptom is two numbers that disagree.
    """
    stats = _sql_params("browse_stats_properties")
    cells = _sql_params("browse_map_cells")
    assert not _MAP_ONLY_PARAMS - cells, (
        f"browse_map_cells lost {sorted(_MAP_ONLY_PARAMS - cells)} — see this file's "
        "header for why listing_ids_filter cannot be dropped."
    )
    assert not cells - stats - _MAP_ONLY_PARAMS, (
        f"browse_map_cells takes {sorted(cells - stats - _MAP_ONLY_PARAMS)} and "
        "browse_stats_properties does not — the map cohort can now be narrowed in a way "
        "the Stats cohort cannot."
    )
    assert not stats - cells, (
        f"browse_stats_properties takes {sorted(stats - cells)} and browse_map_cells does "
        "not — the Stats cohort can now be narrowed in a way the map cohort cannot."
    )


def test_the_map_rpc_carries_all_three_prefilter_id_spaces() -> None:
    """Whatever applyPrefilters filters on, the map RPC must receive.

    `applyPrefilters` is the ONE place the cohort reads apply their id allowlists, and it
    emits `.in()` on three columns. The RPC read has no `.in()` to inherit, so each space
    has to be handed over by name.

    RED by: deleting `listing_ids_filter: pre.listingIds` from the browse_map_cells call
    (the shape an RPC modelled on browse_stats_properties' parameter list alone would
    have had), or by adding a fourth `.in()` to applyPrefilters without a matching
    argument. Neither raises: the map would simply show a cohort the rest of Browse does
    not.
    """
    src = _ts()
    at = src.index("export const applyPrefilters")
    body = _strip_ts_comments(_balanced_block(src, src.index("{", at)))
    spaces = dict(re.findall(r"\.in\('([a-z_]+)',\s*p\.([A-Za-z]+)\)", body))
    assert set(spaces) == {"listing_id", "obec_id", "property_id"}, (
        f"applyPrefilters' id spaces changed: {spaces}"
    )

    call = _rpc_call_block("browse_map_cells")
    for column, field in spaces.items():
        assert re.search(rf"pre\.{field}\b", call), (
            f"applyPrefilters filters on `{column}` via `p.{field}`, but the "
            f"browse_map_cells call never passes `pre.{field}` — that allowlist is "
            "silently dropped on the map and applied everywhere else."
        )


def test_the_map_legacy_flag_is_read_once_at_module_load() -> None:
    """`?map=legacy` must be a module-load constant, not a runtime lookup.

    The map query is keyed `['map', filters]`. A flag that can change between renders
    without changing that key lets react-query serve a CLUSTER payload out of a cache
    entry warmed by a POINTS payload — ListingMap is then handed `cells` for `rows` or the
    reverse, with no refetch and no error. cityQualityLegacy.ts made the same choice for
    the same reason.

    RED by: exporting the detector itself (`export const MAP_LEGACY = detect`) or reading
    `window.location` / `localStorage` inside fetchListingsForMap.
    """
    src = _strip_ts_comments(MAP_LEGACY_TS.read_text(encoding="utf-8"))
    assert re.search(r"export const MAP_LEGACY\s*=\s*detect\(\)\s*;", src), (
        "mapLegacy.ts must export the RESULT of detect(), evaluated once at module load"
    )
    assert "try {" in src and "catch" in src, (
        "the localStorage accessor itself throws in a private window (or with site data "
        "blocked) — cityQualityLegacy.ts guards it and this must too"
    )

    fetcher_at = _ts().index("export const fetchListingsForMap")
    fetcher = _strip_ts_comments(_ts()[fetcher_at: fetcher_at + 4000])
    for forbidden in ("window.location", "localStorage", "URLSearchParams"):
        assert forbidden not in fetcher, (
            f"fetchListingsForMap reads {forbidden} directly — the lane choice must come "
            "from the module-load constant, or the react-query cache can mix shapes."
        )
