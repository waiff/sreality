"""The operator's split statement, executed (E919): the screenshot case on Postgres. s stays, b
(merged in from its own record) leaves, i (merged in too) is confirmed one property with s — the
live lane's must-link read sees (s, i), the must-not-link table holds (b, s) and (b, i) as the
operator's, and the undo puts every advert back on one property and every pair back to its word.
Runs in CI's migrations job (`TEST_DATABASE_URL`); every test rolls back.
"""

from __future__ import annotations

import itertools
import os
import uuid
from typing import Any

import pytest

from autodedup.incremental_sql import RT_MUST_LINK_SQL
from toolkit.property_identity import merge_property_set
from toolkit.property_split import split_property, undo_split

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

OP = "ci-operator@replay.local"
_SREALITY_IDS = itertools.count(9_400_000_001)


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
         f"sp-{uuid.uuid4()}", pid),
    )
    return int(cur.fetchone()[0])


def _pair(x: int, y: int) -> tuple[int, int]:
    return (min(x, y), max(x, y))


def _where(cur: Any, ids: list[int]) -> dict[int, int]:
    cur.execute("SELECT id, property_id FROM listings WHERE id = ANY(%s)", (ids,))
    return {int(a): int(b) for a, b in cur.fetchall()}


def _words(cur: Any, ids: list[int]) -> dict[tuple[int, int], str]:
    cur.execute(
        "SELECT listing_lo, listing_hi, verdict FROM autodedup.verdicts "
        "WHERE kind = 'pair' AND decided_by = %s AND listing_lo = ANY(%s) AND listing_hi = ANY(%s)",
        (OP, ids, ids))
    return {(int(lo), int(hi)): v for lo, hi, v in cur.fetchall()}


def _vetoes(cur: Any, ids: list[int]) -> dict[tuple[int, int], str]:
    cur.execute(
        "SELECT listing_lo, listing_hi, source FROM autodedup.must_not_link "
        "WHERE listing_lo = ANY(%s) AND listing_hi = ANY(%s)", (ids, ids))
    return {(int(lo), int(hi)): src for lo, hi, src in cur.fetchall()}


def test_the_screenshot_case_end_to_end_and_its_undo(cur):
    home, from_b, from_i = _property(cur), _property(cur), _property(cur)
    s = _advert(cur, home, source="sreality")
    b = _advert(cur, from_b, source="bazos")
    i = _advert(cur, from_i, source="idnes")
    merge_property_set(cur.connection, [home, from_b, from_i], source="autodedup", reason="r")
    ids = [s, b, i]

    out = split_property(cur.connection, home, adverts=ids, separate=[[b]], keep_together=True,
                         decided_by=OP, reason="jiná dispozice")
    assert _where(cur, ids) == {s: home, b: from_b, i: home}
    assert [u["property_id"] for u in out["units"]] == [home, from_b]
    assert _words(cur, ids) == {_pair(s, b): "different", _pair(b, i): "different",
                                _pair(s, i): "same"}
    cur.execute(RT_MUST_LINK_SQL)
    assert _pair(s, i) in {(int(lo), int(hi)) for lo, hi in cur.fetchall()}
    assert _vetoes(cur, ids) == {_pair(s, b): "operator", _pair(b, i): "operator"}

    again = split_property(cur.connection, home, adverts=ids, separate=[[b]],
                           keep_together=True, decided_by=OP)
    assert (again["moved"], again["rulings"]["written"]) == (0, 0)

    undo = out["undo"]
    done = undo_split(cur.connection, home, call_id=undo["call_id"],
                      placements=undo["placements"], rulings=undo["rulings"], decided_by=OP)
    assert set(_where(cur, ids).values()) == {done["property_id"]}
    assert set(_words(cur, ids).values()) == {"unsure"} and _vetoes(cur, ids) == {}


def _veto_row(cur: Any, x: int, y: int) -> tuple[str, str | None] | None:
    cur.execute("SELECT source, reason FROM autodedup.must_not_link "
                "WHERE listing_lo = %s AND listing_hi = %s", _pair(x, y))
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def _guard(cur: Any, x: int, y: int, reason: str) -> None:
    cur.execute("INSERT INTO autodedup.must_not_link (listing_lo, listing_hi, source, reason) "
                "VALUES (%s, %s, 'guard', %s)", (*_pair(x, y), reason))


def test_a_guard_veto_is_the_machines_again_after_the_undo(cur):
    """Review finding 2: `different` rewrites the guard row as the operator's; the undo's
    `unsure` retract used to delete it for good."""
    home, away = _property(cur), _property(cur)
    s = _advert(cur, home, source="sreality")
    b = _advert(cur, away, source="bazos")
    merge_property_set(cur.connection, [home, away], source="autodedup", reason="r")
    _guard(cur, s, b, "floor 3 vs 7")

    out = split_property(cur.connection, home, adverts=[s, b], separate=[[b]],
                         keep_together=True, decided_by=OP)
    assert _where(cur, [s, b]) == {s: home, b: away}
    assert _veto_row(cur, s, b)[0] == "operator"
    undo = out["undo"]
    assert undo["rulings"][0]["must_not_link"] == {"source": "guard", "reason": "floor 3 vs 7"}
    undo_split(cur.connection, home, call_id=undo["call_id"], placements=undo["placements"],
               rulings=undo["rulings"], decided_by=OP)
    assert _veto_row(cur, s, b) == ("guard", "floor 3 vs 7")
    assert _words(cur, [s, b]) == {_pair(s, b): "unsure"}


def test_a_group_of_two_own_adverts_leaves_as_one_new_record(cur):
    """Review finding 11, case (a'): two native adverts are each born a record by the one birth
    path, joined by the one merge (decision 17 over the newborns), and ruled `same`."""
    home = _property(cur)
    s = _advert(cur, home, source="sreality")
    b = _advert(cur, home, source="bazos")
    i = _advert(cur, home, source="idnes")
    ids = [s, b, i]

    out = split_property(cur.connection, home, adverts=ids, separate=[[b, i]],
                         keep_together=True, decided_by=OP)
    unit = out["units"][1]
    assert [m["outcome"] for m in unit["moved"]] == ["split_native", "split_native"]
    born = {m["to"] for m in unit["moved"]}
    where = _where(cur, ids)
    assert where[s] == home and where[b] == where[i] == unit["property_id"] and len(born) == 2
    assert unit["property_id"] in born and unit["merge_group_id"] is not None
    cur.execute("SELECT id, status FROM properties WHERE id = ANY(%s)", (sorted(born),))
    assert sorted(st for _pid, st in cur.fetchall()) == ["active", "merged_away"]
    assert _words(cur, ids) == {_pair(s, b): "different", _pair(s, i): "different",
                                _pair(b, i): "same"}
    assert _vetoes(cur, ids) == {_pair(s, b): "operator", _pair(s, i): "operator"}
    cur.execute(RT_MUST_LINK_SQL)
    assert _pair(b, i) in {(int(lo), int(hi)) for lo, hi in cur.fetchall()}

    undo = out["undo"]
    done = undo_split(cur.connection, home, call_id=undo["call_id"],
                      placements=undo["placements"], rulings=undo["rulings"], decided_by=OP)
    assert set(_where(cur, ids).values()) == {done["property_id"]}
    assert set(_words(cur, ids).values()) == {"unsure"} and _vetoes(cur, ids) == {}


def test_the_keeper_swap_keeps_the_record_and_the_machine_veto(cur):
    """Review finding 11, the swap: the property's own advert is separated, so the record stays
    with it and the kept unit's merged-in adverts go home and are joined; a guard row between
    them is still the guard's after the split and after its undo (review finding 2)."""
    home, from_b, from_i = _property(cur), _property(cur), _property(cur)
    s = _advert(cur, home, source="sreality")
    b = _advert(cur, from_b, source="bazos")
    i = _advert(cur, from_i, source="idnes")
    merge_property_set(cur.connection, [home, from_b, from_i], source="autodedup", reason="r")
    _guard(cur, b, i, "g")
    ids = [s, b, i]

    out = split_property(cur.connection, home, adverts=ids, separate=[[s]],
                         keep_together=True, decided_by=OP)
    assert out["record_kept_by"] == "B"
    kept = out["units"][0]
    where = _where(cur, ids)
    assert where[s] == home and where[b] == where[i] == kept["property_id"]
    assert kept["property_id"] in (from_b, from_i) and kept["merge_group_id"] is not None
    assert _words(cur, ids) == {_pair(s, b): "different", _pair(s, i): "different",
                                _pair(b, i): "same"}
    assert _vetoes(cur, ids) == {_pair(s, b): "operator", _pair(s, i): "operator",
                                 _pair(b, i): "guard"}

    undo = out["undo"]
    done = undo_split(cur.connection, home, call_id=undo["call_id"],
                      placements=undo["placements"], rulings=undo["rulings"], decided_by=OP)
    assert set(_where(cur, ids).values()) == {done["property_id"]}
    assert _vetoes(cur, ids) == {_pair(b, i): "guard"} and _veto_row(cur, b, i) == ("guard", "g")
