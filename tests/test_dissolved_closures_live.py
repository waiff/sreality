"""E926 executed: a dissolved must-link closure's record is appended when it differs from the
NEWEST record of its generation sharing an advert with it, the rulings page reads it by any
member and by generation, and the group dialog does not read it at all. The fakes re-implement
the dedupe in Python; only Postgres runs the jsonb equality and the member filters. Runs in CI's
migrations job (`TEST_DATABASE_URL`); every test rolls back.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from autodedup import ui_sql as usql
from autodedup.incremental_lane import _conflict_params
from autodedup.incremental_sql import RT_CLOSURE_CONFLICT_APPEND_SQL
from autodedup.score_sql import CLUSTER_CONFLICT_INSERT_SQL

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

A, B, C, D = 9_600_000_001, 9_600_000_002, 9_600_000_003, 9_600_000_004
CHAIN = [[A, B], [B, C]]


@pytest.fixture()
def cur():
    import psycopg

    conn = psycopg.connect(
        _DB_URL,
        options="-c statement_timeout=20000 -c lock_timeout=5000"
        " -c idle_in_transaction_session_timeout=30000",
    )
    try:
        with conn.cursor() as c:
            yield c
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture()
def generation() -> str:
    return f"ci-{uuid.uuid4()}"


def _closure(invariant: str, members: list[int], must_link: list[list[int]]) -> dict[str, Any]:
    return {"kind": "invariant", "lo": members[0], "hi": members[-1], "invariant": invariant,
            "members": members, "must_link": must_link}


MNL = _closure("must_not_link", [A, B, C], CHAIN)
RENTAL = _closure("category_type", [A, B, C, D], [*CHAIN, [C, D]])


def _append(cur: Any, generation: str, record: dict[str, Any]) -> None:
    cur.execute(RT_CLOSURE_CONFLICT_APPEND_SQL, _conflict_params(record, generation))


def _filed(cur: Any, generation: str) -> list[tuple[str, list[int]]]:
    cur.execute("SELECT invariant, detail -> 'members' FROM autodedup.cluster_conflicts "
                "WHERE detail ->> 'generation' = %s ORDER BY created_at, id", (generation,))
    return [(str(limb), [int(i) for i in members]) for limb, members in cur.fetchall()]


def _page(cur: Any, generation: str, ids: list[int]) -> list[dict[str, Any]]:
    cur.execute(usql.DISSOLVED_CLOSURES_SQL, {"generation": generation, "ids": ids})
    return [dict(zip(usql.CONFLICT_COLUMNS, row)) for row in cur.fetchall()]


def test_the_same_closure_is_filed_once(cur, generation):
    for _ in range(3):
        _append(cur, generation, MNL)
    assert _filed(cur, generation) == [("must_not_link", [A, B, C])]


def test_a_closure_that_changes_and_changes_back_is_filed_again(cur, generation):
    """Filed-once-EVER would skip the third append, and the newest record naming A and B — the
    one the rulings page shows — would blame the rental for what is the operator's own
    must-not-link."""
    for record in (MNL, RENTAL, MNL, MNL):
        _append(cur, generation, record)
    assert _filed(cur, generation) == [("must_not_link", [A, B, C]),
                                       ("category_type", [A, B, C, D]),
                                       ("must_not_link", [A, B, C])]
    newest = _page(cur, generation, [B])[0]
    assert (newest["invariant"], newest["detail"]["must_link"]) == ("must_not_link", CHAIN)


def test_the_page_reads_a_record_by_any_member_and_by_its_generation_only(cur, generation):
    _append(cur, generation, RENTAL)
    other = f"{generation}-other"
    _append(cur, other, RENTAL)
    assert len(_filed(cur, other)) == 1, "another generation's record does not suppress it"
    for member in (B, C):
        (record,) = _page(cur, generation, [member])
        assert (record["listing_lo"], record["listing_hi"]) == (A, D)
        assert record["detail"]["generation"] == generation
    assert _page(cur, generation, [9_600_000_999]) == []
    assert _page(cur, f"{generation}-none", [B]) == []


def test_the_group_dialog_reads_the_edge_conflicts_not_the_record(cur, generation):
    """The record's two ends are the closure's smallest and largest advert — no pair anyone
    ruled or scored — so the group dialog and the proposed splits must not read it as one."""
    edge = {"kind": "invariant", "lo": A, "hi": C, "invariant": "area_spread",
            "members": [A, C]}
    cur.execute(CLUSTER_CONFLICT_INSERT_SQL, _conflict_params(edge, generation))
    _append(cur, generation, MNL)
    cur.execute(usql.CLUSTER_CONFLICTS_SQL, {"cluster_key": None, "ids": [A, C]})
    rows = [dict(zip(usql.CONFLICT_COLUMNS, row)) for row in cur.fetchall()]
    assert [(r["listing_lo"], r["listing_hi"], r["invariant"]) for r in rows] == [
        (A, C, "area_spread")]
