"""W14: the audit page reads its numbers against the WHOLE database.

The operator, 2026-09-14: "Include a proper waterfall from the number 841408 on the
location audit page so that we know exactly what are the numbers we are looking at
there and that we are comparing the 'hidden' or 'unresolved' in light of the entire
db."

`location_audit_waterfall` (migration 523) is that chain — every listing ever
collected, the not-served history, the served set, what the lane has answered for,
and the hidden remainder the page lists — written once an hour by the producer that
already refreshes `location_pin_audit_mv`.

The whole value of it is that it is not a SECOND census. Two things must therefore
hold, and a migration is not importable at runtime, so this file is the rail:

1. THE PIN. The two cuts are `location_data.claims_common`'s own constants,
   rendered character for character (the same rail
   tests/test_location_w5_serve_resolved.py runs over the serving surfaces). A
   re-typed predicate here would put the audit page and Browse on two different
   definitions of "visible", which is exactly what W5 spent a wave removing.
2. THE ARITHMETIC. `lost` is the previous chain step's count minus this one's,
   and every split partitions its parent. The SQL computes them; these tests check
   the SHAPE that computes them and the laws the migration proves at apply time.
"""

from __future__ import annotations

import re
from pathlib import Path

from location_data.claims_common import (
    SERVED_LISTING_PREDICATE,
    SERVED_LOCATION_PREDICATE,
)

REPO = Path(__file__).resolve().parents[1]
W14 = REPO / "migrations" / "523_location_w14_audit_waterfall.sql"
PAGE = REPO / "frontend" / "src" / "pages" / "LocationPinAudit.tsx"
READER = REPO / "frontend" / "src" / "lib" / "locationWaterfall.ts"

_LINE_COMMENT = re.compile(r"--.*$", re.MULTILINE)


def _sql() -> str:
    return W14.read_text(encoding="utf-8")


def _body() -> str:
    """The file with its prose stripped, so a match is CODE and never a comment."""
    return _LINE_COMMENT.sub("", _sql())


def _squeeze(t: str) -> str:
    return " ".join(t.split())


# ----------------------------------------------------------------------- the pin


def test_the_cohort_is_the_shared_served_predicate_verbatim() -> None:
    """RED by: re-typing `l.is_active or exists (...)` by hand in the migration.
    The alias `l` is part of that constant's contract and the waterfall keeps it."""
    assert _squeeze(SERVED_LISTING_PREDICATE) in _squeeze(_body())


def test_the_answer_is_the_shared_consumer_rule_verbatim() -> None:
    """The one definition of "this store has an answer for it": a point, or the
    determination that it is abroad. RED by: spelling it as `ll.geom is not null`
    off the left join — which would be the same question asked a second way, and
    the next change to the rule would move Browse and leave this page behind."""
    assert SERVED_LOCATION_PREDICATE in _body()


def test_the_waterfall_adds_no_column_to_listings() -> None:
    """North star: no new field on the listings. The chain is a read."""
    body = _body().lower()
    assert "alter table listings" not in body
    assert "alter table public.listings" not in body


# ---------------------------------------------------------------- the arithmetic


def _values_block() -> str:
    body = _body()
    start = body.index("lateral (values")
    end = body.index("as v(step_no", start)
    return body[start:end]


def _step(key: str) -> str:
    """The one VALUES row for a step key, squeezed to single spaces."""
    block = _squeeze(_values_block())
    m = re.search(r"\(\s*\d+::smallint[^()]*?'" + re.escape(key) + r"'.*?\)\s*(?:,|$)",
                  block)
    assert m, f"no waterfall row for {key}"
    return m.group(0)


def test_the_six_steps_are_all_there() -> None:
    for key in (
        "all_listings", "not_served", "served", "served_with_verdict",
        "served_located", "hidden",
    ):
        assert _step(key)


def test_every_step_carries_its_own_split_where_the_operator_asked_for_one() -> None:
    """The 41,362 and the 28,174 the operator named are sub-rows of the not-served
    step, not a second query someone has to remember to run."""
    for key in (
        "not_served_no_verdict", "not_served_verdict_no_location",
        "not_served_located",          # the not-served split
        "located_town", "located_foreign", "located_no_town",
        "hidden_unresolved", "hidden_pending",
    ):
        assert _step(key)


def test_lost_is_the_previous_chain_step_minus_this_one() -> None:
    """Written as a DIFFERENCE in the SQL, never as a hand-typed number. RED by:
    `g.served, 87756` or any expression that is not the two counts subtracted."""
    assert "g.all_listings, 0::bigint" in _squeeze(_step("all_listings"))
    assert "g.served, g.all_listings - g.served" in _squeeze(_step("served"))
    assert (
        "g.served_verdict, g.served - g.served_verdict"
        in _squeeze(_step("served_with_verdict"))
    )
    assert (
        "g.served_located, g.served_verdict - g.served_located"
        in _squeeze(_step("served_located"))
    )


def test_the_rows_that_do_not_narrow_the_chain_carry_no_loss() -> None:
    """A deduction is a set carved out, and a split partitions its parent; calling
    either one a "loss" would double-count it down the column."""
    for key in ("not_served", "hidden", "hidden_unresolved", "located_town"):
        assert "null::bigint" in _squeeze(_step(key))


def test_the_share_is_measured_against_the_whole_database() -> None:
    """The wave in one expression: every share is n / all_listings, so the hidden
    set is never reported as a percentage of itself."""
    assert "v.n::numeric * 100 / nullif(g.all_listings, 0)" in _squeeze(_body())


def test_the_hidden_split_comes_from_the_relation_the_page_lists() -> None:
    """`12,054 + 14` must equal `12,068` by construction: the split is a join to
    `location_pin_audit_mv` inside the SAME statement as the chain, and a hidden
    row the last refresh never saw is 'pending' — it arrived since."""
    body = _squeeze(_body())
    assert "left join location_pin_audit_mv a on a.listing_id = b.listing_id" in body
    assert "coalesce(a.state, 'pending') = 'unresolved'" in body
    assert "coalesce(a.state, 'pending') = 'pending'" in body


def test_the_apply_proves_the_four_laws() -> None:
    """The DO block is the part that runs against real numbers; a migration that
    stopped proving them would ship a wrong `lost` straight to the operator."""
    body = _body()
    assert "kind = 'chain'" in body and "order by step_no" in body
    assert "the chain says" in _sql()          # law 1
    assert "not_served % <> % - %" in _sql()   # law 2
    assert "sub-rows of % sum to" in _sql()    # law 3
    assert "is not n/total" in _sql()          # law 4


# --------------------------------------------------------------- the plumbing


def test_the_refresh_is_the_one_that_already_runs_hourly() -> None:
    """No second job and no second cadence: the waterfall is written by
    `refresh_location_pin_audit_mv()`, AFTER the matview refresh so the hidden
    split reads the relation the page is about to list."""
    body = _body()
    assert "create or replace function refresh_location_pin_audit_mv()" in body
    assert "refresh materialized view concurrently location_pin_audit_mv;" in body
    refresh = body.index("refresh materialized view concurrently")
    call = body.index("refresh_location_audit_waterfall()", refresh)
    assert call > refresh
    # And it declares its own freshness (Corollary E), stamped by its producer.
    assert "stamp_derived_artifact(\n    'location_audit_waterfall'" in body
    assert "'location_audit_waterfall', 'refresh_location_pin_audit_mv', 'pg_cron'" in body


def test_the_browser_role_can_read_it_and_anon_cannot() -> None:
    body = _body()
    assert "grant select on location_audit_waterfall to authenticated;" in body
    assert "revoke all on location_audit_waterfall from anon, authenticated;" in body
    assert "enable row level security" in body
    assert "for select to authenticated" in body
    # SELECT only — the RLS-grants gate rejects browser DML on a non-tenant table.
    assert not re.search(r"grant\s+(insert|update|delete|all)\b.*location_audit_waterfall",
                         body)


def test_the_file_applies_statement_by_statement() -> None:
    """The apply workflow retries a lock_timeout from statement 1, so a partial
    apply has to be resumable (migration 514's contract)."""
    body = _body().lower()
    assert "begin;" not in body and "commit;" not in body
    assert "set lock_timeout = '5s';" in body
    assert "create table if not exists location_audit_waterfall" in body


# ------------------------------------------------------------ the read contract


def test_the_page_reads_every_column_the_migration_writes() -> None:
    """A PostgREST select list naming a column the table does not publish is a 400
    at runtime and nothing catches it before the operator opens the page."""
    declared = set(
        re.findall(
            r"^\s{2}(\w+)\s+(?:text|smallint|bigint|numeric|timestamptz)",
            _sql().split("create table if not exists location_audit_waterfall")[1]
            .split(");")[0],
            re.MULTILINE,
        )
    )
    reader = READER.read_text(encoding="utf-8")
    cols = re.search(r"const COLS = \[(.*?)\]", reader, re.DOTALL)
    assert cols, "the reader publishes no column list"
    read = set(re.findall(r"'(\w+)'", cols.group(1)))
    assert read <= declared, f"the reader asks for columns the table lacks: {read - declared}"
    assert {"step_key", "n", "lost", "share_pct", "label_cs"} <= read


def test_the_page_computes_no_step_of_its_own() -> None:
    """The one rule the client side has: it renders what the store wrote — a client
    that recomputed a step would be a second definition of the same question. Scoped
    to the waterfall's own code (the summary matrix below it legitimately sums its
    payload). RED by: deriving `lost` or a share in the component."""
    page = PAGE.read_text(encoding="utf-8")
    start = page.index("function WaterfallTable(")
    component = page[start: page.index("\n}", start)]
    reader = READER.read_text(encoding="utf-8")
    for name, text in (("WaterfallTable", component), ("locationWaterfall.ts", reader)):
        for bad in (".n -", "reduce(", "* 100"):
            assert bad not in text, f"{name} recomputes a waterfall number ({bad})"
    # It renders exactly what the store wrote.
    for field in ("row.n", "row.lost", "row.share_pct", "row.label_cs"):
        assert field in component
