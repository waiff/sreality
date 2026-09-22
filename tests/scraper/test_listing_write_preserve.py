"""R4: the wipe closes inside the ONE shared SET builder, driven by the contract.

Every detail re-fetch rewrites every `LISTING_COLUMNS` member from the parser's output.
For a cell the parse HAS an opinion about (`structured`, `derived`) that is correct — a
portal that stops stating a fact must be able to clear it. For a cell the parse is silent
about (`text`, `none`) it is destruction: it erased 2,949 of 24,621 `condition` fills,
100% of them on rows refetched after the fill.

The rule is per (source, column) and comes from `scraper.attribute_contract`, never a
second hand-kept list, so declaring a producer is the only way to change what preserves.
These tests render the SQL — the one artifact that decides it — for all nine portals and
for BOTH write paths.
"""

from __future__ import annotations

import pytest

from scraper import db
from scraper.attribute_contract import CONTRACT

PORTALS = sorted(CONTRACT)


def _clause(sql: str, column: str) -> str:
    for line in sql.split(",\n"):
        stripped = line.strip()
        if stripped.startswith(f"{column} = "):
            return stripped
    raise AssertionError(f"{column} is not in the SET clause at all")


@pytest.mark.parametrize("portal", PORTALS)
def test_the_contract_decides_which_cells_a_parser_null_may_clear(portal: str) -> None:
    sql = db._listing_update_set_sql(portal)
    for column, declared in sorted(CONTRACT[portal].items()):
        if column == "area_basis":
            continue  # follows area_m2, not its own producer — the next test
        clause = _clause(sql, column)
        if declared.producer in ("text", "none"):
            assert clause == (
                f"{column} = COALESCE(EXCLUDED.{column}, listings.{column})"
            ), f"{portal}/{column} is producer={declared.producer}; a parse NULL is silence"
        else:
            assert clause == f"{column} = EXCLUDED.{column}", (
                f"{portal}/{column} is producer={declared.producer}; the parse IS the "
                f"verdict, so a NULL must still clear"
            )


@pytest.mark.parametrize("portal", PORTALS)
def test_the_three_non_contract_columns_keep_their_own_rule(portal: str) -> None:
    """`description` is the text lane's substrate and clears with the parse;
    `published_at` / `source_url` are the pre-R4 identity preserves."""
    sql = db._listing_update_set_sql(portal)
    assert _clause(sql, "description") == "description = EXCLUDED.description"
    for column in ("published_at", "source_url"):
        assert _clause(sql, column) == (
            f"{column} = COALESCE(EXCLUDED.{column}, listings.{column})"
        )


def test_the_rule_really_differs_by_source() -> None:
    """One rule, nine renderings — not a global widening of the frozenset.

    bazos states none of these facts (the post-publication lane is their only producer),
    sreality states all of them in its payload."""
    bazos = db._listing_update_set_sql("bazos")
    sreality = db._listing_update_set_sql("sreality")
    for column in ("condition", "has_lift", "building_type", "energy_rating"):
        assert _clause(bazos, column).startswith(f"{column} = COALESCE")
        assert _clause(sreality, column) == f"{column} = EXCLUDED.{column}"
    assert db._preserved_columns("sreality") == db._PRESERVE_IF_NULL_COLUMNS


@pytest.mark.parametrize("portal", PORTALS)
def test_the_area_pair_never_decouples(portal: str) -> None:
    """`area_basis` is `derived` on every portal, so on bazos — the one portal whose
    `area_m2` is `text` — the two producers disagree. The rendering must not:
    `derive_headline_area` returns (None, None) together, and a preserved parcel area
    whose basis was blanked reads as a usable area to every consumer."""
    sql = db._listing_update_set_sql(portal)
    number = _clause(sql, "area_m2").removeprefix("area_m2 = ")
    basis = _clause(sql, "area_basis").removeprefix("area_basis = ")
    assert number == basis.replace("area_basis", "area_m2")


@pytest.mark.parametrize("portal", PORTALS)
def test_both_write_paths_render_the_same_rule(portal: str) -> None:
    """The per-item upsert (all nine portals) and the batched drain upsert (sreality)
    are two statements; the SET clause they carry is ONE string from one builder."""
    fragment = db._listing_update_set_sql(portal)
    assert fragment in db._upsert_listing_sql(portal)
    if portal == "sreality":
        assert fragment == db._BATCH_UPDATE_SET
        assert fragment in db._BATCH_UPSERT_SQL


def test_every_declared_producer_is_one_the_rule_knows() -> None:
    """A fifth producer must not silently fall through to 'clears'."""
    producers = {c.producer for cells in CONTRACT.values() for c in cells.values()}
    assert producers <= {"structured", "derived", "text", "none"}
    assert db._PARSE_SILENT_PRODUCERS < producers


def test_a_source_outside_the_contract_keeps_the_pre_r4_rule() -> None:
    assert db._preserved_columns("a-tenth-portal") == db._PRESERVE_IF_NULL_COLUMNS
