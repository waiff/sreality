"""The candidate-group packing (PROGRAM.md §12, E56) — pure, so these are value tests.

What is pinned here is the contract the queue and the write route both depend on:

  * EXACTLY ONCE — every residual pair of the input is inside one candidate group and no
    other. A pair in none is a question the queue silently stopped asking; a pair in two is
    two rulings about one fact, and the second one overwrites the first.
  * THE CAP — no card holds more than 8 adverts, unless the LOCKS alone already do: a unit is
    never split here, so the cap yields to it rather than deferring the question for ever.
  * LOCKED UNITS — a cluster of the generation travels whole, all of its members, even the
    ones no residual pair names.
  * DETERMINISM — the same inputs give the same groups, the same keys and the same order,
    because a review queue that reshuffles under a correcting hand causes the mis-clicks it
    exists to catch.
"""

from __future__ import annotations

import pytest

from autodedup import candidates as c


def pair(lo: int, hi: int, score: float, zone: str = "band", families: int = 1,
         block: int | None = 554782) -> c.ResidualPair:
    return c.ResidualPair(
        listing_lo=lo,
        listing_hi=hi,
        score=score,
        zone=zone,
        families=families,
        block_key=block,
        block_grain="o" if block is not None else None,
    )


def all_pairs(groups: tuple[c.CandidateGroup, ...]) -> list[tuple[int, int]]:
    return [(p.listing_lo, p.listing_hi) for g in groups for p in g.pairs]


# ------------------------------------------------------------------------ the shape of a group


def test_two_lone_adverts_are_one_card():
    groups = c.build_candidates([pair(11, 22, 0.61)], [])
    assert len(groups) == 1
    group = groups[0]
    assert group.size == 2
    assert group.n_units == 2
    assert [u.cluster_key for u in group.units] == [None, None]
    assert group.listing_ids == (11, 22)
    assert group.score_min == pytest.approx(0.61)
    assert group.score_max == pytest.approx(0.61)


def test_a_merged_group_is_ONE_locked_unit_and_travels_whole():
    """The lock is the point: five adverts the engine merged are one question, and the card
    shows all five — including the two no residual pair names."""
    pairs = [pair(11, 101, 0.55), pair(11, 102, 0.51)]
    locks = [(7001, 101), (7001, 102), (7001, 103)]
    groups = c.build_candidates(pairs, locks)
    assert len(groups) == 1
    group = groups[0]
    assert group.n_units == 2
    assert group.size == 4
    locked = [u for u in group.units if u.cluster_key is not None]
    assert len(locked) == 1
    # 103 is in no pair at all and is still on the card.
    assert locked[0].listing_ids == (101, 102, 103)
    # The two pairs against that group are ONE unit edge, not two questions.
    assert len(group.edges) == 1
    assert group.edges[0].score == pytest.approx(0.55)
    assert len(group.edges[0].pairs) == 2
    assert len(group.pairs) == 2


def test_the_fan_out_of_one_advert_against_a_group_collapses_to_one_card():
    """The measured shape: advert X vs each member of group G is the same question five times."""
    locks = [(900, m) for m in (201, 202, 203, 204, 205)]
    pairs = [pair(50, m, 0.4 + i / 100) for i, m in enumerate((201, 202, 203, 204, 205))]
    groups = c.build_candidates(pairs, locks)
    assert len(groups) == 1
    assert groups[0].size == 6
    assert len(groups[0].edges) == 1
    assert len(groups[0].pairs) == 5


# ------------------------------------------------------------------------------ exactly once


def test_every_residual_pair_lands_in_exactly_one_group():
    pairs = [
        pair(1, 2, 0.9), pair(2, 3, 0.8), pair(3, 4, 0.7), pair(1, 4, 0.6),
        pair(10, 11, 0.5), pair(11, 12, 0.45), pair(12, 13, 0.44), pair(13, 14, 0.43),
        pair(14, 15, 0.42), pair(15, 16, 0.41), pair(16, 17, 0.4), pair(17, 18, 0.39),
        pair(18, 19, 0.38), pair(19, 20, 0.37), pair(10, 20, 0.36), pair(11, 19, 0.35),
        pair(2, 11, 0.34),
    ]
    groups = c.build_candidates(pairs, [])
    landed = all_pairs(groups)
    assert sorted(landed) == sorted((p.listing_lo, p.listing_hi) for p in pairs)
    assert len(landed) == len(set(landed)), "a pair reached two cards"


def test_a_dense_clique_still_places_every_pair_once():
    """The hard case for the cap: twelve adverts all joined to each other is 66 pairs and no
    packing of 8 can hold them, so the rounds have to keep going until every pair is placed."""
    ids = list(range(100, 112))
    pairs = [
        pair(a, b, 0.9 - (i / 1000))
        for i, (a, b) in enumerate(
            (a, b) for x, a in enumerate(ids) for b in ids[x + 1:]
        )
    ]
    groups = c.build_candidates(pairs, [])
    landed = all_pairs(groups)
    assert len(landed) == len(pairs)
    assert len(set(landed)) == len(pairs)
    assert all(g.size <= c.MAX_ADVERTS for g in groups)


def test_a_pair_inside_one_locked_unit_is_not_dropped():
    """It cannot happen in the cohort (that is what makes a pair residual) and it still has to
    land somewhere — the guarantee is about the input, not about the query that produced it."""
    locks = [(5, 1), (5, 2)]
    groups = c.build_candidates([pair(1, 2, 0.8), pair(1, 9, 0.7)], locks)
    landed = all_pairs(groups)
    assert sorted(landed) == [(1, 2), (1, 9)]
    assert len(landed) == len(set(landed))


def test_a_pair_inside_a_lock_with_no_other_edge_gets_its_own_card():
    locks = [(5, 1), (5, 2)]
    groups = c.build_candidates([pair(1, 2, 0.8)], locks)
    assert len(groups) == 1
    assert groups[0].n_units == 1
    assert groups[0].pairs[0].listing_lo == 1


# ------------------------------------------------------------------------------------ the cap


def test_no_card_holds_more_than_the_cap():
    chain = [pair(n, n + 1, 0.9 - n / 100) for n in range(1, 20)]
    groups = c.build_candidates(chain, [])
    assert groups
    assert all(g.size <= c.MAX_ADVERTS for g in groups)


def test_the_edge_that_would_burst_the_cap_gets_a_smaller_card_of_its_own():
    """Nothing is dropped: the deferred edge is packed in a later round, where the units start
    alone again."""
    # Three locked groups of four adverts each: any two fill the cap exactly, all three burst it.
    locks = [(1, i) for i in (11, 12, 13, 14)]
    locks += [(2, i) for i in (21, 22, 23, 24)]
    locks += [(3, i) for i in (31, 32, 33, 34)]
    pairs = [pair(11, 21, 0.9), pair(11, 31, 0.8), pair(21, 31, 0.7)]
    groups = c.build_candidates(pairs, locks)
    assert len(groups) == 3
    assert all(g.size == 8 for g in groups)
    assert sorted(all_pairs(groups)) == [(11, 21), (11, 31), (21, 31)]


def test_two_locked_units_that_alone_burst_the_cap_are_still_asked():
    """A unit is never split, so the cap yields to the lock rather than deferring for ever."""
    locks = [(1, i) for i in range(11, 17)]   # six adverts
    locks += [(2, i) for i in range(21, 27)]  # six more
    groups = c.build_candidates([pair(11, 21, 0.9)], locks)
    assert len(groups) == 1
    assert groups[0].size == 12 > c.MAX_ADVERTS
    assert groups[0].n_units == 2


# ------------------------------------------------------------------------------ determinism


def test_the_packing_is_deterministic_under_a_shuffled_input():
    pairs = [
        pair(1, 2, 0.9), pair(2, 3, 0.8), pair(4, 5, 0.75), pair(5, 6, 0.7),
        pair(3, 4, 0.65), pair(6, 7, 0.6), pair(7, 8, 0.55), pair(1, 8, 0.5),
    ]
    first = c.build_candidates(pairs, [])
    for rotation in range(1, len(pairs)):
        again = c.build_candidates(pairs[rotation:] + pairs[:rotation], [])
        assert [g.candidate_key for g in again] == [g.candidate_key for g in first]
        assert [g.listing_ids for g in again] == [g.listing_ids for g in first]


def test_the_key_is_the_smallest_id_and_a_digest_of_the_membership():
    groups = c.build_candidates([pair(77, 12, 0.5)], [])
    key = groups[0].candidate_key
    assert key.startswith("12-")
    assert key == c.candidate_key([77, 12]) == c.candidate_key([12, 77])
    # The digest MOVES with the membership: a stale link opens nothing rather than a group
    # that has since been repacked under the same name.
    assert c.candidate_key([12, 77]) != c.candidate_key([12, 77, 78])


def test_keys_are_unique_across_the_whole_index():
    ids = list(range(200, 214))
    pairs = [
        pair(a, b, 0.9 - i / 1000)
        for i, (a, b) in enumerate((a, b) for x, a in enumerate(ids) for b in ids[x + 1:])
    ]
    groups = c.build_candidates(pairs, [])
    keys = [g.candidate_key for g in groups]
    assert len(keys) == len(set(keys))


def test_lo_and_hi_are_normalised_and_the_best_score_wins():
    groups = c.build_candidates([pair(9, 3, 0.4), pair(3, 9, 0.8)], [])
    assert len(groups) == 1
    assert groups[0].pairs[0].listing_lo == 3
    assert groups[0].score_max == pytest.approx(0.8)
    assert len(groups[0].pairs) == 1


# ---------------------------------------------------------------------------- header facts


def test_the_header_facts_are_read_off_the_underlying_pairs():
    groups = c.build_candidates(
        [
            pair(1, 2, 0.7, zone="band", families=1, block=500),
            pair(2, 3, 0.4, zone="reject", families=32, block=500),
        ],
        [],
    )
    group = groups[0]
    assert group.zones() == {"band": 1, "reject": 1}
    assert group.families() == 33
    assert group.blocks() == ((500, "o"),)
    assert group.score_min == pytest.approx(0.4)
    assert group.score_max == pytest.approx(0.7)


def test_an_empty_cohort_is_no_groups_not_an_error():
    assert c.build_candidates([], [(1, 10), (1, 11)]) == ()


# ---------------------------------------------------------------------------- the memo


def test_the_cache_serves_one_fingerprint_and_rebuilds_on_the_next():
    c.clear_cache()
    calls: list[int] = []

    def build() -> tuple[c.CandidateGroup, ...]:
        calls.append(1)
        return c.build_candidates([pair(1, 2, 0.5)], [])

    first = c.cached_index("g4", (10, "2026-09-18T00:00:00", 3), build)
    again = c.cached_index("g4", (10, "2026-09-18T00:00:00", 3), build)
    assert again is first
    assert len(calls) == 1
    # A NEW SCORE RUN moves the fingerprint, and the cache must not serve the old packing.
    moved = c.cached_index("g4", (11, "2026-09-18T01:00:00", 3), build)
    assert moved is not first
    assert len(calls) == 2
    # So does a re-clustering, which only moves the third number.
    c.cached_index("g4", (11, "2026-09-18T01:00:00", 4), build)
    assert len(calls) == 3
    c.clear_cache()


def test_the_cache_is_bounded():
    c.clear_cache()
    for n in range(c._CACHE_MAX + 3):
        c.cached_index(f"g{n}", (n,), lambda: ())
    assert len(c._CACHE) <= c._CACHE_MAX
    c.clear_cache()
