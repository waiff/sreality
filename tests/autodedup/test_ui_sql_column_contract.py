"""Every `*_COLUMNS` tuple has exactly as many names as its statement selects.

Offline, no DB. `api/routes/autodedup.py` zips rows onto these tuples rather than reading
`cursor.description` (the fake connections carry none), and **`zip` never raises**: a tuple
with one name too many silently drops it, and one name too few silently mislabels every
column after it. Migration 538 added three names to `CLUSTER_COLUMNS` and a stray copy landed
on `RESIDUAL_COLUMNS`, where the extra names were invisible for exactly that reason — this is
the rail that makes the next one loud.
"""

from __future__ import annotations

import pytest

from autodedup import ui_sql as usql


def _top_level(select_list: str) -> list[str]:
    """Split a SELECT list on its TOP-LEVEL commas: half these lists are LATERAL aggregates
    and `json_build_object(...)` calls whose own commas would otherwise be counted."""
    items: list[str] = []
    depth = 0
    current = ""
    for char in select_list:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        items.append(current.strip())
    return items


def _between(sql: str, start: str, end: str) -> str:
    return sql.split(start, 1)[1].split(end)[0]


_CASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("clusters",
     _between(usql._CLUSTER_SELECT, "SELECT", "FROM autodedup.clusters"),
     usql.CLUSTER_COLUMNS),
    ("residual", usql._RESIDUAL_SELECT.split("SELECT", 1)[1], usql.RESIDUAL_COLUMNS),
    ("group members",
     _between(usql.GROUP_MEMBERS_SQL, "SELECT", "FROM autodedup.cluster_members"),
     usql.MEMBER_COLUMNS),
    ("pairs", usql._PAIR_SELECT_LIST, usql.PAIR_COLUMNS),
    ("verdicts", usql._VERDICT_SELECT_LIST, usql.VERDICT_COLUMNS),
    ("edge summary",
     _between(usql.EDGE_SUMMARY_SQL, "SELECT", "FROM autodedup.cluster_members"),
     usql.EDGE_SUMMARY_COLUMNS),
    ("judgements",
     _between(usql.JUDGEMENTS_LATEST_SQL, ")\n", "FROM autodedup.judgements"),
     usql.JUDGEMENT_COLUMNS),
    ("cluster conflicts",
     _between(usql.CLUSTER_CONFLICTS_SQL, "SELECT", "FROM autodedup.cluster_conflicts"),
     usql.CONFLICT_COLUMNS),
    ("candidate pairs",
     _between(usql.CANDIDATE_PAIRS_SQL, "SELECT", "FROM autodedup.pairs"),
     usql.CANDIDATE_PAIR_COLUMNS),
    ("listing detail",
     _between(usql.LISTING_DETAIL_SQL, "SELECT", "FROM listings"),
     usql.LISTING_DETAIL_COLUMNS),
    # The outer SELECT of the CTE, named exactly: `SELECT b.block_key FROM blocks b` also
    # appears inside the `named` CTE's two semi-joins.
    ("blocks",
     usql.BLOCKS_SQL.rsplit("SELECT ", 1)[1].split("FROM blocks b")[0],
     usql.BLOCK_COLUMNS),
)


@pytest.mark.parametrize("name,select_list,columns", _CASES, ids=[c[0] for c in _CASES])
def test_the_select_list_and_the_column_tuple_are_the_same_length(
    name: str, select_list: str, columns: tuple[str, ...]
) -> None:
    items = _top_level(select_list)
    assert len(items) == len(columns), (
        f"{name}: the statement selects {len(items)} expressions and the tuple names "
        f"{len(columns)} — zip would silently drop or mislabel the difference"
    )
