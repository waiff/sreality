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

W3 = "503_location_w3_serving_views.sql"
W3_S3 = "504_location_w3_one_code_predicate.sql"
W3_S4 = "506_location_w3_s4_deletions.sql"
W4A = "507_location_w4a_readers.sql"
W4C = "508_location_w4c_legacy_drops.sql"

# What S4 removes from each view it re-creates. `obec` SURVIVES on the two views
# the pipeline board reads: the board's town sort orders by the TOWN, which is
# the tail of `display_label` rather than its head (lib/pipelineSort).
_S4_DROPS: dict[str, set[str]] = {
    "browse_projection": {
        "locality", "district", "street", "obec", "okres", "region",
        "place_search_text", "home_city_id",
    },
    "listing_feed_public": {"place_search_text"},
    # `district` stays HERE and nowhere else: region_stats() and
    # region_active_by_day() (migrations 425/103) filter on it by NAME off this
    # view, and CI's schema-replay lane runs both.
    "properties_public": {
        "locality", "street", "okres", "region",
        "place_search_text", "home_city_id",
    },
    "pipeline_board_public": {
        "locality", "district", "street", "okres", "region", "place_search_text",
    },
}

# The six views migration 503 widens. The migration each one was defined by
# BEFORE 503 is DERIVED, never listed: hard-coding it is how the first cut of
# this file passed while the migration itself failed to apply — it compared
# `broker_listings_public` against 224 when 358 had already appended
# `listing_id`, so 503 was silently dropping a live column and Postgres refused
# it ("cannot change name of view column"). Same max-numeric-prefix rule
# tests/test_browse_read_path_guardrail.py uses for "the effective definition".
_WIDENED = [
    "browse_projection",
    "listing_feed_public",
    "properties_public",
    "listings_public",
    "pipeline_board_public",
    "broker_listings_public",
]


def _num(name: str) -> int:
    return int(name.split("_", 1)[0])


def _previous_definition(view: str, below: str = W3) -> str:
    """Filename of the highest-numbered migration below `below` that defines `view`."""
    pat = re.compile(
        rf"create\s+(or\s+replace\s+)?view\s+(public\.)?{re.escape(view)}\b", re.IGNORECASE
    )
    hits = [
        p for p in MIGRATIONS.glob("*.sql")
        if _num(p.name) < _num(below) and pat.search(p.read_text(encoding="utf-8"))
    ]
    assert hits, f"no migration before {below} defines {view}"
    return max(hits, key=lambda p: _num(p.name)).name

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


# Every (migration, view) pair in the W3 sprint that re-creates a serving view.
# S3 (504) appends the fourth chip level, `cast_obce_id`, to the two views a
# place-filtering surface reads.
_WIDENING_STEPS = [(W3, v) for v in _WIDENED] + [
    (W3_S3, "properties_public"),
    (W3_S3, "pipeline_board_public"),
]


@pytest.mark.parametrize("after,view", _WIDENING_STEPS)
def test_view_only_appends(after: str, view: str) -> None:
    before = _previous_definition(view, below=after)
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


# ------------------------------------------------------------------ drops-only


@pytest.mark.parametrize("view", sorted(_S4_DROPS))
def test_s4_drops_only(view: str) -> None:
    """S4 is a DROP VIEW + CREATE VIEW, which is the only way to remove a column
    — and therefore the only place in the sprint where the append-only rail
    above does not apply. Two things must hold instead, and neither is checkable
    by Postgres: the new column list must be the old one MINUS exactly the names
    S4 declares (nothing else silently left), and the survivors must keep their
    RELATIVE ORDER. Order is not cosmetic: `sync_browse_list` inserts into
    `browse_list` POSITIONALLY (toolkit/browse_read_model.py), so a reorder
    writes every value into the wrong column with no error anywhere."""
    before = _previous_definition(view, below=W3_S4)
    old = _columns(_sql(before), view)
    new = _columns(_sql(W3_S4), view)
    dropped = _S4_DROPS[view]

    assert set(old) - set(new) == dropped, (
        f"{view}: migration {W3_S4} removes {sorted(set(old) - set(new))} but S4 "
        f"declares {sorted(dropped)}. A column that loses its last reader is "
        f"deleted in the PR that removes the reader, not silently here."
    )
    assert set(new) - set(old) == set(), (
        f"{view}: {W3_S4} ADDS {sorted(set(new) - set(old))}. S4 only deletes "
        f"(rule 25) — a new column belongs in a widening migration, whose "
        f"append-only rail is test_view_only_appends above."
    )
    assert new == [c for c in old if c not in dropped], (
        f"{view}: the surviving columns were REORDERED. Expected "
        f"{[c for c in old if c not in dropped]}, got {new}."
    )


def test_s4_keeps_the_town_on_the_board_lane() -> None:
    """`obec` is one of two pieces of legacy place text S4 spares, and it is
    spared on the two views the pipeline board reads. If a later edit takes it,
    the board's "Mesto A-Z" sort silently falls back to `display_label` and
    orders a column by house number."""
    for view in ("properties_public", "pipeline_board_public"):
        assert "obec" in _columns(_sql(W3_S4), view), f"{view} lost `obec`"
    assert "obec" not in _columns(_sql(W3_S4), "browse_projection")


def test_s4_keeps_district_where_the_region_functions_read_it() -> None:
    """The other survivor -- until W4-c. `region_stats()` and
    `region_active_by_day()` filter `district = any(districts_filter)` -- a legacy
    NAME array -- off `properties_public`. Neither has a repo caller, but CI's
    schema-replay lane compiles both against a real database, and rule 25 deletes
    a column in the PR that removes its last READER: migration 508 drops the two
    functions, and `district` goes with them (see test_w4c_drops_only)."""
    assert "district" in _columns(_sql(W3_S4), "properties_public")
    for view in ("browse_projection", "pipeline_board_public"):
        assert "district" not in _columns(_sql(W3_S4), view), (
            f"{view} kept `district` -- nothing reads it there"
        )


def test_s4_drops_home_city_ids_last_reader_with_it() -> None:
    """A column and the function that joins on it leave together. Dropping
    `home_city_id` while `listings_with_city_quality()` still selected it would
    leave a function that compiles and fails on its first call -- Postgres does
    not track column dependencies through a function body."""
    sql = _sql(W3_S4)
    assert "drop function if exists listings_with_city_quality" in sql.lower()
    assert "home_city_id" not in _columns(sql, "browse_projection")


def test_browse_projection_appends_exactly_the_w3_four() -> None:
    old = _columns(_sql(_previous_definition("browse_projection")), "browse_projection")
    new = _columns(_sql(W3), "browse_projection")
    assert new[len(old):] == [
        "display_label", "cast_obce_id", "uncertainty_radius_m", "granularity_rank",
    ]


def test_browse_projection_resources_the_codes_and_the_pin() -> None:
    """The five re-sourced columns keep their NAMES (so every reader and every
    index keeps working) and change their SOURCE to listing_location."""
    body = " ".join(_view_select_list(_sql(W3), "browse_projection").split())
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
    body = _view_select_list(_sql(W3), "browse_projection").lower()
    assert " over (" not in body and " over(" not in body


# -------------------------------------------------------------------- one label


def _label_function_body() -> str:
    sql = _sql(W3)
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
    sql = _sql(W3)
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


@pytest.mark.parametrize("view", _WIDENED)
def test_every_serving_view_calls_the_one_label(view: str) -> None:
    """No view composes a place string of its own — including the two that read
    it from another view rather than computing it."""
    sql = _sql(W3)
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


# ------------------------------------------------------------------ W4-a rails

# The two views W4-a re-sources. It appends NOTHING: the reader census behind
# that PR found no consumer left for the legacy place TEXT on either view except
# `properties_public.obec` (the kanban town sort), which is re-sourced IN PLACE
# from `ll.obec_name`. A new `obec_name` column beside a legacy `obec` would have
# left the legacy reader alive, which is the opposite of the wave.
_W4A_VIEWS = ["properties_public", "listings_public"]


@pytest.mark.parametrize("view", _W4A_VIEWS)
def test_w4a_changes_no_column_list(view: str) -> None:
    """A re-source is invisible in the column list, and it must be: `create or
    replace view` cannot rename, retype or reposition an existing column, and
    every reader (PostgREST select lists, `sync_browse_list`'s positional
    INSERT) is written against the list as it stands."""
    before = _previous_definition(view, below=W4A)
    old = _columns(_sql(before), view)
    new = _columns(_sql(W4A), view)
    assert new == old, (
        f"{view}: migration {W4A} changed its column list "
        f"(added {sorted(set(new) - set(old))}, removed {sorted(set(old) - set(new))}). "
        f"W4-a moves WHERE a value comes from, never what is published."
    )


def test_w4a_resources_the_watchdogs_relation() -> None:
    """`properties_public` is what the Watchdog matches on: `ST_DWithin` rebuilt
    from lat/lng (api/notifications.py) and `district_where`'s codes. Until W4-a
    all five came from `properties` columns picked by `best_geo` -- a DIFFERENT
    child than the one the label comes from. RED by: re-pointing any one of them
    back at `p.`."""
    body = " ".join(_view_select_list(_sql(W4A), "properties_public").split())
    for frag in (
        "st_y(ll.geom) as lat",
        "st_x(ll.geom) as lng",
        "ll.obec_kod as obec_id",
        "ll.okres_kod as okres_id",
        "ll.kraj_kod as region_id",
        "ll.obec_name as obec",
    ):
        assert frag in body, f"properties_public does not re-source `{frag}`"
    for legacy in ("p.lat,", "p.lng,", "p.obec_id,", "p.okres_id,", "p.region_id,", "p.obec,"):
        assert legacy not in body, f"properties_public still serves `{legacy}`"


def test_w4a_resources_the_detail_relation() -> None:
    """`listings_public` is the listing detail + the extension + the dispatch
    feed. Its legacy place TEXT survives (those are `listings` columns, and
    removing a view column needs a DROP + CREATE -- W4-c); its PIN and its three
    chip codes do not."""
    body = " ".join(_view_select_list(_sql(W4A), "listings_public").split())
    for frag in (
        "st_y(ll.geom) as lat",
        "st_x(ll.geom) as lng",
        "ll.obec_kod as obec_id",
        "ll.okres_kod as okres_id",
        "ll.kraj_kod as region_id",
    ):
        assert frag in body, f"listings_public does not re-source `{frag}`"
    assert "listings.geom" not in body


def test_w4a_builds_the_geography_index_before_the_readers_flip() -> None:
    """`listing_location.geom` is geometry; every moved reader casts it to
    geography so its radius stays in METRES. `listing_location_geom_gist` (501)
    is a geometry index and cannot serve that expression, so without this one the
    cast turns each radius search into a seq scan of the corpus. CONCURRENTLY,
    therefore outside any transaction -- so this file must carry no begin/commit."""
    sql = _sql(W4A).lower()
    assert "create index concurrently if not exists listing_location_geog_gist" in sql
    assert "using gist ((geom::geography))" in sql
    assert "begin;" not in sql and "commit;" not in sql


# ------------------------------------------------------------------ W4-c drops


# What W4-c removes from each view it re-creates. Same three invariants as the S4
# block above (exact set, nothing added, survivors keep their relative order),
# for the same reason: `sync_browse_list` inserts into `browse_list`
# POSITIONALLY, so a silent reorder writes every value into the wrong column.
_W4C_DROPS: dict[str, set[str]] = {
    "browse_projection": {"locality_district_id", "locality_region_id"},
    "listing_feed_public": {
        "locality", "district", "obec", "okres", "region", "street", "house_number",
    },
    "properties_public": {"district", "locality_district_id", "locality_region_id"},
    "broker_listings_public": {"locality", "district"},
    # Re-created VERBATIM: it only DEPENDS on properties_public, which had to be
    # dropped. Declared here so a future edit that quietly narrows it fails.
    "pipeline_board_public": set(),
}


@pytest.mark.parametrize("view", sorted(_W4C_DROPS))
def test_w4c_drops_only(view: str) -> None:
    before = _previous_definition(view, below=W4C)
    old = _columns(_sql(before), view)
    new = _columns(_sql(W4C), view)
    dropped = _W4C_DROPS[view]

    assert set(old) - set(new) == dropped, (
        f"{view}: migration {W4C} removes {sorted(set(old) - set(new))} but W4-c "
        f"declares {sorted(dropped)}."
    )
    assert set(new) - set(old) == set(), (
        f"{view}: {W4C} ADDS {sorted(set(new) - set(old))}. W4-c only deletes (rule 25)."
    )
    assert new == [c for c in old if c not in dropped], (
        f"{view}: the surviving columns were REORDERED. Expected "
        f"{[c for c in old if c not in dropped]}, got {new}."
    )


def test_w4c_leaves_no_legacy_place_column_on_any_serving_view() -> None:
    """The point of the wave: after 508 not one serving view names a dropped
    base-table column, so the ALTERs below it can succeed."""
    gone = {
        "geom", "obec_id", "okres_id", "region_id", "ku_id", "locality", "district",
        "obec", "okres", "region", "street", "house_number", "zip", "street_id",
        "locality_municipality_id", "locality_quarter_id", "locality_ward_id",
        "locality_district_id", "locality_region_id", "street_name_key",
        "street_source", "geo_cell_key", "coord_street_attempt_version",
        "geocode_attempted_at", "place_search_text",
    }
    sql = _sql(W4C)
    for view in ("browse_projection", "listing_feed_public", "listings_public",
                 "broker_listings_public"):
        # listings_public KEEPS its nine legacy place columns as output names
        # (five matviews depend on the view); what it must not do is read them
        # off `listings` -- they are re-sourced from `ll`.
        body = " ".join(_view_select_list(sql, view).split()).lower()
        for col in sorted(gone):
            # A bare "l.<col>" would also match the "ll.<col>" this wave moved TO.
            assert not re.search(r"(?<![a-z_])l\." + col + r"\b", body), (
                f"{view} still reads listings.{col}"
            )
            assert f"listings.{col}" not in body, f"{view} still reads listings.{col}"
    body = " ".join(_view_select_list(sql, "properties_public").split()).lower()
    for col in sorted(gone):
        assert not re.search(r"(?<![a-z_])p\." + col + r"\b", body), (
            f"properties_public still reads properties.{col}"
        )


def test_w4c_keeps_listings_public_width_and_resources_it_in_place() -> None:
    """The one compatibility surface of the wave, and the reason it exists.
    FIVE matviews hold an object-level dependency on `listings_public`
    (image_storage_overview_mv / scraper_health_checks_mv / health_summary_mv,
    portal_health_mv, category_trends_mv), so a DROP + CREATE would mean
    re-creating and REPOPULATING all five inside the window that holds ACCESS
    EXCLUSIVE on `listings`. Instead it takes an in-place `create or replace`:
    every output column survives, seven are re-sourced from listing_location and
    the two sreality portal ids -- which have no twin and were never a query
    dimension -- become typed NULL."""
    sql = _sql(W4C)
    assert "drop view if exists listings_public" not in sql.lower()
    cols = _columns(sql, "listings_public")
    for kept in ("locality", "district", "street", "house_number", "obec",
                 "okres", "region", "locality_district_id", "locality_region_id"):
        assert kept in cols, f"listings_public lost `{kept}` -- that needs the five matviews"
    body = " ".join(_view_select_list(sql, "listings_public").split())
    for frag in ("ll.obec_name  as locality", "ll.okres_name as district",
                 "ll.street_name      as street", "ll.kraj_name  as region",
                 "null::integer as locality_district_id"):
        assert " ".join(frag.split()) in body, f"listings_public does not re-source `{frag}`"


def test_w4c_drops_the_two_region_functions_that_held_district() -> None:
    """A column and its last reader leave together (rule 25). Both are dropped by
    NAME through pg_proc rather than by a transcribed signature -- region_stats
    carries 40+ parameters across two overloads, and hand-typing a signature is
    how a drop silently targets nothing (migration 428's own header)."""
    sql = _sql(W4C).lower()
    assert "p.proname in ('region_stats', 'region_active_by_day', 'browse_stats')" in sql
    assert "district" not in _columns(_sql(W4C), "properties_public")


def test_w4c_carries_no_transaction_and_one_alter_per_hot_table() -> None:
    """Statement autocommit (the apply workflow retries lock_timeout errors from
    statement 1, so a partial apply must be resumable), `lock_timeout = '5s'`
    never raised, and exactly ONE `alter table listings` -- 24 DROP COLUMN
    clauses under a single ACCESS EXCLUSIVE acquisition on the hottest table."""
    sql = _sql(W4C)
    low = sql.lower()
    assert "begin;" not in low and "commit;" not in low
    assert "set lock_timeout = '5s';" in low
    assert low.count("alter table listings\n") == 1
    assert low.count("alter table properties\n") == 1
    assert sum(1 for line in sql.splitlines()
               if line.strip().startswith("drop column if exists")
               ) == 24 + 16  # listings + properties (place_search_text leads, idempotent)
