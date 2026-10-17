"""E940 executed: the three statements the paged scope walk rests on, run by Postgres on seeded
rows — a town's and an okres's keyset pages (full pages, then the short end), the prune bounded
to a page's id range, and `RT_SCOPE_SCAN_SEEN_SQL`'s "read to its end" gate over the page row.
The FakePg tests re-implement each predicate in Python, so a neutralised one passes there; here
it fails. Runs in CI's migrations job (`TEST_DATABASE_URL`); every test rolls back.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest

from autodedup.incremental_lane import EnterBlock, pages_key
from autodedup.incremental_sql import (
    RT_SCOPE_IDS_PRUNE_SQL,
    RT_SCOPE_SCAN_SEEN_SQL,
    RT_SETTING_WRITE_SQL,
)

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

KRAJ, OKRES, TOWN, OTHER, ELSEWHERE = 99, 9_991, 999_901, 999_902, 999_903


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


def _ad(cur: Any, obec: int | None, okres: int | None = OKRES,
        granularity: str = "building") -> int:
    cur.execute(
        "insert into listings (source, source_id_native, raw_json, category_main, "
        " category_type) values ('bazos', %s, '{}'::jsonb, 'byt', 'prodej') returning id",
        (f"e940-{uuid.uuid4()}",))
    listing_id = int(cur.fetchone()[0])
    cur.execute(
        "insert into listing_location (listing_id, match_confidence, granularity, "
        " uncertainty_radius_m, country_status, resolver_version, claim_set_hash, "
        " registry_version, kraj_kod, okres_kod, obec_kod) "
        "values (%s, 'exact', %s, 5, 'cz', 'test', '\\x00'::bytea, 'test', %s, %s, %s)",
        (listing_id, granularity, KRAJ, okres, obec))
    return listing_id


def _query(cur: Any):
    def run(sql: str, params: Any) -> list[tuple]:
        cur.execute(sql, params)
        return cur.fetchall()
    return run


def test_a_town_and_an_okres_are_paged_by_a_keyset_to_their_short_end(cur) -> None:
    town = [_ad(cur, TOWN) for _ in range(5)]
    other = _ad(cur, OTHER)
    alone = _ad(cur, None, granularity="okres")
    _ad(cur, ELSEWHERE, okres=OKRES + 1)
    pages = list(EnterBlock("obec", TOWN, TOWN).pages(_query(cur), 2))
    assert [[row[0] for row in rows] for rows, _end in pages] == [town[0:2], town[2:4], town[4:]]
    assert [end for _rows, end in pages] == [town[1], town[3], None]
    district = EnterBlock("okres", OKRES, obce=(TOWN, OTHER))
    listed = [row[0] for rows, _end in district.pages(_query(cur), 3) for row in rows]
    assert listed == sorted([*town, other, alone]), "every page, the okres-only advert too"


def test_a_page_prunes_only_its_own_id_range(cur, generation: str) -> None:
    block = f"obec:{TOWN}"
    for listing_id in range(10, 16):
        cur.execute("insert into autodedup.rt_scope_ids (generation, block_key, listing_id) "
                    "values (%s, %s, %s)", (generation, block, listing_id))

    def held() -> list[int]:
        cur.execute("select listing_id from autodedup.rt_scope_ids where generation = %s "
                    "order by 1", (generation,))
        return [int(row[0]) for row in cur.fetchall()]

    params = {"generation": generation, "block_key": block}
    cur.execute(RT_SCOPE_IDS_PRUNE_SQL, {**params, "listing_ids": [12], "after_id": 11,
                                         "page_end": 13})
    assert held() == [10, 11, 12, 14, 15], "(11, 13] proves 13 gone; nothing else"
    cur.execute(RT_SCOPE_IDS_PRUNE_SQL, {**params, "listing_ids": [15], "after_id": 13,
                                         "page_end": None})
    assert held() == [10, 11, 12, 15], "the short end: (13, end]"


def test_a_block_inside_its_first_cycle_is_not_walked(cur, generation: str) -> None:
    for block in ("obec:1", "obec:2", "obec:3"):
        cur.execute("insert into autodedup.rt_scope_scan (generation, block_key, rows_found) "
                    "values (%s, %s, 1)", (generation, block))
    cur.execute(RT_SETTING_WRITE_SQL, {
        "key": pages_key(generation), "updated_by": "test",
        "value": json.dumps({"obec:2": {"after": 5, "page": 1, "of": 0},
                             "obec:3": {"after": 9, "page": 1, "of": 3}})})
    cur.execute(RT_SCOPE_SCAN_SEEN_SQL, {"generation": generation})
    assert sorted(row[0] for row in cur.fetchall()) == ["obec:1", "obec:3"]
