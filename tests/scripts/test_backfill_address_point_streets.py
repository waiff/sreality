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


# --- portal coverage + the geocode-provenance gate (location-resolution wave) ---

def test_sources_cover_all_precise_coord_portals():
    from scripts.backfill_address_point_streets import _SOURCES

    # bazos stays out (town-center link pins); pozemek is excluded by category in
    # the SQL, not by source. The three 2026-07 additions must never regress out.
    assert set(_SOURCES) == {
        "sreality", "idnes", "remax", "bezrealitky", "maxima",
        "mmreality", "ceskereality", "realitymix",
    }
    assert "bazos" not in _SOURCES


def test_candidate_sql_gates_geocoded_coords_to_address_grade():
    from scripts.backfill_address_point_streets import _CANDIDATE_SQL

    # A geocode-provenanced coordinate (scraper.location stamp) is only a
    # trustworthy building coordinate at matched_type='regional.address' — a
    # street/municipality centroid must never resolve a street.
    assert "coords'->>'source' IS DISTINCT FROM 'geocode'" in _CANDIDATE_SQL
    assert "matched_type' = 'regional.address'" in _CANDIDATE_SQL
