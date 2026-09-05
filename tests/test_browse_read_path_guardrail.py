"""Guardrails for the Browse read path (migrations 273 -> 275 -> 276/277).

Three invariant families, all deterministic and offline (no database):

1. NO GATE — the publication gate is GONE (migration 475, NEW DEDUP Wave 0:
   its only stamper was the removed dedup engine). The EFFECTIVE
   (latest-migration) definitions of `properties_public` (live view: detail
   pages, watchdog matcher), `browse_projection` (migration 276: the ONE
   projection both Browse read models materialize from) and
   `listing_feed_public` must not call `publication_gate_enabled()` at all.

   Why this is still worth a test rather than a deletion: the function was
   SECURITY DEFINER and the planner cannot inline it, so a bare call in a WHERE
   ran ONCE PER ROW — ~87k times for the byt+pronájem cohort, the PR-#707
   incident. It survived as a scalar subquery `(SELECT publication_gate_enabled())`
   only because that made it an InitPlan. Anything re-introducing a per-row
   SECURITY DEFINER call into these three definitions repeats that outage, so
   the guard now watches for the whole class returning, not for its wrapping.

2. REBUILD INVARIANTS — the pg_cron rebuild functions (migration 277) must
   ANALYZE the replacement relation BEFORE swapping it live (autovacuum never
   analyzes a fresh relation in time; the frontend's count=planned reads
   pg_statistic — no stats = garbage counts), and must pg_notify PostgREST
   after the swap (the OID changed).

3. READ CONTRACT — every column the Browse frontend can select, filter, or
   sort on must exist in `browse_projection`'s output. The registry-driven
   majority is imported from the source of truth (toolkit.filter_registry);
   the frontend column lists are parsed from queries.ts; the few hand-coded
   applyFilters columns are pinned here explicitly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from toolkit.filter_registry import REGISTRY, Agenda

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "migrations"
QUERIES_TS = REPO / "frontend" / "src" / "lib" / "queries.ts"
GATE_CALL = re.compile(r"publication_gate_enabled\s*\(\s*\)", re.IGNORECASE)
# Strip line comments so a comment that merely *describes* the bad pattern
# ("...a BARE call: publication_gate_enabled()...") isn't flagged. `--` covers
# SQL, `#` covers Python sources that embed build SQL.
LINE_COMMENT = re.compile(r"(--|#).*$", re.MULTILINE)


def _strip_comments(src: str) -> str:
    return LINE_COMMENT.sub("", src)


def _assert_no_gate(raw: str, where: str) -> None:
    """Executable `raw` (comments stripped) must not call the publication gate.

    Scoped to ONE view definition by the callers below, not to a whole migration
    file — migration 475 legitimately names the function in its own
    `drop function publication_gate_enabled()`."""
    sql = _strip_comments(raw)
    m = GATE_CALL.search(sql)
    if m is None:
        return
    raise AssertionError(
        f"{where}: publication_gate_enabled() is back in this view. The gate was "
        f"removed with the legacy dedup engine (migration 475) and the function no "
        f"longer exists, so this cannot even resolve. It was also a SECURITY DEFINER "
        f"call the planner cannot inline — how PR-#707 turned one predicate into ~87k "
        f"per-row evaluations. Context: ...{sql[max(0, m.start() - 40):m.end() + 5]!r}"
    )


def _latest_migration_matching(pattern: str, what: str) -> Path:
    pat = re.compile(pattern, re.IGNORECASE)
    hits = [p for p in MIGRATIONS.glob("*.sql") if pat.search(p.read_text())]
    assert hits, f"no migration defines {what}"
    # Numeric prefix orders them; the highest-numbered is the effective one.
    return max(hits, key=lambda p: int(p.name.split("_", 1)[0]))


def _latest_migration_defining(view: str) -> Path:
    # `(public\.)?` is load-bearing: migration 425 writes
    # `create or replace view public.properties_public`, and without the optional
    # schema prefix this resolved the "effective" definition to migration 375 —
    # one column stale, so the read-contract check below was reading the wrong
    # select list. Found while removing the gate (2026-09-05).
    return _latest_migration_matching(
        rf"create\s+(or\s+replace\s+)?(materialized\s+)?view\s+(public\.)?{re.escape(view)}\b",
        view,
    )


def _latest_migration_defining_function(fn: str) -> Path:
    return _latest_migration_matching(
        rf"create\s+or\s+replace\s+function\s+{re.escape(fn)}\s*\(", fn
    )


# ------------------------------------------------------------------- no gate --

def _view_block(view: str) -> tuple[str, str]:
    """(sql, label) for just the effective CREATE VIEW statement of `view`."""
    src = _latest_migration_defining(view)
    lines = src.read_text().split("\n")
    pat = re.compile(
        rf"create\s+(or\s+replace\s+)?(materialized\s+)?view\s+(public\.)?{re.escape(view)}\b",
        re.IGNORECASE,
    )
    start = next(i for i, line in enumerate(lines) if pat.search(line))
    depth, out = 0, []
    for i in range(start, len(lines)):
        out.append(lines[i])
        code = re.sub(r"--.*$", "", lines[i])
        depth += code.count("(") - code.count(")")
        if depth <= 0 and ";" in code:
            break
    return "\n".join(out), f"{src.name}:{view}"


@pytest.mark.parametrize(
    "view", ["properties_public", "browse_projection", "listing_feed_public"]
)
def test_read_path_view_has_no_publication_gate(view: str) -> None:
    sql, label = _view_block(view)
    _assert_no_gate(sql, label)


def test_guardrail_detects_a_returning_gate() -> None:
    """The check itself must fail on a gate call (so it can't silently pass)."""
    with pytest.raises(AssertionError):
        _assert_no_gate(
            "create view v as select 1 where not publication_gate_enabled();",
            "synthetic",
        )
    # the scalar-subquery form was the SAFE spelling while the gate existed; now
    # that the function is dropped it must be caught too.
    with pytest.raises(AssertionError):
        _assert_no_gate(
            "create view v as select 1 where not (select publication_gate_enabled());",
            "synthetic",
        )
    # a comment naming it is not a call.
    _assert_no_gate(
        "-- publication_gate_enabled() used to live here\ncreate view v as select 1;",
        "synthetic",
    )


# --------------------------------------------------------- rebuild invariants --

def _rebuild_invariants(fn: str, next_rel: str, rename_stmt: str) -> None:
    src = _latest_migration_defining_function(fn)
    sql = _strip_comments(src.read_text()).lower()
    analyze_at = sql.find(f"analyze {next_rel}")
    rename_at = sql.find(rename_stmt)
    assert analyze_at != -1, (
        f"{src.name}: {fn} must `analyze {next_rel}` before the swap — "
        f"autovacuum never analyzes it in time and count=planned reads "
        f"pg_statistic (no stats = garbage counts)."
    )
    assert rename_at != -1, f"{src.name}: {fn} lost its swap ({rename_stmt!r})"
    assert analyze_at < rename_at, (
        f"{src.name}: {fn} analyzes AFTER the swap — the fresh relation would "
        f"serve reads with no stats until then."
    )
    assert "pg_notify('pgrst'" in sql, (
        f"{src.name}: {fn} must pg_notify('pgrst', 'reload schema') after the "
        f"swap — the relation OID changed and PostgREST caches it."
    )


def test_list_rebuild_analyzes_before_swap_and_notifies() -> None:
    _rebuild_invariants(
        "rebuild_browse_list",
        "browse_list_next",
        "alter table browse_list_next rename",
    )


def test_map_rebuild_analyzes_before_swap_and_notifies() -> None:
    _rebuild_invariants(
        "rebuild_properties_map_mv",
        "properties_map_mv_next",
        "alter materialized view properties_map_mv_next rename",
    )


# ------------------------------------------------------------- read contract --

def _projection_columns() -> set[str]:
    """Parse browse_projection's output column names from its latest migration.
    Paren-depth-aware split of the top-level SELECT list; alias = the `as X`
    name when present, else the token after the final dot."""
    src = _strip_comments(
        _latest_migration_defining("browse_projection").read_text()
    ).lower()
    start = src.index("view browse_projection as")
    body = src[start:]
    body = body[body.index("select") + len("select"):]
    body = body[: body.index("\nfrom properties")]
    items, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur))
    cols: set[str] = set()
    for item in items:
        item = " ".join(item.split())
        m = re.search(r"\bas\s+([a-z_0-9]+)\s*$", item)
        if m:
            cols.add(m.group(1))
        else:
            cols.add(item.rsplit(".", 1)[-1].strip())
    return cols


def _ts_string_const(name: str) -> str:
    """Extract a `const NAME = '...' + '...';` string constant from queries.ts."""
    src = QUERIES_TS.read_text()
    m = re.search(rf"const {name} =([^;]+);", src)
    assert m, f"queries.ts: const {name} not found"
    return "".join(re.findall(r"'([^']*)'", m.group(1)))


def _sortable_fields() -> set[str]:
    src = QUERIES_TS.read_text()
    m = re.search(r"const SORTABLE_FIELDS[^;]+;", src)
    assert m, "queries.ts: SORTABLE_FIELDS not found"
    return set(re.findall(r"'(\w+)'", m.group(0)))


# Columns applyFilters / districtsFilterClause / applyPrefilters dispatch on
# OUTSIDE the generated registry (hand-coded in frontend/src/lib/queries.ts).
# Mirrors that file — update BOTH when adding a hand-coded filter; the
# registry-driven majority is imported from toolkit.filter_registry below, so
# only genuinely hand-wired columns belong here.
HAND_CODED_FILTER_COLUMNS = {
    "is_active", "last_seen_at", "first_seen_at", "last_change_at",
    # districtsFilterClause
    "obec_id", "okres_id", "region_id", "district", "place_search_text",
    "okres", "region",
    # material / enums / price
    "building_type", "furnished", "ownership", "price_czk",
    # price-change windows (priceChangeCountColumn)
    "price_change_count", "price_change_count_30d", "price_change_count_90d",
    "price_change_count_365d", "total_price_change_pct",
    # bbox
    "lat", "lng",
    # prefilter .in() targets
    "sreality_id", "property_id",
}


def test_frontend_read_contract_subset_of_projection() -> None:
    """Every column Browse can SELECT, FILTER, or SORT on exists in
    browse_projection — the drift net for the read model. A new Browse filter
    or card column that isn't in the projection fails here BEFORE it ships as
    a silently-broken (PostgREST 400) surface."""
    projection = _projection_columns()

    registry_cols = {
        d.pg_column
        for d in REGISTRY.values()
        if d.pg_column and Agenda.BROWSE in d.agendas
    }
    card_cols = set(_ts_string_const("CARD_COLS").split(","))
    table_cols = set(_ts_string_const("TABLE_COLS").split(","))
    map_cols = set(_ts_string_const("MAP_COLS").split(","))
    contract = (
        registry_cols
        | card_cols
        | table_cols
        | map_cols
        | _sortable_fields()
        | HAND_CODED_FILTER_COLUMNS
    )

    missing = sorted(contract - projection)
    assert not missing, (
        f"browse_projection is missing columns the Browse frontend uses: "
        f"{missing}. Add them to the projection (migration) — the rebuild "
        f"functions materialize SELECT * so they propagate automatically."
    )
