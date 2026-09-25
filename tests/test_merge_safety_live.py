"""Merge safety, executed (migrations 559 and 560): a price step never spans two adverts, a
merge writes no status row and a detach restores the absorbed property's own state, merging
then detaching every advert gives back every original property, the operator's merge and
detach land as rulings the apply adapter reads, and the one-time copy rules only what the
operator judged. Runs in CI's migrations job (`TEST_DATABASE_URL`); every test rolls back.
"""

from __future__ import annotations

import itertools
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from toolkit.property_identity import detach_listing, merge_property_set

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

OP = "ci-operator@replay.local"
_SREALITY_IDS = itertools.count(9_200_000_001)


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


def _advert(cur: Any, pid: int, *, source: str, price: int) -> int:
    cur.execute(
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, "
        "category_main, category_type, price_czk, area_m2, is_active, property_id) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'byt', 'prodej', %s, 70, true, %s) RETURNING id",
        (next(_SREALITY_IDS) if source == "sreality" else None, source,
         f"ms-{uuid.uuid4()}", price, pid),
    )
    return int(cur.fetchone()[0])


def _snapshot(cur: Any, listing_id: int, price: int | None, hours_ago: float) -> int:
    cur.execute(
        "INSERT INTO listing_snapshots (listing_id, scraped_at, price_czk, content_hash, raw_json) "
        "VALUES (%s, now() - make_interval(secs => %s), %s, %s, '{}'::jsonb) RETURNING id",
        (listing_id, hours_ago * 3600, price, f"ms-{uuid.uuid4()}"),
    )
    return int(cur.fetchone()[0])


def _recompute(cur: Any, pid: int) -> None:
    from scripts.recompute_property_stats import _RECOMPUTE_ONE_SQL

    cur.execute(_RECOMPUTE_ONE_SQL, {"pid": pid})


def test_a_step_never_spans_two_adverts(cur):
    """Two portals quoting 5.0M and 5.2M, read alternately, are not a drop and a rise on
    every scrape: only the one advert's own cut from 5.0M to 4.9M is a step — in the
    view, in the rollup's counts and in the watchdog's drop reader alike."""
    from api.notifications import _recent_price_drops

    pid = _property(cur)
    a = _advert(cur, pid, source="sreality", price=4_900_000)
    b = _advert(cur, pid, source="idnes", price=5_200_000)
    _snapshot(cur, a, 5_000_000, 5)
    _snapshot(cur, b, 5_200_000, 4)
    _snapshot(cur, a, 5_000_000, 3)
    _snapshot(cur, b, 5_200_000, 2)
    _snapshot(cur, a, None, 1.5)          # an unpriced snapshot is not a price reading
    cut = _snapshot(cur, a, 4_900_000, 1)

    cur.execute(
        "SELECT listing_id, snapshot_id, price_czk, prev_price_czk "
        "FROM listing_price_steps WHERE property_id = %s", (pid,))
    assert cur.fetchall() == [(a, cut, 4_900_000, 5_000_000)]

    _recompute(cur, pid)
    cur.execute(
        "SELECT price_drop_count, price_rise_count, price_change_count, "
        "       round(max_price_drop_pct, 2), round(total_price_change_pct, 2) "
        "FROM properties WHERE id = %s", (pid,))
    assert cur.fetchone() == (1, 0, 1, 2.00, -2.00)

    drops = [d for d in _recent_price_drops(cur.connection, window_days=2) if d[0] == pid]
    assert drops == [(pid, cut, 4_900_000, 5_000_000)]


def _events(cur: Any, pid: int) -> list[bool]:
    cur.execute(
        "SELECT is_active FROM property_status_events WHERE property_id = %s ORDER BY id",
        (pid,))
    return [bool(r[0]) for r in cur.fetchall()]


def _pair(x: int, y: int) -> tuple[int, int]:
    return (min(x, y), max(x, y))


def _merged_pair(cur: Any) -> tuple[int, int, int]:
    survivor, absorbed = _property(cur), _property(cur)
    _advert(cur, survivor, source="sreality", price=5_000_000)
    advert = _advert(cur, absorbed, source="idnes", price=5_000_000)
    _recompute(cur, survivor)
    _recompute(cur, absorbed)
    return survivor, absorbed, advert


def _merge(cur: Any, ids: list[int], *, source: str = "operator") -> dict[str, Any]:
    return merge_property_set(cur.connection, ids, source=source, reason="manual_subset",
                              decided_by=OP)["data"]


def _detach(cur: Any, listing_id: int, **kw: Any) -> dict[str, Any]:
    return detach_listing(cur.connection, listing_id, decided_by=OP, **kw)["data"]


def _placed(cur: Any, ids: list[int]) -> dict[int, int]:
    cur.execute("SELECT id, property_id FROM listings WHERE id = ANY(%s)", (ids,))
    return {int(lid): int(pid) for lid, pid in cur.fetchall()}


def test_a_merge_writes_no_status_row_and_the_absorbed_history_stays_its_own(cur):
    survivor, absorbed, moved = _merged_pair(cur)
    before_s, before_a = _events(cur, survivor), _events(cur, absorbed)

    assert _merge(cur, [survivor, absorbed])["survivor_id"] == survivor
    assert _events(cur, absorbed) == before_a, "a merge wrote or moved the absorbed history"
    assert _events(cur, survivor) == before_s
    assert _detach(cur, moved)["restored_property_id"] == absorbed
    cur.execute("SELECT status, is_active FROM properties WHERE id = %s", (absorbed,))
    assert cur.fetchone() == ("active", True)
    assert _events(cur, absorbed) == before_a, "its own history already reads active"

    # Its next real deactivation closes the window its own history opened (no blank chart).
    cur.execute("UPDATE properties SET is_active = false WHERE id = %s", (absorbed,))
    assert _events(cur, absorbed) == [True, False]


def test_a_detach_gives_a_property_ending_on_inactive_its_active_state_back(cur):
    """Every merge before 559 left the absorbed property on a false 'inactive' (211,026 rows
    today). Its undo must log 'active', or the chart reads a live property as delisted."""
    survivor, absorbed, moved = _merged_pair(cur)
    cur.execute(
        "INSERT INTO property_status_events (property_id, is_active, event_at) "
        "VALUES (%s, false, now())", (absorbed,))
    _merge(cur, [survivor, absorbed])
    assert _events(cur, absorbed) == [True, False]

    _detach(cur, moved)
    assert _events(cur, absorbed) == [True, False, True]


def test_merging_then_detaching_every_advert_restores_every_original_property(cur):
    """W3's gate, executed: three set merges, one built on another, then a detach of every
    advert; every original property_id is back, every property active, no ledger row live."""
    props = [_property(cur) for _ in range(6)]
    adverts = [_advert(cur, pid, source=src, price=5_000_000)
               for pid, src in zip(props, ("sreality", "idnes", "remax", "bazos", "maxima",
                                           "realitymix"))]
    for pid in props:
        _recompute(cur, pid)
    original = _placed(cur, adverts)
    _merge(cur, props[:3])
    _merge(cur, props[3:5], source="autodedup")
    _merge(cur, [props[0], props[3], props[5]])
    assert len(set(_placed(cur, adverts).values())) == 1

    for lid in adverts:
        _detach(cur, lid)
    assert _placed(cur, adverts) == original
    cur.execute("SELECT count(*) FROM properties WHERE id = ANY(%s) AND status = 'active'",
                (props,))
    assert cur.fetchone()[0] == len(props)
    cur.execute("SELECT count(*) FROM property_merge_events "
                "WHERE listing_ref_id = ANY(%s) AND undone_at IS NULL", (adverts,))
    assert cur.fetchone()[0] == 0


def _rulings(cur: Any, ids: list[int], by: str = OP) -> list[tuple[int, int, str]]:
    cur.execute(
        "SELECT listing_lo, listing_hi, verdict FROM autodedup.verdicts "
        "WHERE kind = 'pair' AND decided_by = %s AND listing_lo = ANY(%s) "
        "AND listing_hi = ANY(%s) ORDER BY listing_lo, listing_hi", (by, ids, ids))
    return [(int(lo), int(hi), v) for lo, hi, v in cur.fetchall()]


def _vetoes(cur: Any, ids: list[int]) -> list[tuple[int, int]]:
    cur.execute(
        "SELECT listing_lo, listing_hi FROM autodedup.must_not_link "
        "WHERE listing_lo = ANY(%(ids)s) AND listing_hi = ANY(%(ids)s) "
        "AND source = 'operator' ORDER BY 1, 2", {"ids": ids})
    return [(int(lo), int(hi)) for lo, hi in cur.fetchall()]


def test_the_operator_merge_rules_the_cards_and_the_detach_rules_the_advert_against_the_rest(
        cur):
    """s2 sits on the survivor beside its card s1 (the removed engine put it there) and the
    operator vetoed s2 = a1 earlier: the merge of the two cards rules s1 = a1 only and leaves
    that veto standing; detaching a1 rules it different from both adverts that stay."""
    from autodedup import ui_sql as usql

    survivor, absorbed = _property(cur), _property(cur)
    s1 = _advert(cur, survivor, source="sreality", price=5_000_000)
    s2 = _advert(cur, survivor, source="remax", price=5_000_000)
    a1 = _advert(cur, absorbed, source="idnes", price=5_000_000)
    _recompute(cur, survivor)
    _recompute(cur, absorbed)
    ids, card, veto = [s1, s2, a1], _pair(s1, a1), _pair(s2, a1)
    cur.execute(usql.MUST_NOT_LINK_UPSERT_SQL,
                {"listing_lo": veto[0], "listing_hi": veto[1], "reason": "earlier"})

    merged = _merge(cur, [survivor, absorbed])
    assert merged["pairs_ruled_same"] == 1
    assert _rulings(cur, ids) == [(*card, "same")]
    assert _vetoes(cur, ids) == [veto], "a merge of two cards retracted a veto on a third advert"

    detached = _detach(cur, a1, reason="jiné patro")
    assert detached["rulings_written"] == 2
    assert _rulings(cur, ids) == sorted([(*card, "different"), (*veto, "different")])
    assert _vetoes(cur, ids) == sorted([card, veto])

    # The adapter's negative read (autodedup/apply_sql.py PAIR_VERDICTS_SQL), verbatim in
    # shape: any decider, a negative verdict, both sides in the candidate set.
    cur.execute(
        "SELECT v.listing_lo, v.listing_hi FROM autodedup.verdicts v "
        "WHERE v.kind = 'pair' AND v.verdict = any(%(negatives)s::text[]) "
        "AND v.listing_lo = any(%(ids)s::bigint[]) AND v.listing_hi = any(%(ids)s::bigint[]) "
        "ORDER BY 1, 2",
        {"negatives": list(usql.NEGATIVE_VERDICTS), "ids": ids})
    assert [(int(lo), int(hi)) for lo, hi in cur.fetchall()] == sorted([card, veto])


def _copy_statement() -> str:
    sql = (Path(__file__).resolve().parent.parent / "migrations"
           / "560_one_merge_one_undo.sql").read_text()
    return sql[sql.index("with live as ("):].strip().rstrip(";")


def test_the_copy_rules_the_operators_live_merge_same_and_nothing_it_did_not_judge(cur):
    """Migration 560's copy, executed: a pre-ruling operator merge is ruled "same" on the two
    adverts it united; an advert another merge brought there and a merge since undone are
    left out; a re-run inserts nothing."""
    survivor, absorbed, brought, undone = (_property(cur) for _ in range(4))
    s1 = _advert(cur, survivor, source="sreality", price=5_000_000)
    b1 = _advert(cur, brought, source="remax", price=5_000_000)
    a1 = _advert(cur, absorbed, source="idnes", price=5_000_000)
    u1 = _advert(cur, undone, source="bazos", price=5_000_000)
    for pid in (survivor, absorbed, brought, undone):
        _recompute(cur, pid)
    _merge(cur, [survivor, brought], source="autodedup")
    old = _merge(cur, [survivor, absorbed], source="autodedup")["merge_group_id"]
    gone = _merge(cur, [survivor, undone], source="autodedup")["merge_group_id"]
    detach_listing(cur.connection, u1, decided_by=OP, source="autodedup")
    cur.execute("UPDATE property_merge_events SET source = 'operator' "
                "WHERE merge_group_id = ANY(%s::uuid[])", ([old, gone],))

    cur.execute(_copy_statement())
    ids = [s1, b1, a1, u1]
    assert _rulings(cur, ids, by="operator") == [(*_pair(s1, a1), "same")]
    cur.execute("SELECT decided_at = (SELECT min(created_at) FROM property_merge_events "
                "WHERE merge_group_id = %s::uuid) FROM autodedup.verdicts "
                "WHERE decided_by = 'operator' AND listing_lo = %s AND listing_hi = %s",
                (old, *_pair(s1, a1)))
    assert cur.fetchone() == (True,), "the ruling is dated when the operator merged"

    cur.execute(_copy_statement())
    assert cur.rowcount == 0, "a re-run inserts nothing"
