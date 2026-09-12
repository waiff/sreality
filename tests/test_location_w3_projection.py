"""W3 S1 rails: the serving views may only APPEND, and there is ONE label rule.

Two invariant families, both offline (the schema-aware sweep that actually
compiles this SQL is tests/test_sql_schema_prepare.py, CI's DB lane):

1. APPEND-ONLY. `browse_projection` is materialized by `browse_list` and
   `properties_map_mv`, and `toolkit/browse_read_model.sync_browse_list` patches
   `browse_list` with a POSITIONAL `INSERT ... SELECT * FROM browse_projection`.
   Postgres itself refuses a `create or replace view` that repositions or retypes
   an existing column, but it cannot see across migration FILES: this asserts
   that each view's previous column list is a strict PREFIX of its new one, so a
   future edit that "tidies" the order fails here rather than at apply time (or,
   worse, after an apply that dropped and recreated the view).

2. ONE LABEL. Decision W3-2 fixes both the inputs and the fallback order of the
   place string. Every serving view must call `location_display_label` — no view
   may compose a label of its own — and the function must take exactly the seven
   `listing_location` columns W3-2 names, in a CASE whose branches run
   foreign -> street -> cast obce -> obec -> NULL.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

# (view, migration that defined it BEFORE W3, migration that widens it)
_WIDENED = [
    ("browse_projection", "475_new_dedup_teardown_finish.sql", "503_location_w3_serving_views.sql"),
    ("listing_feed_public", "475_new_dedup_teardown_finish.sql", "503_location_w3_serving_views.sql"),
    ("properties_public", "475_new_dedup_teardown_finish.sql", "503_location_w3_serving_views.sql"),
    ("listings_public", "494_listings_public_source_url.sql", "503_location_w3_serving_views.sql"),
    ("pipeline_board_public", "425_measure_price_per_m2.sql", "503_location_w3_serving_views.sql"),
    ("broker_listings_public", "224_broker_listings_public_subtype.sql", "503_location_w3_serving_views.sql"),
]

# The seven inputs of the label, in the order W3-2 names them. The house number
# pair is two columns because Czech addresses carry two numbers (popisné /
# orientační) and the label renders them as "cp/co".
_LABEL_INPUTS = [
    "street_name",
    "house_number_cp",
    "house_number_co",
    "obec_name",
    "cast_obce_name",
    "country_code",
    "country_status",
]

_LINE_COMMENT = re.compile(r"--.*$", re.MULTILINE)


def _sql(name: str) -> str:
    return _LINE_COMMENT.sub("", (MIGRATIONS / name).read_text(encoding="utf-8"))


def _view_select_list(sql: str, view: str) -> str:
    """The text between `create [or replace] view <view> [with (...)] as select`
    and the view's own top-level FROM."""
    pat = re.compile(
        rf"create\s+(?:or\s+replace\s+)?view\s+(?:public\.)?{re.escape(view)}\b",
        re.IGNORECASE,
    )
    m = pat.search(sql)
    assert m, f"no create view {view} in this migration"
    rest = sql[m.end():]
    # skip an optional `with (security_invoker = true)` before `as select`
    sel = re.search(r"\bas\s+select\b", rest, re.IGNORECASE)
    assert sel, f"{view}: no `as select` after the view name"
    body = rest[sel.end():]
    depth = 0
    for i, ch in enumerate(body):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and body.startswith("from", i) and (i == 0 or not body[i - 1].isalnum()):
            after = body[i + 4: i + 5]
            if after in ("", " ", "\n", "\t"):
                return body[:i]
    raise AssertionError(f"{view}: no top-level FROM found")


def _columns(sql: str, view: str) -> list[str]:
    """Output column names of `view`, IN ORDER. Alias when present, else the
    token after the final dot."""
    items, depth, cur = [], 0, []
    for ch in _view_select_list(sql, view):
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
    out: list[str] = []
    for item in items:
        item = " ".join(item.split()).strip()
        if not item:
            continue
        m = re.search(r"\bas\s+([a-zA-Z_0-9]+)\s*$", item, re.IGNORECASE)
        out.append((m.group(1) if m else item.rsplit(".", 1)[-1]).lower())
    return out


# ------------------------------------------------------------------ append-only


@pytest.mark.parametrize("view,before,after", _WIDENED, ids=[v for v, _, _ in _WIDENED])
def test_view_only_appends(view: str, before: str, after: str) -> None:
    old = _columns(_sql(before), view)
    new = _columns(_sql(after), view)
    assert new[: len(old)] == old, (
        f"{view}: migration {after} does not APPEND to {before}'s column list — the "
        f"first divergence is at position "
        f"{next(i for i, (a, b) in enumerate(zip(old, new)) if a != b)}. "
        f"`create or replace view` cannot reposition or rename an existing output "
        f"column, and `sync_browse_list` inserts into browse_list POSITIONALLY "
        f"(toolkit/browse_read_model.py) — a reorder writes every value into the "
        f"wrong column."
    )
    assert len(new) > len(old), f"{view}: nothing appended — is this the right migration?"


def test_browse_projection_appends_exactly_the_w3_four() -> None:
    old = _columns(_sql("475_new_dedup_teardown_finish.sql"), "browse_projection")
    new = _columns(_sql("503_location_w3_serving_views.sql"), "browse_projection")
    assert new[len(old):] == [
        "display_label", "cast_obce_id", "uncertainty_radius_m", "granularity_rank",
    ]


def test_browse_projection_resources_the_codes_and_the_pin() -> None:
    """The five re-sourced columns keep their NAMES (so every reader and every
    index keeps working) and change their SOURCE to listing_location."""
    body = " ".join(_view_select_list(
        _sql("503_location_w3_serving_views.sql"), "browse_projection").split())
    for frag in (
        "ll.obec_kod as obec_id",
        "ll.okres_kod as okres_id",
        "ll.kraj_kod as region_id",
        "st_y(ll.geom) as lat",
        "st_x(ll.geom) as lng",
    ):
        assert frag in body, f"browse_projection no longer re-sources `{frag}`"


def test_no_window_function_in_the_projection() -> None:
    """W3-2 refused the shared-pin count. `sync_browse_list` filters
    `WHERE property_id = ANY(...)`, and that qual cannot be pushed below a window
    function — every merge would aggregate the whole corpus."""
    body = _view_select_list(
        _sql("503_location_w3_serving_views.sql"), "browse_projection").lower()
    assert " over (" not in body and " over(" not in body


# -------------------------------------------------------------------- one label


def _label_function_body() -> str:
    sql = _sql("503_location_w3_serving_views.sql")
    m = re.search(
        r"create\s+or\s+replace\s+function\s+location_display_label\s*\((.*?)\)\s*returns\s+text(.*?)\$fn\$",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert m, "location_display_label is not defined in migration 503"
    body = re.search(r"\$fn\$(.*?)\$fn\$", sql, re.DOTALL)
    assert body, "location_display_label has no $fn$-quoted body"
    return body.group(1)


def test_label_takes_exactly_the_w3_2_inputs_in_order() -> None:
    sql = _sql("503_location_w3_serving_views.sql")
    m = re.search(
        r"create\s+or\s+replace\s+function\s+location_display_label\s*\((.*?)\)\s*returns\s+text",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert m, "location_display_label is not defined in migration 503"
    params = [p.strip().split()[0] for p in m.group(1).split(",")]
    assert params == [f"p_{c}" for c in _LABEL_INPUTS], (
        "the label's inputs drifted from decision W3-2. It is composed from "
        "exactly these seven listing_location columns, in this order: "
        f"{_LABEL_INPUTS}"
    )


def test_label_fallback_order_is_exactly_w3_2() -> None:
    """foreign -> street (+ cp/co) -> cast obce -> obec -> NULL.

    Order is the rule, not a detail: putting the town branch above the street one
    would silently coarsen every address in the product, and a test that only
    checked "the branches exist" would pass."""
    body = _label_function_body()
    marks = [
        "p_country_status = 'foreign'",          # 1. a DETERMINATION, never a default
        "nullif(btrim(p_street_name), '')",      # 2. street wins when there is one
        "nullif(btrim(p_cast_obce_name), '')",   # 3. only when it differs from the town
        "else nullif(btrim(p_obec_name), '')",   # 4. the town alone; 5. NULL is the else
    ]
    positions = []
    for mark in marks:
        i = body.find(mark)
        assert i != -1, f"the label lost its `{mark}` branch"
        positions.append(i)
    assert positions == sorted(positions), (
        f"the label's branches are out of order: {marks}"
    )
    # The house number renders as the Czech cp/co pair, not as two separate tokens.
    assert "concat_ws('/'," in body
    # The part-of-town branch must not repeat the town ("Brno, Brno").
    assert "btrim(p_cast_obce_name) <> btrim(p_obec_name)" in body


@pytest.mark.parametrize("view", [v for v, _, _ in _WIDENED])
def test_every_serving_view_calls_the_one_label(view: str) -> None:
    """No view composes a place string of its own — including the two that read
    it from another view rather than computing it."""
    sql = _sql("503_location_w3_serving_views.sql")
    select_list = " ".join(_view_select_list(sql, view).split())
    assert "as display_label" in select_list or "p.display_label" in select_list, (
        f"{view} does not publish display_label"
    )
    if "as display_label" in select_list:
        call = re.search(
            r"location_display_label\s*\((.*?)\)\s*as\s+display_label", select_list, re.DOTALL
        )
        assert call, f"{view} publishes display_label without calling location_display_label"
        args = [a.strip() for a in call.group(1).split(",")]
        assert args == [f"ll.{c}" for c in _LABEL_INPUTS], (
            f"{view} calls the label with {args} — it must pass exactly the seven "
            f"listing_location columns W3-2 names, in order"
        )
