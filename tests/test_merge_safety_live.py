"""Merge safety, executed (migrations 559, 560 and 561): a price step never spans two adverts, a
merge writes no status row and a detach restores the absorbed property's own state, merging
then detaching every advert a merge moved gives back every original property, a native advert
splits off to a record born the one way, the operator's merge and detach land as rulings the
apply adapter reads, the one-time copy rules only what the operator judged, and a merged
property speaks with ONE canonical advert everywhere (decisions 13 and 18). Runs in CI's
migrations job (`TEST_DATABASE_URL`); every test rolls back.
"""

from __future__ import annotations

import itertools
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from toolkit.property_identity import detach_listing, listing_origins, merge_property_set

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


def _advert(cur: Any, pid: int, *, source: str, price: int = 5_000_000, active: bool = True,
            condition: str | None = None, levels: tuple[Any, Any] = (None, None)) -> int:
    cur.execute(
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, "
        "category_main, category_type, price_czk, area_m2, is_active, property_id, condition, "
        "building_condition_level, apartment_condition_level) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'byt', 'prodej', %s, 70, %s, %s, %s, %s, %s) RETURNING id",
        (next(_SREALITY_IDS) if source == "sreality" else None, source,
         f"ms-{uuid.uuid4()}", price, active, pid, condition, *levels),
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
    advert a merge moved; every original property_id is back, every property active, no ledger
    row live, and the asset link the merges carried 4 -> 3 -> 0 is back on 4 alone."""
    props = [_property(cur) for _ in range(6)]
    adverts = [_advert(cur, pid, source=src, price=5_000_000)
               for pid, src in zip(props, ("sreality", "idnes", "remax", "bazos", "maxima",
                                           "realitymix"))]
    for pid in props:
        _recompute(cur, pid)
    cur.execute("INSERT INTO assets DEFAULT VALUES RETURNING id")
    asset = int(cur.fetchone()[0])
    cur.execute("UPDATE properties SET asset_id = %s WHERE id = %s", (asset, props[4]))
    original = _placed(cur, adverts)
    _merge(cur, props[:3])
    _merge(cur, props[3:5], source="autodedup")
    _merge(cur, [props[0], props[3], props[5]])
    assert len(set(_placed(cur, adverts).values())) == 1

    for lid in sorted(listing_origins(cur.connection, adverts)):
        _detach(cur, lid)
    assert _placed(cur, adverts) == original
    cur.execute("SELECT count(*) FROM properties WHERE id = ANY(%s) AND status = 'active'",
                (props,))
    assert cur.fetchone()[0] == len(props)
    cur.execute("SELECT count(*) FROM property_merge_events "
                "WHERE listing_ref_id = ANY(%s) AND undone_at IS NULL", (adverts,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT id, asset_id FROM properties WHERE id = ANY(%s) AND asset_id IS NOT NULL",
                (props,))
    assert cur.fetchall() == [(props[4], asset)]


def test_a_native_advert_splits_off_to_a_new_record_and_a_merge_back_is_undone_to_it(cur):
    """A property grouped at ingest (no ledger row moved either advert): the split births the
    advert a record through the one birth path, writes ONE closed ledger row the constraints
    accept, and rules it different from the advert that stays; merged back, a detach returns it
    to the record it was born on."""
    pid = _property(cur)
    stay, leave = _advert(cur, pid, source="sreality"), _advert(cur, pid, source="idnes")
    _recompute(cur, pid)
    assert _detach(cur, leave, source="autodedup")["outcome"] == "propose_only"

    out = _detach(cur, leave, reason="jiné patro")
    born = out["restored_property_id"]
    assert (out["outcome"], out["survivor_property_id"]) == ("split_native", pid)
    assert _placed(cur, [stay, leave]) == {stay: pid, leave: born}
    cur.execute("SELECT repr_listing_ref_id, status, is_active FROM properties WHERE id = %s",
                (born,))
    assert cur.fetchone() == (leave, "active", True)
    cur.execute(
        "SELECT survivor_property_id, retired_property_id, prev_property_id, source, undone_by, "
        "undone_at IS NOT NULL FROM property_merge_events WHERE listing_ref_id = %s", (leave,))
    assert cur.fetchall() == [(pid, born, born, "operator", OP, True)]
    assert _rulings(cur, [stay, leave]) == [(*_pair(stay, leave), "different")]
    assert _detach(cur, leave)["outcome"] == "not_merged"

    assert _merge(cur, [pid, born], source="autodedup")["survivor_id"] == pid
    back = _detach(cur, leave)
    assert (back["outcome"], back["restored_property_id"], back["reactivated"]) == (
        "detached", born, True)
    assert _placed(cur, [stay, leave]) == {stay: pid, leave: born}


def test_a_propertys_last_own_advert_stays_while_merged_ones_share_it(cur):
    """Splitting it would leave the survivor with only merged adverts, and their detach an
    active property with none: its own advert stays and the merged one goes home instead."""
    survivor, absorbed, moved = _merged_pair(cur)
    _merge(cur, [survivor, absorbed])
    cur.execute("SELECT id FROM listings WHERE property_id = %s AND id <> %s", (survivor, moved))
    (own,) = (int(r[0]) for r in cur.fetchall())
    assert _detach(cur, own)["outcome"] == "last_native"
    assert _detach(cur, moved)["outcome"] == "detached"
    assert _placed(cur, [own, moved]) == {own: survivor, moved: absorbed}


def _rulings(cur: Any, ids: list[int], by: str = OP) -> list[tuple[int, int, str]]:
    """Each pair's NEWEST ruling by `by` — the ruling every reader obeys since migration 573
    made the store a ledger (a detach after a merge is a second row, not an overwrite)."""
    cur.execute(
        "SELECT DISTINCT ON (listing_lo, listing_hi) listing_lo, listing_hi, verdict "
        "FROM autodedup.verdicts "
        "WHERE kind = 'pair' AND decided_by = %s AND listing_lo = ANY(%s) "
        "AND listing_hi = ANY(%s) ORDER BY listing_lo, listing_hi, decided_at DESC, id DESC",
        (by, ids, ids))
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


# --- one property, one voice (migration 561) --------------------------------------------


def test_active_beats_trust_and_the_id_breaks_a_tie(cur):
    pid = _property(cur)
    _advert(cur, pid, source="sreality", active=False)
    first, _second = _advert(cur, pid, source="idnes"), _advert(cur, pid, source="idnes")
    cur.execute("SELECT listing_id FROM property_canonical_listings(%s) WHERE canonical_rank = 1",
                (pid,))
    assert cur.fetchone()[0] == first
    _recompute(cur, pid)
    cur.execute("SELECT repr_listing_ref_id FROM properties WHERE id = %s", (pid,))
    assert cur.fetchone()[0] == first


def test_condition_and_both_levels_come_from_the_one_canonical_advert(cur):
    """Rule 14: the old golden record took the raw condition trust-first (the delisted
    sreality here) and the levels from the representative: two flats in one row."""
    pid = _property(cur)
    _advert(cur, pid, source="sreality", active=False, condition="novostavba", levels=(1, 1))
    _advert(cur, pid, source="idnes", condition="velmi_dobry", levels=(2, 3))
    _recompute(cur, pid)
    cur.execute("SELECT condition, building_condition_level, apartment_condition_level "
                "FROM properties WHERE id = %s", (pid,))
    assert cur.fetchone() == ("velmi_dobry", 2, 3)


def test_the_price_history_and_the_alerts_are_the_canonical_adverts_own(cur):
    from api.notifications import _recent_price_drops

    pid = _property(cur)
    canon = _advert(cur, pid, source="sreality", price=4_900_000)
    other = _advert(cur, pid, source="idnes", price=4_000_000)
    _snapshot(cur, canon, 5_000_000, 3)
    cut = _snapshot(cur, canon, 4_900_000, 1)
    _snapshot(cur, other, 4_500_000, 3)
    _snapshot(cur, other, 4_000_000, 1)
    _recompute(cur, pid)
    cur.execute("SELECT current_price_czk, price_drop_count, price_change_count "
                "FROM properties WHERE id = %s", (pid,))
    assert cur.fetchone() == (4_900_000, 1, 1)
    drops = [d for d in _recent_price_drops(cur.connection, window_days=2) if d[0] == pid]
    assert drops == [(pid, cut, 4_900_000, 5_000_000)]


def test_a_merge_that_changes_the_canonical_advert_replays_no_older_step(cur):
    """The absorbed sreality advert outranks the survivor's bazos one, so it becomes canonical;
    its cut from yesterday was never the price the property showed, so neither producer fires
    it. A cut after the handover does fire."""
    from api.notifications import _recent_price_drops

    survivor, absorbed = _property(cur), _property(cur)
    _advert(cur, survivor, source="bazos", price=5_200_000)
    moved = _advert(cur, absorbed, source="sreality", price=5_000_000)
    _snapshot(cur, moved, 5_300_000, 30)
    _snapshot(cur, moved, 5_000_000, 20)
    for pid in (survivor, absorbed):
        _recompute(cur, pid)
    _merge(cur, [survivor, absorbed])
    cur.execute("SELECT repr_listing_ref_id, repr_since > '-infinity' FROM properties "
                "WHERE id = %s", (survivor,))
    assert cur.fetchone() == (moved, True)
    assert [d for d in _recent_price_drops(cur.connection, window_days=2) if d[0] == survivor] == []

    after = _snapshot(cur, moved, 4_900_000, -0.01)
    drops = [d for d in _recent_price_drops(cur.connection, window_days=2) if d[0] == survivor]
    assert drops == [(survivor, after, 4_900_000, 5_000_000)]


def test_comparables_count_a_property_once_and_leave_out_the_subjects_siblings(cur):
    """Decision 13: a two-portal comparable is ONE comparable (its canonical advert), and the
    subject's sibling on another portal is not the subject's comparable."""
    from toolkit.comparables import ComparableFilters, TargetSpec, build_query

    subject_p, two_portal, single = _property(cur), _property(cur), _property(cur)
    subject, sibling = (_advert(cur, subject_p, source="sreality"),
                        _advert(cur, subject_p, source="idnes"))
    canon, twin = _advert(cur, two_portal, source="sreality"), _advert(cur, two_portal, source="bazos")
    alone = _advert(cur, single, source="remax")
    ids = {subject, sibling, canon, twin, alone}
    for lid in ids:
        cur.execute(
            "INSERT INTO listing_location (listing_id, geom, match_confidence, granularity, "
            "  uncertainty_radius_m, country_status, resolver_version, claim_set_hash, "
            "  registry_version) VALUES (%s, ST_SetSRID(ST_MakePoint(17.91, 49.01), 4326), "
            "  'exact', 'building', 5, 'cz', 'test', '\\x00'::bytea, 'test')", (lid,))
    for pid in (subject_p, two_portal, single):
        _recompute(cur, pid)
    sql, params = build_query(
        TargetSpec(lat=49.01, lng=17.91, area_m2=70, exclude_listing_ids=[subject]),
        ComparableFilters(radius_m=500, category_main="byt", category_type="prodej"))
    cur.execute(sql, params)
    assert {int(r[0]) for r in cur.fetchall()} & ids == {canon, alone}
