"""Migration 573 executed (E919): the operator's rulings are a ledger, and every reader obeys the
newest row. A flip and a withdrawal are NEW rows; the lane's must-links (`RT_MUST_LINK_SQL`), the
apply path's negatives (`apply_sql.PAIR_VERDICTS_SQL`, `Negatives.read` over group rulings) and
the rulings page's own reads all take the newest word per pair / per set. The writes also hold on
a store the migration has not reached (the pre-573 unique indexes, re-created inside the test's
transaction). Runs in CI's migrations job (`TEST_DATABASE_URL`); every test rolls back.
"""

from __future__ import annotations

import itertools
import os
import uuid
from typing import Any

import pytest

from autodedup import apply_sql
from autodedup import incremental_sql
from autodedup import ui_sql as usql
from autodedup.apply import Negatives
from toolkit.property_identity import record_ruling

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


def _advert(cur: Any, pid: int, *, source: str = "sreality") -> int:
    cur.execute(
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, "
        "category_main, category_type, price_czk, area_m2, is_active, property_id) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'byt', 'prodej', 5000000, 70, true, %s) RETURNING id",
        (next(_SREALITY_IDS) if source == "sreality" else None, source,
         f"ledger-{uuid.uuid4()}", pid),
    )
    return int(cur.fetchone()[0])


def _pair(cur: Any) -> tuple[int, int]:
    a = _advert(cur, _property(cur))
    b = _advert(cur, _property(cur), source="idnes")
    return min(a, b), max(a, b)


def _rows(cur: Any, lo: int, hi: int) -> list[tuple[str, str | None]]:
    cur.execute("SELECT verdict, note FROM autodedup.verdicts WHERE kind = 'pair' "
                "AND listing_lo = %s AND listing_hi = %s ORDER BY decided_at, id", (lo, hi))
    return [(str(v), n) for v, n in cur.fetchall()]


def _must_links(cur: Any) -> set[tuple[int, int]]:
    cur.execute(incremental_sql.RT_MUST_LINK_SQL)
    return {(int(lo), int(hi)) for lo, hi in cur.fetchall()}


def _negatives(cur: Any, lo: int, hi: int) -> set[tuple[int, int]]:
    cur.execute(apply_sql.PAIR_VERDICTS_SQL, {
        "listing_ids": [lo, hi], "negatives": list(usql.NEGATIVE_VERDICTS)})
    return {(int(a), int(b)) for a, b, _v in cur.fetchall()}


def _veto(cur: Any, lo: int, hi: int) -> bool:
    cur.execute("SELECT 1 FROM autodedup.must_not_link WHERE listing_lo = %s "
                "AND listing_hi = %s AND source = 'operator'", (lo, hi))
    return cur.fetchone() is not None


def _rule(cur: Any, lo: int, hi: int, verdict: str, note: str | None = None,
          by: str = OP) -> tuple[Any, ...] | None:
    return record_ruling(cur.connection, lo, hi, verdict=verdict, decided_by=by, note=note)


def test_a_flip_and_a_withdrawal_are_new_rows_and_the_newest_one_binds(cur):
    lo, hi = _pair(cur)
    stored = _rule(cur, lo, hi, "same")
    assert stored is not None and stored[usql.VERDICT_COLUMNS.index("verdict")] == "same"
    # Saying the same thing again appends nothing and answers the standing row.
    again = _rule(cur, lo, hi, "same")
    assert again is not None and again[0] == stored[0]
    assert _rows(cur, lo, hi) == [("same", None)]
    assert (lo, hi) in _must_links(cur)

    _rule(cur, lo, hi, "unsure", "nejsem si jistý")
    assert _rows(cur, lo, hi) == [("same", None), ("unsure", "nejsem si jistý")]
    assert (lo, hi) not in _must_links(cur), "a withdrawn same still binds the lane"
    assert not _negatives(cur, lo, hi)

    _rule(cur, lo, hi, "different", "jiné patro")
    assert [v for v, _ in _rows(cur, lo, hi)] == ["same", "unsure", "different"]
    assert _negatives(cur, lo, hi) == {(lo, hi)}
    assert _veto(cur, lo, hi)
    assert (lo, hi) not in _must_links(cur)

    # A flip back by ANOTHER decider still wins: the newest word, whoever said it.
    _rule(cur, lo, hi, "same", by="operator")
    assert not _negatives(cur, lo, hi)
    assert not _veto(cur, lo, hi)
    assert (lo, hi) in _must_links(cur)
    cur.execute(usql.PAIR_NEWEST_RULING_SQL, {"listing_lo": lo, "listing_hi": hi})
    newest = cur.fetchone()
    assert newest[usql.VERDICT_COLUMNS.index("verdict")] == "same"
    assert newest[usql.VERDICT_COLUMNS.index("decided_by")] == "operator"


def test_a_group_correction_appends_on_its_set_and_apply_reads_the_newest(cur):
    a, b = _pair(cur)

    def say(verdict: str, by: str = OP) -> None:
        cur.execute(usql.VERDICT_CLUSTER_APPEND_SQL, {
            "cluster_key": a, "verdict": verdict, "note": None, "reasons": [],
            "decided_by": by, "generation": "g-ledger", "member_ids": [a, b]})

    def refused() -> bool:
        return bool(Negatives.read(cur.connection, [a, b], [a]).sets.get(a))

    say("different")
    assert refused()
    say("same", by="someone.else@replay.local")
    assert not refused(), "an older negative by another operator outlived the newer same"
    say("different")
    assert refused()
    say("unsure")
    assert not refused(), "a withdrawn group negative still refuses"
    cur.execute("SELECT count(*) FROM autodedup.verdicts WHERE kind = 'cluster' "
                "AND cluster_key = %s", (a,))
    assert cur.fetchone() == (4,)
    cur.execute(usql.CLUSTER_NEWEST_RULING_SQL, {"cluster_key": a, "generation": "g-ledger"})
    assert cur.fetchone()[usql.VERDICT_COLUMNS.index("verdict")] == "unsure"


def test_the_writes_hold_on_a_store_573_has_not_reached(cur):
    """The code ships before the migration is applied: with the pre-573 unique indexes back,
    a same-decider re-ruling updates that decider's row in place and never raises."""
    cur.execute("CREATE UNIQUE INDEX pre573_pair_uidx ON autodedup.verdicts "
                "(kind, listing_lo, listing_hi, decided_by) WHERE kind = 'pair'")
    cur.execute("CREATE UNIQUE INDEX pre573_cluster_uidx ON autodedup.verdicts "
                "(kind, cluster_key, (coalesce(generation, ''::text)), decided_by) "
                "WHERE kind = 'cluster'")
    lo, hi = _pair(cur)
    _rule(cur, lo, hi, "same")
    flipped = _rule(cur, lo, hi, "different", "jiné patro")
    assert flipped is not None and flipped[usql.VERDICT_COLUMNS.index("verdict")] == "different"
    assert _rows(cur, lo, hi) == [("different", "jiné patro")]
    _rule(cur, lo, hi, "same", by="operator")
    assert [v for v, _ in _rows(cur, lo, hi)] == ["different", "same"]
    for verdict in ("same", "unsure"):
        cur.execute(usql.VERDICT_CLUSTER_APPEND_SQL, {
            "cluster_key": lo, "verdict": verdict, "note": None, "reasons": [],
            "decided_by": OP, "generation": "g-pre", "member_ids": [lo, hi]})
        assert cur.fetchone()[usql.VERDICT_COLUMNS.index("verdict")] == verdict
    cur.execute("SELECT count(*) FROM autodedup.verdicts WHERE kind = 'cluster' "
                "AND cluster_key = %s", (lo,))
    assert cur.fetchone() == (1,)


def _params(**over: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "generation": "g-ledger-none", "verdict": None, "source": None, "status": None,
        "engine": None, "together": None, "decided_from": None, "decided_to": None,
        "obec": None, "cast_obce": None, "listing": None, "property": None,
        "merge_group": None,
    }
    params.update(over)
    return params


def _page(cur: Any, sql: str, columns: tuple[str, ...], **over: Any) -> list[dict[str, Any]]:
    params = _params(**over)
    params.update({"after_at": None, "after_lo": None, "after_hi": None, "after_key": None,
                   "limit": 50})
    cur.execute(sql, params)
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def test_the_rulings_page_lists_every_source_with_its_status(cur):
    lo, hi = _pair(cur)
    _rule(cur, lo, hi, "same")
    _rule(cur, lo, hi, "unsure")
    (row,) = _page(cur, usql.RULINGS_PAIR_SQL, usql.RULING_PAIR_COLUMNS, listing=lo)
    assert (row["listing_lo"], row["listing_hi"]) == (lo, hi)
    assert (row["source"], row["status"], row["verdict"], row["n_rows"]) == (
        "pair", "withdrawn", "unsure", 2)
    assert (row["together_now"], row["engine_view"], row["agreement"]) == (
        False, "unseen", "none")

    # A group confirmed `same` implies its member pairs; a veto with no ruling is listed too.
    c, d = _pair(cur)
    cur.execute(usql.VERDICT_CLUSTER_APPEND_SQL, {
        "cluster_key": c, "verdict": "same", "note": None, "reasons": [], "decided_by": OP,
        "generation": "g-ledger", "member_ids": [c, d]})
    (implied,) = _page(cur, usql.RULINGS_PAIR_SQL, usql.RULING_PAIR_COLUMNS, listing=c)
    assert (implied["source"], implied["ruling_kind"], implied["group_cluster_key"]) == (
        "implied", "cluster", c)
    # Same, but apart now: the page says the ruling is not reflected in production.
    assert implied["agreement"] == "disagrees"
    e, f = _pair(cur)
    cur.execute(usql.MUST_NOT_LINK_UPSERT_SQL, {"listing_lo": e, "listing_hi": f,
                                                "reason": "operator: different"})
    (veto,) = _page(cur, usql.RULINGS_PAIR_SQL, usql.RULING_PAIR_COLUMNS, listing=e)
    assert (veto["source"], veto["ruling_id"], veto["must_not_link"]) == (
        "must_not_link", None, "operator")

    cur.execute(usql.RULINGS_PAIR_FACETS_SQL, _params(listing=lo))
    facets = {(facet, value): n for facet, value, n in cur.fetchall()}
    assert facets[("total", None)] == 1
    assert facets[("status", "withdrawn")] == 1

    (group,) = _page(cur, usql.RULINGS_GROUP_SQL, usql.RULING_GROUP_COLUMNS, listing=c)
    assert (group["source"], group["set_recorded"], group["n_members"], group["n_properties"],
            group["status"], group["agreement"]) == ("group", True, 2, 2, "standing",
                                                       "disagrees")
    cur.execute(usql.RULINGS_GROUP_FACETS_SQL, _params(listing=c))
    assert ("total", None, 1) in [tuple(r) for r in cur.fetchall()]
    cur.execute(usql.RULING_TOWNS_SQL, {"limit": 40})
    cur.fetchall()
