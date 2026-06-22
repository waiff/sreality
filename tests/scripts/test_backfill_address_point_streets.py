"""Hermetic tests for the pure match-partitioning of the RÚIAN street resolver."""

from __future__ import annotations

from scripts.backfill_address_point_streets import partition_matches


def test_unique_street_is_a_matched_update_with_house():
    updates, other, counts = partition_matches([(101, ["Husova"], "12")])
    assert updates == [(101, "Husova", "12")]
    assert other == []
    assert counts == {"matched": 1, "ambiguous": 0, "nomatch": 0}


def test_no_point_in_range_is_nomatch_stamp_only():
    updates, other, counts = partition_matches([(102, None, None)])
    assert updates == []
    assert other == [102]
    assert counts == {"matched": 0, "ambiguous": 0, "nomatch": 1}


def test_two_distinct_streets_is_ambiguous_stamp_only():
    updates, other, counts = partition_matches([(103, ["Husova", "Nádražní"], "7")])
    assert updates == []
    assert other == [103]
    assert counts == {"matched": 0, "ambiguous": 1, "nomatch": 0}


def test_matched_row_may_carry_a_null_house_number():
    updates, _, counts = partition_matches([(104, ["Polní"], None)])
    assert updates == [(104, "Polní", None)]
    assert counts["matched"] == 1


def test_mixed_batch_splits_and_preserves_order():
    updates, other, counts = partition_matches([
        (1, ["A"], "1"),          # matched
        (2, None, None),          # nomatch
        (3, ["B", "C"], "3"),     # ambiguous
        (4, ["D"], "4"),          # matched
    ])
    assert updates == [(1, "A", "1"), (4, "D", "4")]
    assert other == [2, 3]
    assert counts == {"matched": 2, "ambiguous": 1, "nomatch": 1}
