"""Migration 564's one-time copy, executed (E299): the operator's Browse merges become rows of
`autodedup.operator_merges` with 560's members and sides, and every `same` ruling a merge wrote
is linked to its group by a column — 560's copied rulings and a merge made through
`merge_property_set` since 559 alike. Runs in CI's migrations job (`TEST_DATABASE_URL`); every
test rolls back.
"""

from __future__ import annotations

import itertools
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from autodedup import labels_sql
from toolkit.property_identity import detach_listing, merge_property_set

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

OP = "ci-operator@replay.local"
_SREALITY_IDS = itertools.count(9_300_000_001)
_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


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


def _property(cur: Any) -> int:
    cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
    return int(cur.fetchone()[0])


def _advert(cur: Any, pid: int, *, source: str) -> int:
    cur.execute(
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, "
        "category_main, category_type, price_czk, area_m2, is_active, property_id) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'byt', 'prodej', 5000000, 70, true, %s) RETURNING id",
        (next(_SREALITY_IDS) if source == "sreality" else None, source,
         f"om-{uuid.uuid4()}", pid),
    )
    return int(cur.fetchone()[0])


def _recompute(cur: Any, pid: int) -> None:
    from scripts.recompute_property_stats import _RECOMPUTE_ONE_SQL

    cur.execute(_RECOMPUTE_ONE_SQL, {"pid": pid})


def _merge(cur: Any, ids: list[int], *, source: str) -> str:
    return merge_property_set(cur.connection, ids, source=source, reason="manual_subset",
                              decided_by=OP)["data"]["merge_group_id"]


def _pair(x: int, y: int) -> tuple[int, int]:
    return (min(x, y), max(x, y))


def _statement(path: Path, start: str, end: str) -> str:
    sql = path.read_text(encoding="utf-8")
    head = sql.index(start)
    return sql[head:sql.index(end, head) + len(end)].strip().rstrip(";")


def _copy_560() -> str:
    sql = (_MIGRATIONS / "560_one_merge_one_undo.sql").read_text(encoding="utf-8")
    return sql[sql.index("with live as ("):].strip().rstrip(";")


def _copy_564() -> str:
    return _statement(_MIGRATIONS / "564_autodedup_operator_merges.sql", "with live as (",
                      "on conflict (merge_group_id) do nothing;")


def _backfill_564() -> str:
    path = _MIGRATIONS / "564_autodedup_operator_merges.sql"
    sql = path.read_text(encoding="utf-8")
    head = sql.index("update autodedup.verdicts v")
    return sql[head:sql.index("reset lock_timeout", head)].strip().rstrip(";")


def test_the_copy_records_560s_members_and_sides_and_links_the_rulings(cur):
    survivor, absorbed, brought, undone = (_property(cur) for _ in range(4))
    s1 = _advert(cur, survivor, source="sreality")
    b1 = _advert(cur, brought, source="remax")
    a1 = _advert(cur, absorbed, source="idnes")
    u1 = _advert(cur, undone, source="bazos")
    for pid in (survivor, absorbed, brought, undone):
        _recompute(cur, pid)
    # b1 arrives on the survivor through a merge that is not the operator's: its side is
    # `brought`, so the operator's group never pairs it.
    _merge(cur, [survivor, brought], source="autodedup")
    old = _merge(cur, [survivor, absorbed], source="autodedup")
    gone = _merge(cur, [survivor, undone], source="autodedup")
    detach_listing(cur.connection, u1, decided_by=OP, source="autodedup")
    cur.execute("UPDATE property_merge_events SET source = 'operator' "
                "WHERE merge_group_id = ANY(%s::uuid[])", ([old, gone],))
    cur.execute(_copy_560())

    # A merge the operator made through Browse since 559 rules its own pairs.
    left, right = _property(cur), _property(cur)
    c1 = _advert(cur, left, source="sreality")
    c2 = _advert(cur, right, source="ceskereality")
    _recompute(cur, left)
    _recompute(cur, right)
    browse = _merge(cur, [left, right], source="operator")

    cur.execute(_copy_564())
    assert cur.rowcount == 2, "the undone merge is not copied"
    cur.execute(
        "SELECT merge_group_id::text, member_ids, member_sides, member_property_ids, n_pairs, "
        "       survivor_property_id, retired_property_ids, events, undone_events, status, "
        "       source, decided_by, copied_by "
        "FROM autodedup.operator_merges WHERE merge_group_id = ANY(%s::uuid[]) "
        "ORDER BY merged_at", ([old, gone, browse],))
    rows = {row[0]: row for row in cur.fetchall()}
    assert set(rows) == {old, browse}
    first = rows[old]
    assert first[1] == sorted([s1, a1])
    sides = dict(zip(first[1], first[2]))
    assert sides == {s1: survivor, a1: absorbed}
    assert first[3] == [survivor, survivor]
    assert first[4] == 1
    assert (first[5], first[6], first[7], first[8]) == (survivor, [absorbed], 1, 0)
    assert first[9:] == ("live", "browse", "operator", "migration 564")
    assert rows[browse][4] == 1 and sorted(rows[browse][1]) == sorted([c1, c2])

    cur.execute(_backfill_564())
    assert cur.rowcount == 2
    cur.execute(
        "SELECT listing_lo, listing_hi, operator_merge_group_id::text FROM autodedup.verdicts "
        "WHERE kind = 'pair' AND operator_merge_group_id IS NOT NULL "
        "AND listing_lo = ANY(%s::bigint[]) ORDER BY 1",
        ([s1, a1, c1, c2],))
    assert cur.fetchall() == sorted([(*_pair(s1, a1), old), (*_pair(c1, c2), browse)])

    cur.execute(_copy_564())
    assert cur.rowcount == 0, "a re-run copies nothing"
    cur.execute(_backfill_564())
    assert cur.rowcount == 0, "a re-run links nothing twice"


def test_a_ruling_typed_against_a_pair_is_never_linked(cur):
    """The link names a merge's OWN ruling: a verdict the operator typed on the pair stays a
    verdict, whatever its note says about something else."""
    left, right = _property(cur), _property(cur)
    c1 = _advert(cur, left, source="sreality")
    c2 = _advert(cur, right, source="idnes")
    _recompute(cur, left)
    _recompute(cur, right)
    cur.execute(
        "INSERT INTO autodedup.verdicts (kind, listing_lo, listing_hi, verdict, note, "
        "decided_by) VALUES ('pair', %s, %s, 'same', 'stejny byt', %s)", (*_pair(c1, c2), OP))
    browse = _merge(cur, [left, right], source="operator")
    cur.execute(_copy_564())
    cur.execute(_backfill_564())
    cur.execute(
        "SELECT note, operator_merge_group_id::text FROM autodedup.verdicts "
        "WHERE kind = 'pair' AND listing_lo = %s AND listing_hi = %s ORDER BY id",
        _pair(c1, c2))
    # The merge's ruling is a row of its own (migration 574: the store is a ledger), and only
    # that row is linked; the typed ruling keeps its note and no link.
    assert cur.fetchall() == [("stejny byt", None), (f"operator merge {browse}", browse)]


def test_the_labels_lane_reads_the_copy(cur):
    left, right = _property(cur), _property(cur)
    c1 = _advert(cur, left, source="sreality")
    c2 = _advert(cur, right, source="remax")
    _recompute(cur, left)
    _recompute(cur, right)
    browse = _merge(cur, [left, right], source="operator")
    cur.execute(_copy_564())
    cur.execute(_backfill_564())

    cur.execute(labels_sql.OPERATOR_MERGES_PRESENT_SQL)
    assert cur.fetchone() == (True,)
    cur.execute(labels_sql.OPERATOR_MERGES_SQL)
    assert browse in {str(row[0]) for row in cur.fetchall()}
    cur.execute(labels_sql.OPERATOR_MERGE_LINKS_SQL)
    assert browse in {str(row[1]) for row in cur.fetchall()}
    cur.execute(labels_sql.OPERATOR_MERGE_MEMBERS_SQL, {"ids": [c1, c2]})
    assert sorted(row[0] for row in cur.fetchall()) == sorted([c1, c2])
    cur.execute(labels_sql.PAIR_VERDICTS_SQL)
    assert any(row[0] == min(c1, c2) and row[7] is not None for row in cur.fetchall())
