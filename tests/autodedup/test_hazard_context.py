"""W8 hazard context: the live window is read off SIGHTINGS, and a price list is a stack."""

from __future__ import annotations

from autodedup.dataset import Listing, Location
from autodedup.hazard_context import (
    BlockCell,
    block_cells,
    block_members,
    category_group,
    confusable_twins,
    disjoint_windows,
    is_safe_context,
    live_overlap_days,
    live_window,
    pair_cell,
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


# --- the safe context -------------------------------------------------------------------


def test_safe_context_needs_all_three_clauses() -> None:
    a = make(1, last_seen="2026-06-01T00:00:00+00:00")
    b = make(2, first="2026-07-01T00:00:00+00:00", last_seen="2026-08-01T00:00:00+00:00")
    cells = block_cells([a, b])
    assert is_safe_context(a, b, cells, catalog_ratio_max=0.0)
    # a catalogue-only gallery is never safe, whatever the windows say
    assert not is_safe_context(a, b, cells, catalog_ratio_max=0.9)
    # co-live is never safe
    c = make(3, first="2026-05-15T00:00:00+00:00", last_seen="2026-07-01T00:00:00+00:00")
    assert not is_safe_context(a, c, block_cells([a, c]), catalog_ratio_max=0.0)
    # a one-shape stack is never safe
    stack = [make(i, area=50.0, last_seen="2026-06-01T00:00:00+00:00") for i in range(10, 16)]
    stack_cells = block_cells(stack)
    late = make(20, first="2026-07-01T00:00:00+00:00", area=50.0)
    assert not is_safe_context(stack[0], late, block_cells([*stack, late]),
                               catalog_ratio_max=0.0)
    assert isinstance(stack_cells[("ruian:1001", "byt|prodej")], BlockCell)
