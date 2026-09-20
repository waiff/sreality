"""W8 hazard context: the live window is read off SIGHTINGS, and a price list is a stack."""

from __future__ import annotations

from pathlib import Path

from autodedup.dataset import Image, Listing, Location
from autodedup.hazard_context import (
    BlockCell,
    block_cells,
    block_members,
    category_group,
    confusable_twins,
    ContextIndex,
    ContextStamp,
    disjoint_windows,
    fungible_catalogue,
    live_overlap_days,
    live_window,
    pair_cell,
    rail_plan,
    rail_reopen,
)


def make(
    listing_id: int,
    *,
    area: float | None = 50.0,
    disposition: str | None = "2+kk",
    first: str = "2026-05-01T00:00:00+00:00",
    last_seen: str | None = "2026-06-01T00:00:00+00:00",
    inactive: str | None = None,
    active: bool = False,
    ruian: int | None = 1001,
    broker: str | None = "b1",
    native: str | None = None,
    category_main: str = "byt",
    category_type: str = "prodej",
) -> Listing:
    return Listing(
        id=listing_id,
        block="jablonec",
        source="sreality",
        source_id_native=native if native is not None else str(listing_id),
        category_main=category_main,
        category_type=category_type,
        disposition=disposition,
        area_m2=area,
        price=5_000_000.0,
        first_seen_at=first,
        last_seen_at=last_seen,
        inactive_at=inactive,
        is_active=active,
        broker_key=broker,
        location=Location(obec_kod=563510, ruian_adm_kod=ruian),
    )


# --- the live window --------------------------------------------------------------------


def test_delisted_advert_ends_at_the_last_sighting_not_the_detection_stamp() -> None:
    # The pair that motivated the module: A stops being seen on 1 June and the sweep only
    # notices on 10 July; B appears on 15 June. Reading `inactive_at` as the end invents
    # 25 days of co-liveness that never happened.
    a = make(1, last_seen="2026-06-01T00:00:00+00:00", inactive="2026-07-10T00:00:00+00:00")
    b = make(2, first="2026-06-15T00:00:00+00:00", last_seen="2026-08-01T00:00:00+00:00")
    assert live_overlap_days(a, b) == 0.0
    assert disjoint_windows(a, b)


def test_genuine_co_liveness_is_still_counted() -> None:
    a = make(1, first="2026-05-01T00:00:00+00:00", last_seen="2026-06-30T00:00:00+00:00")
    b = make(2, first="2026-06-01T00:00:00+00:00", last_seen="2026-08-01T00:00:00+00:00")
    assert live_overlap_days(a, b) == 29.0
    assert not disjoint_windows(a, b)


def test_an_active_advert_has_no_end_yet() -> None:
    live = make(1, active=True, last_seen="2026-06-01T00:00:00+00:00")
    _, end = live_window(live)
    assert end is not None and end.year == 2999
    later = make(2, first="2026-08-01T00:00:00+00:00", active=True)
    assert live_overlap_days(live, later) > 1000


def test_a_missing_stamp_never_claims_overlap() -> None:
    a = make(1, first="", last_seen=None)
    assert live_overlap_days(a, make(2)) == 0.0


# --- the block cell ---------------------------------------------------------------------


def test_one_shape_repeated_is_a_stack_and_a_mixed_building_is_not() -> None:
    stack = [make(i, area=52.0 + 0.1 * i) for i in range(6)]
    cells = block_cells(stack)
    cell = cells[("ruian:1001", "byt|prodej")]
    assert cell.n_listings == 6
    assert cell.n_unit_shapes == 1
    assert cell.is_shape_stack

    mixed = [make(i, area=40.0 + 15.0 * i, disposition=f"{i}+kk") for i in range(6)]
    mixed_cell = block_cells(mixed)[("ruian:1001", "byt|prodej")]
    assert mixed_cell.n_unit_shapes == 6
    assert not mixed_cell.is_shape_stack


def test_a_small_block_is_never_a_stack() -> None:
    small = [make(i, area=52.0) for i in range(3)]
    assert not block_cells(small)[("ruian:1001", "byt|prodej")].is_shape_stack


def test_the_cell_census_counts_brokers_and_native_ids() -> None:
    rows = [make(1, broker="b1", native="x"), make(2, broker="b2", native="y"),
            make(3, broker=None, native="y")]
    cell = block_cells(rows)[("ruian:1001", "byt|prodej")]
    assert (cell.n_brokers, cell.n_source_native_ids) == (2, 2)


def test_a_pair_takes_the_wider_of_the_two_cells() -> None:
    dense = [make(i, ruian=1001, area=52.0) for i in range(6)]
    lone = make(99, ruian=2002)
    cells = block_cells([*dense, lone])
    cell = pair_cell(dense[0], lone, cells)
    assert cell is not None and cell.n_listings == 6


def test_a_null_category_side_reads_as_its_own_cell_and_the_wider_one_wins() -> None:
    known = [make(i, category_type="prodej", area=52.0) for i in range(5)]
    unknown = make(50, category_type=None)
    cells = block_cells([*known, unknown])
    assert category_group(unknown) == "byt|?"
    cell = pair_cell(known[0], unknown, cells)
    assert cell is not None and cell.n_listings == 5


# --- twins ------------------------------------------------------------------------------


def test_twins_are_the_units_at_this_address_a_merge_could_fuse_it_with() -> None:
    rows = [make(1, area=50.0), make(2, area=50.5), make(3, area=80.0),
            make(4, area=50.0, disposition="3+kk"),
            make(5, area=50.0, category_type="pronajem")]
    members = block_members(rows)["ruian:1001"]
    assert confusable_twins(rows[0], members) == [2]


# --- the refusing direction: the fungible-catalogue veto ---------------------------------


def _index(rows: list[Listing], images: dict[int, list[Image]] | None = None) -> ContextIndex:
    return ContextIndex.build({row.id: row for row in rows}, images or {})


def test_a_stated_from_price_refuses_the_promotion() -> None:
    a = make(1)
    a.description = "Ceny od 5 499 000 Kč, k nastěhování ihned."
    b = make(2)
    index = _index([a, b])
    assert a.id in index.from_price and b.id not in index.from_price
    stamp = index.stamp(a, b)
    assert fungible_catalogue(stamp, True, block_min=None, image_population_min=10,
                              from_price_veto=True) == "from_price"
    assert fungible_catalogue(stamp, True, block_min=None, image_population_min=10,
                              from_price_veto=False) is None


def test_a_shared_STOCK_image_refuses_and_a_shared_private_one_does_not() -> None:
    a, b = make(1), make(2)
    images = {
        1: [Image(listing_id=1, image_id=11, phash=7, pop=34)],
        2: [Image(listing_id=2, image_id=21, phash=7, pop=34)],
    }
    index = _index([a, b], images)
    assert index.shared_image_population(1, 2) == 34
    assert fungible_catalogue(index.stamp(a, b), False, block_min=None,
                              image_population_min=10, from_price_veto=True) == "stock_images"

    private = {
        1: [Image(listing_id=1, image_id=11, phash=9, pop=2)],
        2: [Image(listing_id=2, image_id=21, phash=9, pop=2)],
    }
    lone = _index([a, b], private)
    assert fungible_catalogue(lone.stamp(a, b), False, block_min=None,
                              image_population_min=10, from_price_veto=True) is None


def test_one_templated_photo_beside_private_ones_does_not_make_a_warrant_stock() -> None:
    # The limb asks whether the whole agreement is stock, not whether any of it is: reading the
    # MOST-carried shared image instead refuses 101 g5 promotions carrying 35 labelled duplicates.
    a, b = make(1), make(2)
    images = {
        1: [Image(listing_id=1, image_id=11, phash=7, pop=40),
            Image(listing_id=1, image_id=12, phash=8, pop=1)],
        2: [Image(listing_id=2, image_id=21, phash=7, pop=40),
            Image(listing_id=2, image_id=22, phash=8, pop=1)],
    }
    index = _index([a, b], images)
    assert index.shared_image_population(1, 2) == 1
    assert fungible_catalogue(index.stamp(a, b), False, block_min=None,
                              image_population_min=10, from_price_veto=True) is None


def test_images_that_are_not_SHARED_never_refuse() -> None:
    a, b = make(1), make(2)
    images = {
        1: [Image(listing_id=1, image_id=11, phash=7, pop=40)],
        2: [Image(listing_id=2, image_id=21, phash=8, pop=40)],
    }
    index = _index([a, b], images)
    assert index.shared_image_population(1, 2) == 0
    assert fungible_catalogue(index.stamp(a, b), False, block_min=None,
                              image_population_min=10, from_price_veto=True) is None


def test_the_block_limb_is_off_unless_a_settings_row_asks_for_it() -> None:
    rows = [make(i, area=52.0) for i in range(25)]
    index = _index(rows)
    stamp = index.stamp(rows[0], rows[1])
    assert stamp.cell_n_listings == 25
    assert fungible_catalogue(stamp, False, block_min=None, image_population_min=10,
                              from_price_veto=True) is None
    assert fungible_catalogue(stamp, False, block_min=20, image_population_min=10,
                              from_price_veto=True) == "catalogue_block"


# --- the rail ----------------------------------------------------------------------------


def test_the_rail_fires_only_when_the_census_CROSSES_the_bar() -> None:
    under = ContextStamp(cell_n_listings=4, shared_image_pop_min=2)
    assert rail_reopen(under, ContextStamp(25, 2), block_min=20,
                       image_population_min=10) == "catalogue_block"
    assert rail_reopen(under, ContextStamp(4, 14), block_min=20,
                       image_population_min=10) == "stock_images"
    # already over the bar when it merged: that was the veto's business, not the rail's
    assert rail_reopen(ContextStamp(25, 2), ContextStamp(30, 2), block_min=20,
                       image_population_min=10) is None
    # no limb configured, nothing to cross
    assert rail_reopen(under, ContextStamp(99, 99), block_min=None,
                       image_population_min=None) is None


def test_the_rail_caps_a_block_and_names_what_it_deferred() -> None:
    rows = [make(i, area=52.0) for i in range(25)]
    index = _index(rows)
    merges = [(i, i + 1, ContextStamp(4, 0), "ruian:1001") for i in range(0, 8)]
    actions, counters = rail_plan(merges, index, {row.id: row for row in rows},
                                  block_min=20, image_population_min=10, max_per_block=3)
    assert counters == {"reopened": 3, "deferred_by_cap": 5, "blocks_at_cap": 1,
                        "blocks_touched": 1}
    assert {action.limb for action in actions} == {"catalogue_block"}
    assert actions[0].to_json()["stamped"] == [4, 0]


def test_a_certificate_is_exempt_from_the_rail() -> None:
    rows = [make(i, area=52.0) for i in range(25)]
    index = _index(rows)
    merges = [(0, 1, ContextStamp(4, 0), "ruian:1001")]
    _, counters = rail_plan(merges, index, {row.id: row for row in rows}, block_min=20,
                            image_population_min=10, max_per_block=8,
                            exempt=frozenset({(0, 1)}))
    assert counters["reopened"] == 0


def test_the_stack_test_survives_as_a_signal_and_reaches_no_rule() -> None:
    stack = [make(i, area=52.0) for i in range(6)]
    cell = block_cells(stack)[("ruian:1001", "byt|prodej")]
    assert isinstance(cell, BlockCell) and cell.is_shape_stack
    import autodedup.decide as decide
    assert "is_shape_stack" not in Path(decide.__file__).read_text(encoding="utf-8")
