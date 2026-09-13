"""W5: consumers serve a listing only when its location is resolved or foreign.

The operator's ruling of 2026-09-13 in one sentence: a listing is visible to Browse,
the map, the feed, the watchdog and dedup only once `listing_location` has an ANSWER
for it — a point, or the determination that it is abroad. No flag, no new column: the
rule reads the store, so it covers the migration-510 audit set, every future listing
whose page carries no location, and nothing else.

There is exactly ONE spelling of it — `location_data.claims_common`'s
`SERVED_LOCATION_PREDICATE` — and this file is the rail that keeps it that way:

1. THE PIN. Every SQL surface that had to carry the rule in a migration carries the
   RENDERED CONSTANT character for character. A migration is not importable at runtime,
   so the only thing standing between the two texts is this test.
2. WHERE IT LANDS. `browse_projection` (which `browse_list` and `properties_map_mv`
   both materialize) and `listing_feed_public` gain a WHERE and not one column; the
   detail-by-id surfaces gain nothing at all.
3. WHERE THE CODE CARRIES IT. The watchdog matcher reads `properties_public`, a detail
   surface that deliberately keeps serving unresolved rows, so it cannot inherit the
   rule and renders it instead. Path C candidate generation renders it too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from location_data.claims_common import (
    SERVED_LOCATION_PREDICATE,
    served_location_predicate,
)
# The column-list parser is W3's, not a second copy: the two rails ask the same
# question of the same files, and a divergent parser would make one of them lie.
from tests.test_location_w3_projection import _LINE_COMMENT, _columns

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "migrations"
W4C = MIGRATIONS / "508_location_w4c_legacy_drops.sql"
W5 = MIGRATIONS / "512_location_w5_serve_resolved.sql"

# The two LIST surfaces the rule lands on, with the listing-id expression each one keys
# it to. `browse_projection` is property-grain and renders its DISPLAY listing;
# `listing_feed_public` is listing-grain and renders its own row.
SURFACES = {
    "browse_projection": "p.repr_listing_ref_id",
    "listing_feed_public": "l.id",
}

# Reachable by direct link, and therefore deliberately NOT filtered: the listing page,
# the extension, the watchdog's own match relation, the operator's kanban cards, and
# every link the migration-510 audit page offers into the exempted set itself.
DETAIL_SURFACES = ("listings_public", "properties_public", "pipeline_board_public")


def _sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


_VIEW_RE = "create\\s+(?:or\\s+replace\\s+)?view\\s+(?:public\\.)?{}\\b"


def _view_body(sql: str, view: str) -> str:
    """The whole `create view <view> ... ;` statement, comments included.

    The statement terminator is found in a copy with every `--` comment blanked out —
    `listing_feed_public` carries "legacy compat only; NULL post-Gate-2" on a column,
    and taking the first raw `;` cut the view off at its fourth column."""
    m = re.search(_VIEW_RE.format(re.escape(view)), sql, re.IGNORECASE)
    assert m, f"no create view {view} here"
    masked = re.sub(r"--[^\n]*", lambda c: " " * len(c.group(0)), sql)
    end = masked.index(";", m.end())
    return sql[m.start(): end + 1]


# --------------------------------------------------------------------------- the pin


def test_the_rule_has_exactly_one_shape() -> None:
    """A change to the constant is a change to every surface, so the shape itself is
    pinned: an EXISTS against the one store, both arms of the answer, and nothing else
    (no `is_active`, no granularity floor, no confidence — the ruling names none)."""
    assert SERVED_LOCATION_PREDICATE == (
        "EXISTS (SELECT 1 FROM listing_location sl"
        " WHERE sl.listing_id = l.id"
        " AND (sl.geom IS NOT NULL OR sl.country_status = 'foreign'))"
    )
    assert served_location_predicate("x.y") == SERVED_LOCATION_PREDICATE.replace(
        "sl.listing_id = l.id", "sl.listing_id = x.y"
    )


def test_the_inner_alias_never_collides_with_the_label_join() -> None:
    """`sl`, not `ll`: every surface that carries this rule already joins
    `listing_location ll` for `display_label` and the chip codes, and a repeated alias
    is a 42712 at apply time — or, worse, a silently self-joined subquery."""
    assert "listing_location sl" in SERVED_LOCATION_PREDICATE
    for view, key in SURFACES.items():
        body = _view_body(_sql(W5), view)
        assert "left join listing_location ll" in body.lower()
        assert served_location_predicate(key) in body


@pytest.mark.parametrize("view,key", sorted(SURFACES.items()))
def test_migration_512_carries_the_constant_verbatim(view: str, key: str) -> None:
    """THE PIN. RED by: hand-editing either text, or re-keying one surface without the
    other. `pg_get_viewdef` re-prints a parsed tree, so the migration's own DO block
    cannot check this — only an offline comparison against the importable constant can."""
    body = _view_body(_sql(W5), view)
    assert served_location_predicate(key) in body, (
        f"{view} in migration 512 does not carry SERVED_LOCATION_PREDICATE rendered on "
        f"`{key}`. The two texts are the same rule and must be the same characters."
    )
    # And the comment that says where it came from, so the next reader finds the constant.
    assert "SERVED_LOCATION_PREDICATE" in body


# ------------------------------------------------------------------ what 512 may change


@pytest.mark.parametrize("view", sorted(SURFACES))
def test_512_changes_a_where_and_not_a_column(view: str) -> None:
    """A WHERE is the whole change. `create or replace view` cannot reposition or retype
    an existing column, and `sync_browse_list` inserts into `browse_list` POSITIONALLY
    (toolkit/browse_read_model.py) — a reorder writes every value into the wrong column
    with no error anywhere. Every PostgREST select against the feed keeps compiling too."""
    stripped = {m: _LINE_COMMENT.sub("", _sql(m)) for m in (W5, W4C)}
    assert _columns(stripped[W5], view) == _columns(stripped[W4C], view)


@pytest.mark.parametrize("view", DETAIL_SURFACES)
def test_512_leaves_every_detail_surface_alone(view: str) -> None:
    """The ruling hides rows from CONSUMERS. A direct link to an unresolved listing must
    keep working, and a pipeline card the operator created must never vanish from the
    board — rule 22 makes it operator state, not a market read."""
    assert not re.search(_VIEW_RE.format(re.escape(view)), _sql(W5), re.IGNORECASE), (
        f"migration 512 redefines {view}, which is a detail-by-id surface"
    )


def test_512_forces_both_rebuilds_and_proves_the_rule_landed() -> None:
    """`browse_list` and `properties_map_mv` are MATERIALIZATIONS of the projection, so
    until each is rebuilt the rule is live in the view and absent from what Browse and
    the map actually read."""
    sql = _sql(W5).lower()
    assert "select rebuild_browse_list();" in sql
    assert "select rebuild_properties_map_mv();" in sql
    assert "set statement_timeout = '900s';" in sql
    # Statement autocommit: the apply workflow retries a lock_timeout from statement 1,
    # so a partial apply has to be resumable.
    assert "begin;" not in sql and "commit;" not in sql
    assert "set lock_timeout = '5s';" in sql
    assert "pg_get_viewdef" in sql and "pg_get_functiondef" in sql


# ------------------------------------------------------------------ the code surfaces


def test_watchdog_matcher_renders_the_rule_first_and_unconditionally() -> None:
    """Rule 16: Browse and the Watchdog share one definition of "matches". Browse
    inherits the rule from `browse_projection`; the matcher reads `properties_public`
    and cannot, so it renders the constant itself. RED by: making it conditional on a
    spec field — an unresolved listing must never fire a notification for ANY filter."""
    from api.notifications import WatchdogFilterSpec, _build_match_clauses

    where, _ = _build_match_clauses(WatchdogFilterSpec())
    assert where[0] == served_location_predicate("l.listing_id")


def test_dedup_path_c_refuses_the_exempted_set() -> None:
    """Path C blocks on `obec_kod`, which excludes almost all of the exempted set — but
    not all of it: a row can carry a town with a NULL geom (34 live rows, 2026-09-13),
    and those are exactly the rows the ruling exempts. Spelled with the ONE definition,
    so a change to the rule reaches this lane too."""
    from toolkit.dedup_candidates_sql import _BASE_CTE

    assert served_location_predicate("l.listing_id") in _BASE_CTE


def test_verify_pipeline_counts_the_hidden_set_with_the_same_rule() -> None:
    """The number is visible outside the audit page, per portal, and it is a WORKLOAD
    number — it never moves the check's status."""
    from scripts.verify_pipeline import _LOCATION_TOWN_COVERAGE_SQL

    assert f"NOT {SERVED_LOCATION_PREDICATE}" in _LOCATION_TOWN_COVERAGE_SQL
    assert "AS hidden_n" in _LOCATION_TOWN_COVERAGE_SQL


def test_comparables_keeps_the_rules_narrower_form() -> None:
    """`_shared_filter_where` does not render the rule, and must not start: its first
    clause is `ll.geom IS NOT NULL`, which is the rule minus the foreign arm — strictly
    narrower, because a comparable needs a POINT. This pins the clause that makes the
    omission correct, so deleting it cannot silently widen the estimation cohort."""
    from toolkit.comparables import ComparableFilters, TargetSpec, _shared_filter_where

    where, _ = _shared_filter_where(
        TargetSpec(lat=50.08, lng=14.42), ComparableFilters(radius_m=1000),
    )
    assert where[0] == "ll.geom IS NOT NULL"
