"""W6-c: `listings_public` is exactly what its readers read, and the blue-green
that narrowed it leaves nothing behind.

Migration 508 kept the view 61 columns wide because five materialized views hold
an object-level dependency on it and `create or replace view` cannot drop a
column. 517 takes the width by renaming the wide view aside, creating the narrow
one under the original name, rebuilding each matview beside itself as `<name>_next`
and swapping it in. Two things can go wrong silently and both are pinned here:

1. THE WIDTH IS THE READERS. The kept columns must equal `DETAIL_COLS` in
   frontend/src/lib/queries.ts — the SPA's listing-detail select list, and the
   widest reader of the view. A column added back to the migration without a
   reader, or removed from DETAIL_COLS without leaving the view, fails here.
   `npx tsc --noEmit` cannot see this: PostgREST select lists are strings.

2. NO SCAFFOLDING SURVIVES. `_next` is a build artefact. A file that creates one
   and does not swap it leaves a stale duplicate of a multi-million-row matview
   on disk, and — because apply_migration.yml probes the live catalog for every
   object a migration DECLARES — a `_next` visible to that parser would fail the
   receipt even after a perfectly successful apply.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.migration_objects import parse_objects

from tests.test_location_w3_projection import _columns, _sql

ROOT = Path(__file__).resolve().parents[1]
W6C = "517_location_w6c_narrow_public_views.sql"

# The five matviews that hold the dependency, from pg_depend on the live
# catalog: image_storage_overview_mv (115), scraper_health_checks_mv +
# health_summary_mv (354), portal_health_mv (219), category_trends_mv (233).
# portal_health_mv is the one that depends on BOTH public views.
MATVIEWS = (
    "image_storage_overview_mv",
    "scraper_health_checks_mv",
    "health_summary_mv",
    "portal_health_mv",
    "category_trends_mv",
)

# The census result. Four were typed NULL placeholders projecting no data at
# all; ten are the legacy place columns that `display_label` replaced; the last
# three are a broker name and the two condition levels Browse reads elsewhere.
DROPPED = {
    "locality", "district", "locality_district_id", "locality_region_id",
    "street", "house_number", "obec", "okres", "region",
    "obec_id", "okres_id", "region_id",
    "broker_name", "broker_email", "broker_phone",
    "building_condition_level", "apartment_condition_level",
}


# Kept by 517 with a reader then; the MF readers left in the MF render-by-shape
# PR (MF is property-grain: properties_public), and the columns leave the view
# with the stored MF columns in the destructive MF cleanup. Delete this set there.
READERLESS_UNTIL_MF_CLEANUP = {
    "mf_reference_rent_czk", "mf_gross_yield_pct", "mf_reference_rent",
}


def _detail_cols() -> list[str]:
    """`DETAIL_COLS` from frontend/src/lib/queries.ts, by quote pairing.

    The constant is a run of single-quoted fragments joined with `+`, with block
    comments between them — which is why the source carries a standing note that
    no apostrophe may appear inside it."""
    src = (ROOT / "frontend" / "src" / "lib" / "queries.ts").read_text(encoding="utf-8")
    m = re.search(r"\bconst DETAIL_COLS\s*=(.*?);\n", src, re.DOTALL)
    assert m, "DETAIL_COLS not found in frontend/src/lib/queries.ts"
    joined = "".join(re.findall(r"'([^']*)'", m.group(1)))
    return [c for c in joined.split(",") if c]


def test_listings_public_is_exactly_its_readers() -> None:
    cols = _columns(_sql(W6C), "listings_public")
    assert len(cols) == len(set(cols)), f"517 projects a duplicate column: {cols}"
    assert len(cols) == 44, f"517 leaves listings_public {len(cols)} columns wide, expected 44"
    read = set(cols) - READERLESS_UNTIL_MF_CLEANUP
    assert read == set(_detail_cols()), (
        "listings_public and the SPA's DETAIL_COLS disagree — "
        f"only in the view: {sorted(read - set(_detail_cols()))}; "
        f"only in DETAIL_COLS: {sorted(set(_detail_cols()) - read)}"
    )


def test_the_seventeen_are_gone() -> None:
    cols = set(_columns(_sql(W6C), "listings_public"))
    assert not cols & DROPPED, f"517 still projects {sorted(cols & DROPPED)}"
    # And the file says so itself, so a half-applied run cannot pass silently.
    sql = _sql(W6C)
    assert "expected 44 (was 61)" in sql, "517 lost its own width assertion"


def test_portal_listing_counts_is_not_touched() -> None:
    """8/8 of its columns are read by portal_health_mv, so the census leaves it
    alone — narrowing it would be a rebuild that removes nothing."""
    sql = _sql(W6C)
    assert not re.search(
        r"create\s+(?:or\s+replace\s+)?view\s+(?:public\.)?portal_listing_counts\b", sql,
        re.IGNORECASE,
    ), "517 redefines portal_listing_counts — all eight columns have a reader"
    assert "expected 8" in sql, "517 does not assert portal_listing_counts keeps its width"


def test_every_dependent_matview_is_rebuilt_and_swapped() -> None:
    """Both loops must name all five. A matview missing from the build array is
    left bound to the legacy view and section 5's DROP fails; one missing from
    the swap array is left as a `_next` duplicate."""
    sql = _sql(W6C)
    for mv in MATVIEWS:
        assert sql.count(f"'{mv}'") >= 3, (
            f"{mv} is not named in all three of 517's arrays (build, swap, assert)"
        )


def test_no_next_object_is_declared_or_left_behind() -> None:
    """`parse_objects` is what apply_migration.yml's receipt probes with. Every
    `_next` relation this file builds is created through dynamic SQL precisely so
    the receipt never looks for a name the same file renames away."""
    declared = parse_objects((ROOT / "migrations" / W6C).read_text(encoding="utf-8"))
    idents = {o.ident for o in declared}
    assert not any(i.endswith("_next") for i in idents), (
        f"517 declares a transient object the receipt will probe for: {sorted(idents)}"
    )
    assert "listings_public" in idents, (
        "517 declares no listings_public — the receipt would confirm nothing"
    )
    sql = _sql(W6C)
    assert "_next objects left behind" in sql, "517 dropped its leftover-scaffolding guard"
    assert "listings_public_legacy survived" in sql, "517 dropped its legacy-view guard"


def test_the_swap_is_idempotent_by_construction() -> None:
    """apply_migration.yml re-runs the whole file on a lock timeout, and the
    refresh cron (*/10) can cause one. The drop is gated on `_next` still
    existing, which is what stops a second pass from dropping a matview it has
    already replaced and then failing on the rename."""
    sql = _sql(W6C)
    assert "continue when to_regclass('public.' || nxt) is null;" in sql, (
        "517's swap is no longer gated on `_next` existing — a re-run would drop "
        "a matview it already swapped"
    )
    assert "drop view if exists listings_public_legacy;" in sql
