"""Gates for the sold-comps read surface (migration 545 over migration 542's store).

Offline and deterministic — there is no database in CI's normal lane, so these are
regex gates over the migration text, the same shape as tests/test_migration_rls_grants.py
and tests/test_browse_read_path_guardrail.py. The live checks are the schema replay
(.github/workflows/migrations.yml) and tests/test_sql_schema_prepare.py.

Three invariant families:

1. SHAPE — `sold_comparables` must stay inlinable: `language sql`, `stable`, SECURITY
   INVOKER, one SELECT, NO `SET` clause, and exactly three parameters. Migration 109 is
   the recorded cost of the alternative: ~50 optional null-guarded filter params made a
   function un-inlinable, generically planned, and it timed out. Adding a filter as a
   parameter here is the failure this class guards.

2. POSTURE — the sales view is definer-style (security_invoker UNSET) over a deny-all
   RLS table, browser roles get SELECT/EXECUTE and nothing more, and it publishes every
   stored column EXCEPT `raw`. The fetch LEDGER gets no view at all: which cells were
   fetched is a projection of tenant pipeline state, so its one reader is a SECURITY
   DEFINER function scoped to the municipality containing the point asked about.

3. CONTRACT — every column an `Agenda.SOLD` filter names is actually returned by
   `sold_comparables`. Without this a sold filter would be a PostgREST 400 (or worse, a
   silent no-op) the first time the operator set it.
"""
from __future__ import annotations

import re
from pathlib import Path

from toolkit.filter_registry import REGISTRY, Agenda

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "migrations"
STORE = MIGRATIONS / "542_sold_transactions.sql"
SURFACE = MIGRATIONS / "545_sold_comps_read_surface.sql"

LINE_COMMENT = re.compile(r"--.*$", re.MULTILINE)


def _sql(path: Path) -> str:
    return LINE_COMMENT.sub("", path.read_text(encoding="utf-8")).lower()


def _balanced(src: str, open_at: int) -> str:
    """The text inside the parenthesis group that starts at `open_at`."""
    depth, i = 0, open_at
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[open_at + 1:i]
        i += 1
    raise AssertionError("unbalanced parentheses")


def _table_columns(sql: str, table: str) -> list[str]:
    m = re.search(rf"create table if not exists {table}\s*\(", sql)
    assert m, f"{table} is not created here"
    body = _balanced(sql, m.end() - 1)
    out: list[str] = []
    depth = 0
    field = ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(field)
            field = ""
        else:
            field += ch
    out.append(field)
    names = []
    for frag in out:
        head = frag.split()
        if head and head[0] not in {"primary", "unique", "check", "foreign", "constraint"}:
            names.append(head[0])
    return names


def _view_columns(sql: str, view: str, alias: str) -> list[str]:
    m = re.search(rf"create or replace view {view} as(.*?);", sql, re.S)
    assert m, f"{view} is not created here"
    return re.findall(rf"\b{alias}\.([a-z0-9_]+)", m.group(1))


def _function_block(sql: str, name: str) -> str:
    m = re.search(rf"create or replace function public\.{name}\s*\(.*?\$\$;", sql, re.S)
    assert m, f"{name} is not created here"
    return m.group(0)


def _function_params(block: str) -> list[str]:
    inner = _balanced(block, block.index("("))
    return [p.strip().split()[0] for p in inner.split(",") if p.strip()]


def _returns_table_columns(block: str) -> list[str]:
    m = re.search(r"returns table\s*\(", block)
    assert m, "not a returns-table function"
    inner = _balanced(block, m.end() - 1)
    return [c.strip().split()[0] for c in inner.split(",") if c.strip()]


def _body(block: str) -> str:
    return block.split("$$", 1)[1].rsplit("$$", 1)[0]


# --- 1. shape: the function stays inlinable ------------------------------


def test_sold_comparables_takes_the_point_and_the_radius_and_nothing_else() -> None:
    """Migration 109's rail. A filter must never arrive here as an optional
    parameter: the planner cannot fold `(param is null or col = param)` guards in a
    non-inlined body, which is exactly how browse_stats_properties timed out and had
    to forfeit inlining permanently."""
    params = _function_params(_function_block(_sql(SURFACE), "sold_comparables"))
    assert params == ["p_lat", "p_lng", "p_radius_m"], params


def test_sold_comparables_is_inlinable() -> None:
    """STABLE + SECURITY INVOKER + single SELECT + no SET clause = inlined, so
    PostgREST's filters, ORDER BY and LIMIT reach sold_transactions_geog_gist
    (migration 537's contract). `sold_coverage` is deliberately outside this contract —
    it is a definer-scoped one-row point lookup, and inlining buys it nothing."""
    block = _function_block(_sql(SURFACE), "sold_comparables")
    assert "language sql" in block
    assert re.search(r"\bstable\b", block)
    assert "security definer" not in block, "sold_comparables must run as the caller"
    header = block.split("$$", 1)[0]
    assert not re.search(r"\bset\s+\w+\s*(to|=)", header), "a SET clause blocks inlining"
    assert _body(block).count("select") == 1, "not a single SELECT"


def test_sold_spatial_predicates_cast_to_geography() -> None:
    """`geom` is geometry, so an uncast ST_DWithin/ST_Distance measures DEGREES and
    silently answers a different question (migration 507's rail). The serving index
    is on `(geom::geography)`, so the cast is also what reaches it."""
    body = _body(_function_block(_sql(SURFACE), "sold_comparables"))
    for call in ("st_dwithin", "st_distance"):
        m = re.search(rf"{call}\(([^;]*?)\)\s*(?:as|\n)", body, re.S)
        assert m, f"{call} not found"
        assert m.group(1).count("::geography") == 2, (
            f"{call}: both arguments must be cast to geography, got {m.group(1)!r}"
        )


# --- 2. posture -----------------------------------------------------------


def test_the_sold_view_is_definer_style() -> None:
    """The base table is RLS-on with zero policies (deny-all). A view with
    `security_invoker = true` — migration 536's TENANT shape — would return zero rows
    to every signed-in account."""
    headers = re.findall(r"create or replace view\s+\w+(.*?)\bas\b", _sql(SURFACE))
    assert headers, "no view is created here"
    for header in headers:
        assert not header.strip(), f"view option set on a MARKET view: {header!r}"


def test_the_public_view_publishes_every_stored_column_but_raw() -> None:
    stored = set(_table_columns(_sql(STORE), "sold_transactions"))
    published = set(_view_columns(_sql(SURFACE), "sold_transactions_public", "s"))
    assert published == stored - {"raw"}, (
        f"missing {sorted(stored - {'raw'} - published)}, "
        f"unexpected {sorted(published - stored)}"
    )


def test_the_fetch_ledger_is_never_published_to_a_browser_role() -> None:
    """A cell is fetched only where SOME account holds a live deal-pipeline card, so the
    ledger's obec set is a projection of tenant state — a browser-readable ledger would
    hand every signed-in account the map and the cadence of every other account's deal
    sourcing. Its one reader is `sold_coverage`, scoped to a single municipality."""
    sql = _sql(SURFACE)
    assert "sold_transaction_fetches_public" not in sql
    assert "sold_transaction_fetches" in _body(_function_block(sql, "sold_coverage"))


def test_browser_roles_get_read_access_and_nothing_else() -> None:
    sql = _sql(SURFACE)
    view = "sold_transactions_public"
    assert f"revoke all on {view} from anon, authenticated;" in sql
    assert f"grant select on {view} to authenticated;" in sql
    for name in ("sold_comparables", "sold_coverage"):
        assert re.search(
            rf"revoke execute on function public\.{name}\([^)]*\)\s*from public, anon;",
            sql,
        ), name
        assert re.search(
            rf"grant execute on function public\.{name}\([^)]*\)\s*"
            rf"to authenticated, service_role;",
            sql,
        ), name


def test_sold_coverage_answers_about_the_points_own_municipality() -> None:
    """The fetched cell is the obec envelope expanded by 5 km, so cells overlap heavily
    and "the newest box containing the point" would answer with whichever town happened
    to be walked last — a village's counts over a Prague address. The point is resolved
    through `admin_boundaries` instead, and the obec NAME comes back so the sentence can
    name the town it is about."""
    block = _function_block(_sql(SURFACE), "sold_coverage")
    cols = _returns_table_columns(block)
    assert cols == [
        "obec_kod", "obec_name", "fetched_at", "record_count", "source_total",
        "last_attempt_at", "last_attempt_status",
    ], cols
    body = _body(block)
    assert "admin_boundaries" in body and "level = 'obec'" in body
    assert "bbox" not in body, "coverage must not be inferred from the margin-expanded box"
    assert "security definer" in block and "set search_path = public" in block


def test_sold_coverage_distinguishes_a_failing_lane_from_an_unasked_cell() -> None:
    """A cell that has only ever FAILED is not a cell nobody asked for: collapsing the
    two renders a broken fetch lane as operator inaction ("add this property to the
    pipeline") on the only surface that reads this ledger."""
    body = _body(_function_block(_sql(SURFACE), "sold_coverage"))
    assert body.count("f.status = 'ok'") == 1, "the successful fetch is one of two arms"
    assert "try.status" in body, "the newest attempt of ANY status must come back too"


# --- 3. contract: the registry and the function agree --------------------


def test_every_sold_filter_reads_a_column_sold_comparables_returns() -> None:
    """The drift net. A SOLD filter naming a column the RPC does not return is a
    PostgREST 400 the first time the operator sets it — and the sold surface has no
    hand-coded escape set to hide in."""
    returned = set(_returns_table_columns(
        _function_block(_sql(SURFACE), "sold_comparables")))
    missing = sorted(
        f"{d.id} -> {d.pg_column}"
        for d in REGISTRY.values()
        if Agenda.SOLD in d.agendas and d.pg_column not in returned
    )
    assert not missing, (
        f"sold_comparables does not return: {missing}. Either return the column "
        f"(migration) or drop Agenda.SOLD from the filter."
    )
