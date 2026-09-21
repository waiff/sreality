"""W9m — the store must keep the number the clustering RANKS on (E115, E116).

`cluster.edge_rank` is `(certificate first, -score, lo, hi)`. The invariants it feeds are not
transitively closed — `floor_spread` can permit {a,c} and {b,c} while refusing {a,b} and the
triple — so the EDGE ORDER decides which union survives. The batch lane ranks the float64
`Decision`s in the process that computed them; the real-time lane ranks `Decision`s rebuilt
from stored rows. While `autodedup.pairs.score` was `real`, those were two different numbers
on 97.6% of the live cohort's merge edges, and the two engines partitioned the same component
differently (M175, M176).

These are the reproductions. The first two fail against the pre-541 store and pass against the
shipped one; the last is the standing rail that keeps the twin narrowing the way the column
does, so `replay.py` can never again prove equivalence over a precision production does not
have.
"""

from __future__ import annotations

import re
from itertools import permutations
from pathlib import Path
from typing import Any

from autodedup.cluster import cluster_pairs, edge_rank
from autodedup.dataset import Listing, Location
from autodedup.decide import Decision
from autodedup.fingerprint import Fingerprint, build_fingerprint
from autodedup.incremental import PairRow
from autodedup.incremental_store import MemoryStore
from autodedup.settings import Settings
from autodedup.store_score import (
    PAIR_SCORE_SQL_TYPE,
    is_lossless,
    narrow,
    store_eps,
)

SETTINGS = Settings()

# The three scores of the live minimal case 8876 / 144514 / 162485, read off
# `autodedup.cluster_conflicts` on 2026-09-21. All three are stored as exactly 1.0 by a `real`
# column, and they are three different float64 numbers.
TOP = 1.0
MIDDLE = 0.999999997735753
BOTTOM = 0.9999999942645902


def _listing(listing_id: int, floor: int | None) -> Listing:
    return Listing(
        id=listing_id, block="b", source="sreality", category_main="byt",
        category_type="prodej", disposition="3+kk", area_m2=78.0, floor=floor,
        total_floors=6, price=6_000_000.0, location=Location(obec_kod=1),
    )


def _world() -> tuple[dict[int, Listing], dict[int, Fingerprint]]:
    """Three flats on floors 4, 3 and unknown — `floor_spread`'s non-transitive shape."""
    rows = {1: _listing(1, 4), 2: _listing(2, 3), 3: _listing(3, None)}
    return rows, {i: build_fingerprint(row, [], SETTINGS) for i, row in rows.items()}


def _edges(sql_type: str) -> list[Decision]:
    """The same three merge edges, scored as the given column hands them back."""
    return [
        Decision(2, 3, "merge", narrow(TOP, sql_type), {"LOC"}, "K-C", None, "certificate:K-C"),
        Decision(1, 3, "merge", narrow(MIDDLE, sql_type), {"LOC"}, "K-C", None,
                 "certificate:K-C"),
        Decision(1, 2, "merge", narrow(BOTTOM, sql_type), {"LOC"}, "K-C", None,
                 "certificate:K-C"),
    ]


def _members(sql_type: str) -> list[list[int]]:
    listings, fps = _world()
    result = cluster_pairs(_edges(sql_type), listings, fps, SETTINGS)
    return sorted(sorted(members) for members in result.clusters.values())


# ------------------------------------------------------- the defect, reproduced


def test_a_float4_store_collapses_the_edge_order_and_moves_the_cluster() -> None:
    """E114's minimal case. Three merge edges, three distinct float64 scores, one float4 image.

    Under the true scores the highest edge (2,3) unions first and {1,3} is then refused as a
    floor spread; under the stored ones all three tie at 1.0, `edge_rank` falls through to
    `(lo, hi)`, and (1,3) wins the same slot. Same decisions, same invariants, different
    partition — which is the whole of the live/batch cluster gap."""
    assert len({narrow(score, "real") for score in (TOP, MIDDLE, BOTTOM)}) == 1
    assert len({TOP, MIDDLE, BOTTOM}) == 3

    assert _members("double precision") == [[2, 3]]
    assert _members("real") == [[1, 3]]


def test_the_shipped_column_keeps_the_ranking_the_engine_computed() -> None:
    """The repair, stated as the property it buys: the store's own number ranks the same."""
    listings, fps = _world()
    true_order = [edge_rank(edge) for edge in sorted(_edges("double precision"), key=edge_rank)]
    stored_order = [edge_rank(edge) for edge in
                    sorted(_edges(PAIR_SCORE_SQL_TYPE), key=edge_rank)]

    assert true_order == stored_order
    assert _members(PAIR_SCORE_SQL_TYPE) == _members("double precision")
    assert cluster_pairs(_edges(PAIR_SCORE_SQL_TYPE), listings, fps, SETTINGS).clusters == {
        2: [2, 3]
    }


def test_a_component_reclustered_from_stored_rows_reaches_the_batch_partition() -> None:
    """The live path, end to end: decide in memory, WRITE, read back, re-cluster.

    `incremental._recluster` rebuilds a component's `Decision`s from `PairRow`s the store hands
    back, so this is the round trip that has to be lossless. It is only lossless because the
    twin narrows exactly as the column does — flip the type and the same round trip loses the
    order."""
    listings, fps = _world()
    batch = cluster_pairs(_edges("double precision"), listings, fps, SETTINGS)

    store = MemoryStore()
    store.upsert_pairs([_pair_row(edge) for edge in _edges("double precision")])
    replayed = [row.decision() for row in store.pairs_within([1, 2, 3])]

    assert cluster_pairs(replayed, listings, fps, SETTINGS).clusters == batch.clusters


def test_the_recluster_is_a_pure_function_of_the_stored_edge_set() -> None:
    """Why the repair is `by construction` and not a new rule: `incremental._recluster` already
    hands `cluster_pairs` a whole component's edges, and `cluster_pairs` sorts them by
    `edge_rank` before it unions anything. So the partition cannot depend on the order the
    rows come back in, on the order the listings were claimed, or on how many a pass claimed —
    once `edge_rank` is a TOTAL order. float4 was what stopped it being one (E115)."""
    listings, fps = _world()
    edges = _edges(PAIR_SCORE_SQL_TYPE)
    expected = cluster_pairs(edges, listings, fps, SETTINGS)

    for permutation in permutations(edges):
        shuffled = cluster_pairs(list(permutation), listings, fps, SETTINGS)
        assert shuffled.clusters == expected.clusters
        assert shuffled.conflicts == expected.conflicts
        assert shuffled.bridges == expected.bridges


def test_the_twin_narrows_a_score_exactly_as_the_column_does() -> None:
    """E116: the replay proof covers the STORE, not an idealisation of it — which is the whole
    reason the fourth defect of this class went unseen. Under the shipped column the narrowing
    is the identity; under the column as it was, the twin loses the same bits Postgres lost."""
    shipped = MemoryStore()
    shipped.upsert_pairs([_pair_row(Decision(1, 2, "merge", MIDDLE, {"LOC"}, "K-C"))])
    assert shipped.pairs[(1, 2)].score == MIDDLE == narrow(MIDDLE)

    pre_541 = MemoryStore(score_sql_type="real")
    pre_541.upsert_pairs([_pair_row(Decision(1, 2, "merge", MIDDLE, {"LOC"}, "K-C"))])
    assert pre_541.pairs[(1, 2)].score == 1.0 != MIDDLE


def test_the_proof_would_now_catch_the_defect_it_missed() -> None:
    """The regression arm: the SAME component written through a pre-541 store and re-clustered
    reaches a different partition than the batch pass. `replay.py` ran for four waves without
    ever exercising this, because its twin kept the `PairRow` verbatim (E114)."""
    listings, fps = _world()
    batch = cluster_pairs(_edges("double precision"), listings, fps, SETTINGS)

    pre_541 = MemoryStore(score_sql_type="real")
    pre_541.upsert_pairs([_pair_row(edge) for edge in _edges("double precision")])
    replayed = [row.decision() for row in pre_541.pairs_within([1, 2, 3])]

    assert cluster_pairs(replayed, listings, fps, SETTINGS).clusters != batch.clusters


# ------------------------------------------------------------ the standing rails


def test_the_declared_column_type_is_the_one_the_migrations_leave() -> None:
    """One definition, and it lives in SQL. A migration that narrows the column again has to
    move `PAIR_SCORE_SQL_TYPE` with it, or this fails."""
    declared = _score_column_type_from_migrations()
    assert declared == PAIR_SCORE_SQL_TYPE
    assert is_lossless(declared), (
        f"autodedup.pairs.score is {declared!r}; cluster.edge_rank ranks on it, so a lane that "
        "re-reads it cannot reproduce the pass that wrote it")


def test_the_resolution_of_each_type_is_stated_rather_than_assumed() -> None:
    assert store_eps("real") > 1e-7 > store_eps("double precision")
    assert narrow(MIDDLE, "real") == 1.0
    assert narrow(MIDDLE, "double precision") == MIDDLE


def _score_column_type_from_migrations() -> str:
    """The last type `migrations/` declares for `autodedup.pairs.score`."""
    root = Path(__file__).resolve().parents[2] / "migrations"
    create = re.compile(r"^\s*score\s+(real|double precision)\s*,", re.MULTILINE)
    alter = re.compile(
        r"alter\s+table\s+autodedup\.pairs\s+alter\s+column\s+score\s+(?:set\s+data\s+)?type"
        r"\s+(real|double precision)", re.IGNORECASE)
    declared = ""
    for path in sorted(root.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if "autodedup.pairs" not in text and "create table if not exists autodedup.pairs" \
                not in text:
            continue
        for match in alter.finditer(text):
            declared = match.group(1).lower()
        if not declared and "create table if not exists autodedup.pairs" in text:
            body = text.split("create table if not exists autodedup.pairs", 1)[1]
            found = create.search(body.split(");", 1)[0])
            if found:
                declared = found.group(1).lower()
    assert declared, "no migration declares autodedup.pairs.score"
    return declared


def _pair_row(decision: Decision, **over: Any) -> PairRow:
    row = PairRow(
        lo=decision.lo, hi=decision.hi, probes=["K1"], from_lo=True, from_hi=True,
        zone=decision.zone, score=decision.score, families=sorted(decision.families),
        certificate=decision.certificate, veto=decision.veto, reason=decision.reason,
        evidence={}, context={}, fp_lo="a", fp_hi="b",
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row
