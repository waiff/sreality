"""Merge safety, executed (migration 559): a price step never spans two adverts, a merge
writes no status row and an unmerge restores the absorbed property's own state, and the
operator's merge / undo land as rulings on the cards the operator judged, readable by the
apply adapter's negatives.

The fakes in tests/test_merge_safety.py pin the SQL shape; only executed SQL can show the
view, the trigger and the ruling store agree. Runs in CI's migrations job
(`TEST_DATABASE_URL`); skipped locally. Every test rolls back.
"""

from __future__ import annotations

import itertools
import os
import uuid
from typing import Any

import pytest

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


def _merged_pair(cur: Any) -> tuple[int, int]:
    survivor, absorbed = _property(cur), _property(cur)
    _advert(cur, survivor, source="sreality", price=5_000_000)
    _advert(cur, absorbed, source="idnes", price=5_000_000)
    _recompute(cur, survivor)
    _recompute(cur, absorbed)
    return survivor, absorbed


def test_a_merge_writes_no_status_row_and_the_absorbed_history_stays_its_own(cur):
    import api.property_merge as pm

    survivor, absorbed = _merged_pair(cur)
    before_s, before_a = _events(cur, survivor), _events(cur, absorbed)

    merged = pm.merge_property_set(cur.connection, [survivor, absorbed], decided_by=OP)
    assert merged is not None and merged["survivor_id"] == survivor
    assert _events(cur, absorbed) == before_a, "a merge wrote or moved the absorbed history"
    assert _events(cur, survivor) == before_s

    pm.unmerge(cur.connection, merged["merge_group_id"], decided_by=OP)
    cur.execute("SELECT status, is_active FROM properties WHERE id = %s", (absorbed,))
    assert cur.fetchone() == ("active", True)
    assert _events(cur, absorbed) == before_a, "its own history already reads active"

    # Its next real deactivation closes the window its own history opened (no blank chart).
    cur.execute("UPDATE properties SET is_active = false WHERE id = %s", (absorbed,))
    assert _events(cur, absorbed) == [True, False]


def test_an_unmerge_gives_a_property_ending_on_inactive_its_active_state_back(cur):
    """Every merge before 559 left the absorbed property on a false 'inactive' (211,026 rows
    today). Its undo must log 'active', or the chart reads a live property as delisted."""
    import api.property_merge as pm

    survivor, absorbed = _merged_pair(cur)
    cur.execute(
        "INSERT INTO property_status_events (property_id, is_active, event_at) "
        "VALUES (%s, false, now())", (absorbed,))
    merged = pm.merge_property_set(cur.connection, [survivor, absorbed], decided_by=OP)
    assert merged is not None
    assert _events(cur, absorbed) == [True, False]

    pm.unmerge(cur.connection, merged["merge_group_id"], decided_by=OP)
    assert _events(cur, absorbed) == [True, False, True]


def _rulings(cur: Any, ids: list[int]) -> list[tuple[int, int, str]]:
    cur.execute(
        "SELECT listing_lo, listing_hi, verdict FROM autodedup.verdicts "
        "WHERE kind = 'pair' AND decided_by = %s AND listing_lo = ANY(%s) "
        "AND listing_hi = ANY(%s) ORDER BY listing_lo, listing_hi", (OP, ids, ids))
    return [(int(lo), int(hi), v) for lo, hi, v in cur.fetchall()]


def _vetoes(cur: Any, ids: list[int]) -> list[tuple[int, int]]:
    cur.execute(
        "SELECT listing_lo, listing_hi FROM autodedup.must_not_link "
        "WHERE listing_lo = ANY(%(ids)s) AND listing_hi = ANY(%(ids)s) "
        "AND source = 'operator' ORDER BY 1, 2", {"ids": ids})
    return [(int(lo), int(hi)) for lo, hi in cur.fetchall()]


def test_the_operator_merge_and_undo_rule_only_the_cards_the_operator_saw(cur):
    """s2 sits on the survivor beside its card s1 (the removed engine put it there) and the
    operator vetoed s2 = a1 earlier: the merge of the two cards rules s1 = a1 only and leaves
    that veto standing; the undo rules s1 != a1 only."""
    import api.property_merge as pm
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

    merged = pm.merge_property_set(cur.connection, [survivor, absorbed], decided_by=OP)
    assert merged is not None and merged["pairs_ruled_same"] == 1
    assert _rulings(cur, ids) == [(*card, "same")]
    assert _vetoes(cur, ids) == [veto], "a merge of two cards retracted a veto on a third advert"

    undone = pm.unmerge(cur.connection, merged["merge_group_id"], decided_by=OP, reason="jiné patro")
    assert undone["data"]["pairs_ruled_different"] == 1
    assert _rulings(cur, ids) == [(*card, "different")]
    assert _vetoes(cur, ids) == sorted([card, veto])

    # The adapter's negative read (autodedup/apply_sql.py PAIR_VERDICTS_SQL), verbatim in
    # shape: any decider, a negative verdict, both sides in the candidate set.
    cur.execute(
        "SELECT v.listing_lo, v.listing_hi FROM autodedup.verdicts v "
        "WHERE v.kind = 'pair' AND v.verdict = any(%(negatives)s::text[]) "
        "AND v.listing_lo = any(%(ids)s::bigint[]) AND v.listing_hi = any(%(ids)s::bigint[]) "
        "ORDER BY 1, 2",
        {"negatives": list(usql.NEGATIVE_VERDICTS), "ids": ids})
    assert [(int(lo), int(hi)) for lo, hi in cur.fetchall()] == [card]


def test_undoing_a_group_of_three_rules_no_pair_different(cur):
    """A rejected group says its members are not ALL one property, never which pair was wrong
    (PROGRAM.md E55): the merge's "same" is withdrawn, and no pair is vetoed."""
    import api.property_merge as pm

    props = [_property(cur) for _ in range(3)]
    ids = [_advert(cur, pid, source=src, price=5_000_000)
           for pid, src in zip(props, ("sreality", "idnes", "remax"))]
    for pid in props:
        _recompute(cur, pid)
    every = sorted(_pair(x, y) for i, x in enumerate(ids) for y in ids[i + 1:])

    merged = pm.merge_property_set(cur.connection, props, decided_by=OP)
    assert merged is not None
    assert _rulings(cur, ids) == [(*p, "same") for p in every]

    undone = pm.unmerge(cur.connection, merged["merge_group_id"], decided_by=OP)
    assert undone["data"]["pairs_ruled_different"] == 0
    assert _rulings(cur, ids) == [(*p, "unsure") for p in every]
    assert _vetoes(cur, ids) == []
