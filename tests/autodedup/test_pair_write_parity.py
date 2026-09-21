"""W9m — both lanes write the same pair, and the store is the definition (E117, D40).

Migration 539 gave `autodedup.pairs` a `certificate` COLUMN, and said in its own comment why:
`E63 can re-promote a certified pair under a context_rule reason, so the decision string is
lossy, and E33 orders a component's edges certificate-first` — `a lane that clustered from
stored rows without it would union in a different order than the cohort pass`. The real-time
lane wrote it. The batch score lane never did, so the batch generation held 0 certificates
against a decision string that named one on 3,866 rows, and `rt_equivalence` read 'K-C'
against NULL on 3,462 shared pairs and called it 3,462 moved DECISIONS (M171).

Two repairs, and the census rail that keeps them: the score lane writes every column the
clustering reads, and a refused bridge names the invariant that refused it on both lanes.
"""

from __future__ import annotations

import re

from autodedup.cluster import cluster_pairs
from autodedup.incremental_sql import RT_PAIR_UPSERT_SQL
from autodedup.score_lane import conflict_params, pair_params
from autodedup.score_sql import PAIR_UPSERT_SQL
from tests.autodedup.test_store_precision import SETTINGS, _edges, _world

# What `cluster.edge_rank` and `cluster_pairs` read off a stored row. Every lane that writes a
# pair writes all of these, or a generation is not re-clusterable from its own store (D40).
CLUSTERING_COLUMNS: frozenset[str] = frozenset(
    {"score", "zone", "certificate", "guard_veto", "families"}
)


def _insert_columns(sql: str) -> set[str]:
    body = sql.split("insert into autodedup.pairs (", 1)[1].split(") values", 1)[0]
    return {column.strip() for column in body.replace("\n", " ").split(",") if column.strip()}


def _updated_columns(sql: str) -> set[str]:
    body = sql.split("do update set", 1)[1]
    return set(re.findall(r"^\s*([a-z_]+)\s*=", body, re.MULTILINE))


# ------------------------------------------------------ the defect, reproduced


def test_the_score_lane_writes_the_certificate_the_clustering_orders_on() -> None:
    """The reproduction: a certified pair scored by the batch lane used to store NULL."""
    row = {"lo": 7, "hi": 9, "score": 1.0, "zone": "merge", "certificate": "K-C",
           "reason": "certificate:K-C", "families": ["LOC", "IMG"], "probes": ["K1"]}

    params = pair_params(row, {}, "w6_gold", "g")

    assert params["certificate"] == "K-C"
    assert "certificate" in _insert_columns(PAIR_UPSERT_SQL)
    assert "certificate" in _updated_columns(PAIR_UPSERT_SQL)


def test_an_uncertified_pair_stores_null_rather_than_an_empty_string() -> None:
    row = {"lo": 7, "hi": 9, "score": 0.4, "zone": "band", "certificate": None,
           "reason": "model", "families": [], "probes": ["K1"]}

    assert pair_params(row, {}, "w6_gold", "g")["certificate"] is None


def test_both_lanes_write_every_column_the_clustering_reads() -> None:
    """The standing rail. A column one lane writes and the other does not is the shape of
    M171, and it is invisible until an instrument reads the two stores against each other."""
    for name, sql in (("score lane", PAIR_UPSERT_SQL), ("real-time lane", RT_PAIR_UPSERT_SQL)):
        missing = CLUSTERING_COLUMNS - _insert_columns(sql)
        assert not missing, f"{name} does not write {sorted(missing)}"
        stale = CLUSTERING_COLUMNS - _updated_columns(sql)
        assert not stale, f"{name} does not refresh {sorted(stale)} on conflict"


def test_neither_lane_narrows_the_score_on_the_way_in() -> None:
    """The cast is half of migration 541: a `::real` parameter narrows the value BEFORE the
    column ever sees it, so widening the column alone would have repaired nothing."""
    for sql in (PAIR_UPSERT_SQL, RT_PAIR_UPSERT_SQL):
        assert "%(score)s::double precision" in sql
        assert "%(score)s::real" not in sql


# --------------------------------------------------- the second asymmetry (E118)


def test_a_refused_bridge_names_the_invariant_that_refused_it() -> None:
    """`apply_bridges` records WHY it refused each bridge and the real-time lane stores it;
    the score lane wrote NULL on all 1,976 of them, so the two refusal censuses could not be
    read against each other (M177 had to compare 14 with 289 with 677 by hand)."""
    listings, fps = _world()
    clustered = cluster_pairs(_edges("double precision"), listings, fps, SETTINGS)
    bridges = [{"lo": 1, "hi": 2, "score": 0.9, "certificate": None, "families": [],
                "left_cluster": 1, "right_cluster": 2, "left_members": [1, 5],
                "right_members": [2, 6], "applied": False, "invariant": "floor_spread"}]

    rows = conflict_params({"conflicts": clustered.conflicts, "bridges": bridges}, {}, "g")
    bridge_rows = [row for row in rows if row["kind"] == "bridge"]

    assert len(bridge_rows) == 1
    assert bridge_rows[0]["invariant"] == "floor_spread"


def test_an_applied_bridge_is_still_not_a_conflict() -> None:
    bridges = [{"lo": 1, "hi": 2, "score": 0.9, "certificate": None, "families": [],
                "left_cluster": 1, "right_cluster": 2, "left_members": [1],
                "right_members": [2], "applied": True, "invariant": None}]

    assert conflict_params({"conflicts": [], "bridges": bridges}, {}, "g") == []
