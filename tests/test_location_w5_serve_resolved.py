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
W5 = MIGRATIONS / "514_location_w5_serve_resolved.sql"

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
def test_migration_514_carries_the_constant_verbatim(view: str, key: str) -> None:
    """THE PIN. RED by: hand-editing either text, or re-keying one surface without the
    other. `pg_get_viewdef` re-prints a parsed tree, so the migration's own DO block
    cannot check this — only an offline comparison against the importable constant can."""
    body = _view_body(_sql(W5), view)
    assert served_location_predicate(key) in body, (
        f"{view} in migration 514 does not carry SERVED_LOCATION_PREDICATE rendered on "
        f"`{key}`. The two texts are the same rule and must be the same characters."
    )
    # And the comment that says where it came from, so the next reader finds the constant.
    assert "SERVED_LOCATION_PREDICATE" in body


# ------------------------------------------------------------------ what 514 may change


@pytest.mark.parametrize("view", sorted(SURFACES))
def test_514_changes_a_where_and_not_a_column(view: str) -> None:
    """A WHERE is the whole change. `create or replace view` cannot reposition or retype
    an existing column, and `sync_browse_list` inserts into `browse_list` POSITIONALLY
    (toolkit/browse_read_model.py) — a reorder writes every value into the wrong column
    with no error anywhere. Every PostgREST select against the feed keeps compiling too."""
    stripped = {m: _LINE_COMMENT.sub("", _sql(m)) for m in (W5, W4C)}
    assert _columns(stripped[W5], view) == _columns(stripped[W4C], view)


@pytest.mark.parametrize("view", DETAIL_SURFACES)
def test_514_leaves_every_detail_surface_alone(view: str) -> None:
    """The ruling hides rows from CONSUMERS. A direct link to an unresolved listing must
    keep working, and a pipeline card the operator created must never vanish from the
    board — rule 22 makes it operator state, not a market read."""
    assert not re.search(_VIEW_RE.format(re.escape(view)), _sql(W5), re.IGNORECASE), (
        f"migration 514 redefines {view}, which is a detail-by-id surface"
    )


def test_514_forces_both_rebuilds_and_proves_the_rule_landed() -> None:
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


# --------------------------------------------------- the operator's audit surface

# The audit page's read contract (frontend/src/lib/pinAudit.ts `ROW_COLS`). It is
# a PostgREST select list, so a column the matview stops publishing is a 400 at
# runtime and nothing catches it before the operator opens the page.
PIN_AUDIT_TS = REPO / "frontend" / "src" / "lib" / "pinAudit.ts"


def _audit_matview_relation_file() -> Path:
    """The migration that LAST created the relation, not 514 forever. A matview
    cannot gain or lose a column in place, so every change to it is another
    DROP + CREATE in a new file (515 dropped `old_evidence`, 518 added `state`);
    a rail pinned to 514 would keep checking a body production no longer has."""
    creators = sorted(
        f
        for f in MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")
        if "create materialized view location_pin_audit_mv as" in _sql(f)
    )
    assert creators, "no migration creates location_pin_audit_mv"
    return creators[-1]


def _audit_matview_sql() -> str:
    sql = _sql(_audit_matview_relation_file())
    start = sql.index("create materialized view location_pin_audit_mv as")
    masked = re.sub(r"--[^\n]*", lambda c: " " * len(c.group(0)), sql)
    return sql[start: masked.index(";", start) + 1]


def test_514_recreates_the_audit_surface_513_retired() -> None:
    """513 dropped the matview, both functions, the cron job and the registry row,
    because v1 read five `properties` place columns that 508 removes. W5 brings the
    surface back on a definition that cannot expire the same way."""
    sql = _sql(W5).lower()
    for obj in (
        "create materialized view location_pin_audit_mv as",
        "create unique index if not exists location_pin_audit_mv_pk",
        "create function location_pin_audit_summary()",
        "create or replace function refresh_location_pin_audit_mv()",
        "'refresh-location-pin-audit'",
        "insert into public.derived_artifacts",
    ):
        assert obj in sql, f"migration 514 does not restore `{obj}`"
    # pg_cron is absent from the CI replay container, so the schedule must be
    # guarded or the whole migration fails there (migrations 136/274/510).
    assert "create extension if not exists pg_cron" in sql
    assert "pg_cron unavailable" in sql


def test_the_audit_set_is_the_hidden_set_and_nothing_else() -> None:
    """The point of the surface: the page and the rule are ONE definition. Since W15
    (operator ruling 2026-09-14) the cohort is that rule and NOTHING else — every
    listing that fails `SERVED_LOCATION_PREDICATE`, rendered verbatim and negated, so
    a row that is merely foreign is not an audit finding and a delisted one is."""
    body = _audit_matview_sql()
    squeeze = lambda t: " ".join(t.split())  # noqa: E731
    flat = squeeze(body)
    assert squeeze("not " + SERVED_LOCATION_PREDICATE) in flat, (
        "the audit cohort does not negate SERVED_LOCATION_PREDICATE verbatim"
    )
    # RED by: asking the same question a second way off the left join, which is how
    # the page and Browse would end up on two definitions of "visible".
    assert "ll.geom is null" not in body


def test_the_audit_cohort_carries_no_second_definition_of_what_counts() -> None:
    """W15 retired the served set (`SERVED_LISTING_PREDICATE`): the lane covers every
    listing, and a delisted listing with no location is a finding like any other. RED
    by: a `properties`/`repr_listing_ref_id` arm sneaking back into the cohort."""
    body = _audit_matview_sql().lower()
    assert "repr_listing_ref_id" not in body
    assert "from properties" not in body
    # ONE arm driven from `listings`, not 514's union of two candidate sets.
    assert "from listings l" in body
    assert "with candidates as" not in body


def test_the_audit_view_names_no_dropped_properties_column() -> None:
    """Exactly what killed v1: it selected `p.street` / `p.locality` / `p.district`
    / `p.lat` / `p.lng`, so Postgres recorded a column dependency and 508's DROP
    COLUMN could not run until 513 retired the whole surface."""
    body = _audit_matview_sql()
    for col in ("street", "locality", "district", "lat", "lng", "geom"):
        assert not re.search(r"(?<![a-z_])p\." + col + r"\b", body), (
            f"the audit matview reads properties.{col} — the v1 failure, verbatim"
        )


def test_the_audit_page_reads_only_columns_the_matview_publishes() -> None:
    body = _audit_matview_sql()
    published = set(re.findall(r"\bas\s+([a-z_0-9]+)", body))
    published |= set(re.findall(r"(?<![a-z_])(?:l|ll|c)\.([a-z_0-9]+)", body))
    ts = PIN_AUDIT_TS.read_text(encoding="utf-8")
    cols = re.search(r"const ROW_COLS = \[(.*?)\]", ts, re.DOTALL)
    assert cols, "pinAudit.ts no longer declares ROW_COLS"
    wanted = set(re.findall(r"'([a-z_0-9]+)'", cols.group(1)))
    assert wanted <= published, (
        f"the page selects {sorted(wanted - published)} off location_pin_audit_mv, "
        f"which the matview does not publish — PostgREST answers that with a 400"
    )


def test_the_map_is_gone_from_the_audit_page() -> None:
    """The whole subject of the page is rows with NO point to draw; v1's map drew
    their LEGACY coordinates, and 508 deleted those columns. Less, not a broken map."""
    assert not (REPO / "frontend" / "src" / "components" / "PinAuditMap.tsx").exists()
    for rel in ("frontend/src/lib/pinAudit.ts", "frontend/src/pages/LocationPinAudit.tsx"):
        text = (REPO / rel).read_text(encoding="utf-8")
        for gone in ("legacy_lat", "legacy_lng", "PinAuditMap", "PIN_AUDIT_MAP_CAP"):
            assert gone not in text, f"{rel} still names {gone}"


def test_the_audit_page_is_routed_and_leads_the_nav() -> None:
    """513 unrouted the page. It comes back as the FIRST top-level nav entry with
    its count — the badge is a work queue that counts down on its own."""
    routes = (REPO / "frontend" / "src" / "lib" / "routes.ts").read_text(encoding="utf-8")
    assert "newDedupPinAudit: def('/new-dedup/pin-audit')" in routes
    assert "ROUTES.newDedupPinAudit.childPath" in (
        REPO / "frontend" / "src" / "routes.tsx"
    ).read_text(encoding="utf-8")
    shell = (REPO / "frontend" / "src" / "components" / "Shell.tsx").read_text(encoding="utf-8")
    start = shell.index("const navItems")
    nav = shell[start: shell.index("];", start)]
    assert nav.index("'!AUDIT POLOH'") < nav.index("'Browse'"), (
        "!AUDIT POLOH is no longer the first nav entry"
    )


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


def test_the_dedup_funnel_is_cut_with_the_same_rule_as_the_lane_it_describes() -> None:
    """W16. The counterpart the rail above was missing: FUNNEL_SQL is the READOUT of the
    same population `_BASE_CTE` pairs, and until W16 it was cut one step looser — no geom
    test, no foreign test — so "placed precisely enough to name a town" counted rows the
    generator then dropped, and its loss merged three unlike things. RED by: the readout
    drifting from the lane again."""
    from toolkit.dedup_candidates_sql import FUNNEL_SQL

    assert served_location_predicate("l.id") in FUNNEL_SQL


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
