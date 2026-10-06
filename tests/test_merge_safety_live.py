"""Merge safety, executed (migrations 559, 560, 561 and 588): a price step never spans two adverts,
a merge writes no status row and a detach restores the absorbed property's own state, merging
then detaching every advert a merge moved gives back every original property, a native advert
splits off to a record born the one way, the operator's merge and detach land as rulings the
apply adapter reads, the one-time copy rules only what the operator judged, and a merged
property speaks with ONE canonical advert everywhere (decisions 13 and 18), chosen and built by
the merge sprint's rules (docs/design/merge-sprint/PROGRAM.md MS5, MS6, MS10, MS19). Runs in
CI's migrations job (`TEST_DATABASE_URL`); every test rolls back.
"""

from __future__ import annotations

import itertools
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests._live_property import REQUIRED_DB, db_url
from toolkit.property_identity import detach_listings, listing_origins, merge_property_set

pytestmark = REQUIRED_DB

OP = "ci-operator@replay.local"
_SREALITY_IDS = itertools.count(9_200_000_001)


@pytest.fixture()
def cur():
    import psycopg

    conn = psycopg.connect(
        db_url(),
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


def _seen(cur: Any, listing_id: int, first_days_ago: float, last_days_ago: float = 0) -> None:
    """Both seen dates, which the insert leaves at the test transaction's one now()."""
    cur.execute(
        "UPDATE listings SET first_seen_at = now() - make_interval(secs => %s), "
        "last_seen_at = now() - make_interval(secs => %s) WHERE id = %s",
        (first_days_ago * 86400, last_days_ago * 86400, listing_id))


def _located(cur: Any, *listing_ids: int) -> None:
    """A map point per advert (`listing_location.geom`, the canonical order's second key)."""
    for lid in listing_ids:
        cur.execute(
            "INSERT INTO listing_location (listing_id, geom, match_confidence, granularity, "
            "  uncertainty_radius_m, country_status, resolver_version, claim_set_hash, "
            "  registry_version) VALUES (%s, ST_SetSRID(ST_MakePoint(17.91, 49.01), 4326), "
            "  'exact', 'building', 5, 'cz', 'test', '\\x00'::bytea, 'test')", (lid,))


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
    data = detach_listings(cur.connection, [listing_id], decided_by=OP, **kw)["data"]
    return {**data["adverts"][0], "rulings_written": data["rulings_written"]}


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
    row live."""
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

    for lid in sorted(listing_origins(cur.connection, adverts)):
        _detach(cur, lid)
    assert _placed(cur, adverts) == original
    cur.execute("SELECT count(*) FROM properties WHERE id = ANY(%s) AND status = 'active'",
                (props,))
    assert cur.fetchone()[0] == len(props)
    cur.execute("SELECT count(*) FROM property_merge_events "
                "WHERE listing_ref_id = ANY(%s) AND undone_at IS NULL", (adverts,))
    assert cur.fetchone()[0] == 0


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
    assert (out["outcome"], out["left_property_id"]) == ("split_native", pid)
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
    """Each pair's NEWEST ruling by `by` — the ruling every reader obeys since migration 574
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

    # The adapter's negative read ITSELF (autodedup/apply_sql.py PAIR_VERDICTS_SQL): any
    # decider, the newest ruling per pair, a negative verdict, both sides in the candidate set.
    # The earlier veto on (s2, a1) was written down as its `different` before the detach's
    # word (E920), so that pair has two negative rows and is read once, as its newest.
    from autodedup import apply_sql

    cur.execute(apply_sql.PAIR_VERDICTS_SQL,
                {"negatives": list(usql.NEGATIVE_VERDICTS), "listing_ids": ids})
    assert sorted((int(lo), int(hi)) for lo, hi, _v in cur.fetchall()) == sorted([card, veto])
    cur.execute("SELECT verdict, note, decided_by FROM autodedup.verdicts WHERE kind = 'pair' "
                "AND listing_lo = %s AND listing_hi = %s ORDER BY decided_at, id", veto)
    assert cur.fetchall()[0] == ("different", "earlier", "operator"), (
        "the bare veto's word is kept in the history before the detach's ruling")


def _as_at_560(cur: Any) -> None:
    """Migration 560's one-time copy, as it ran: its conflict target is the per-decider pair
    index of 528, which 574 dropped (the store is a ledger since), so the replay recreates that
    index inside the test's transaction (rolled back with it) before running the statement."""
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS autodedup_verdicts_pair_uidx ON autodedup.verdicts "
                "(kind, listing_lo, listing_hi, decided_by) WHERE kind = 'pair'")


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
    detach_listings(cur.connection, [u1], decided_by=OP, source="autodedup")
    cur.execute("UPDATE property_merge_events SET source = 'operator' "
                "WHERE merge_group_id = ANY(%s::uuid[])", ([old, gone],))

    _as_at_560(cur)
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
    _located(cur, *ids)
    for pid in (subject_p, two_portal, single):
        _recompute(cur, pid)
    sql, params = build_query(
        TargetSpec(lat=49.01, lng=17.91, area_m2=70, exclude_listing_ids=[subject]),
        ComparableFilters(radius_m=500, category_main="byt", category_type="prodej"))
    cur.execute(sql, params)
    assert {int(r[0]) for r in cur.fetchall()} & ids == {canon, alone}


# --- the merge sprint's rollup (migration 588) ------------------------------------------


def _ranked(cur: Any, pid: int) -> list[int]:
    cur.execute("SELECT listing_id FROM property_canonical_listings(%s) ORDER BY canonical_rank",
                (pid,))
    return [int(r[0]) for r in cur.fetchall()]


def _recomputed_canonical(cur: Any, pid: int) -> int:
    _recompute(cur, pid)
    cur.execute("SELECT repr_listing_ref_id FROM properties WHERE id = %s", (pid,))
    return int(cur.fetchone()[0])


def test_an_active_advert_speaks_by_its_map_point_then_its_first_sighting(cur):
    """MS5, active adverts: a map point, then the earliest first sighting, then trust; ended last."""
    pid = _property(cur)
    unlocated, later = _advert(cur, pid, source="sreality"), _advert(cur, pid, source="idnes")
    earliest = _advert(cur, pid, source="bazos")
    ended = _advert(cur, pid, source="sreality", active=False)
    _located(cur, later, earliest, ended)
    for lid, first, last in ((unlocated, 30, 0), (later, 10, 0), (earliest, 20, 0), (ended, 60, 40)):
        _seen(cur, lid, first, last)
    assert _ranked(cur, pid) == [earliest, later, unlocated, ended]
    assert _recomputed_canonical(cur, pid) == earliest


def test_with_no_active_advert_the_latest_ended_advert_with_a_map_point_speaks(cur):
    """MS5, every advert ended: a map point, then the latest last sighting; not first seen or trust."""
    pid = _property(cur)
    recent = _advert(cur, pid, source="idnes", active=False)
    older = _advert(cur, pid, source="sreality", active=False)
    unlocated = _advert(cur, pid, source="bazos", active=False)
    _located(cur, recent, older)
    for lid, first, last in ((recent, 40, 2), (older, 20, 10), (unlocated, 15, 1)):
        _seen(cur, lid, first, last)
    assert _ranked(cur, pid) == [recent, older, unlocated]
    assert _recomputed_canonical(cur, pid) == recent


def test_a_live_adverts_sighting_never_moves_the_canonical_advert(cur):
    """561's moving key is gone: two live adverts first seen together keep their order (trust,
    then id) when the other is seen again, and `repr_since` is not restamped."""
    pid = _property(cur)
    canon, other = _advert(cur, pid, source="sreality"), _advert(cur, pid, source="idnes")
    _located(cur, canon, other)
    for lid in (canon, other):
        _seen(cur, lid, 10, 1)

    def speaker() -> tuple[Any, ...]:
        _recompute(cur, pid)
        cur.execute("SELECT repr_listing_ref_id, repr_since::text FROM properties WHERE id = %s",
                    (pid,))
        return cur.fetchone()

    before = speaker()
    _seen(cur, other, 10, 0)
    assert speaker() == before and before[0] == canon


@pytest.mark.parametrize(("canonical", "ended", "union"), [
    (False, True, True), (True, False, True), (None, True, True),
    (False, None, False), (False, False, False), (None, None, None)])
def test_the_six_amenities_are_a_union(cur, canonical, ended, union):
    """MS6: yes if any advert (an ended one too) says yes, no if one says no, else unknown."""
    amenities = ("has_lift", "has_balcony", "has_parking", "terrace", "garage", "cellar")
    pid = _property(cur)
    canon = _advert(cur, pid, source="sreality")
    other = _advert(cur, pid, source="idnes", active=False)
    for lid, value in ((canon, canonical), (other, ended)):
        cur.execute("UPDATE listings SET " + ", ".join(f"{c} = %s" for c in amenities)
                    + " WHERE id = %s", (*[value] * len(amenities), lid))
    assert _recomputed_canonical(cur, pid) == canon
    cur.execute(f"SELECT {', '.join(amenities)} FROM properties WHERE id = %s", (pid,))
    assert cur.fetchone() == (union,) * len(amenities)


def test_the_portal_lists_and_newest_ad_dates_follow_every_advert(cur):
    """MS19: both portal lists and each portal's newest-advert date follow a merge and a detach."""
    from toolkit.filter_registry import PORTAL_OPTIONS

    p, q = _property(cur), _property(cur)
    old_idnes = _advert(cur, p, source="idnes")
    new_idnes = _advert(cur, p, source="idnes", active=False)
    sreality, bazos = _advert(cur, p, source="sreality"), _advert(cur, q, source="bazos")
    for lid, first, last in ((old_idnes, 20, 0), (new_idnes, 5, 2), (sreality, 10, 0), (bazos, 3, 0)):
        _seen(cur, lid, first, last)
    for pid in (p, q):
        _recompute(cur, pid)
    cur.execute("SELECT id, first_seen_at FROM listings WHERE id = ANY(%s)",
                ([new_idnes, sreality, bazos],))
    first = {int(lid): at for lid, at in cur.fetchall()}
    dates = {"idnes": first[new_idnes], "sreality": first[sreality]}

    def portals() -> tuple[Any, ...]:
        cur.execute("SELECT all_sources, active_sources, "
                    + ", ".join(f"newest_ad_at_{o.value}" for o in PORTAL_OPTIONS)
                    + " FROM properties WHERE id = %s", (p,))
        row = cur.fetchone()
        return row[0], row[1], {o.value: d for o, d in zip(PORTAL_OPTIONS, row[2:]) if d}

    assert portals() == (["idnes", "sreality"], ["idnes", "sreality"], dates)
    _merge(cur, [p, q])
    assert portals() == (["bazos", "idnes", "sreality"], ["bazos", "idnes", "sreality"],
                         {**dates, "bazos": first[bazos]})
    assert _detach(cur, bazos)["outcome"] == "detached"
    cur.execute("UPDATE listings SET is_active = false WHERE property_id = %s", (p,))
    _recompute(cur, p)
    assert portals() == (["idnes", "sreality"], [], dates)


def test_a_same_portal_relist_counts_its_predecessors_steps_and_one_handover(cur):
    """MS10: a predecessor's own cut plus one handover step count; alerts quote the canonical's own."""
    from api.notifications import _recent_price_drops

    pid = _property(cur)
    before = _advert(cur, pid, source="sreality", price=5_200_000, active=False)
    relist = _advert(cur, pid, source="sreality", price=4_900_000)
    _seen(cur, before, 40, 10)
    _seen(cur, relist, 5)
    _snapshot(cur, before, 5_300_000, 30 * 24)
    _snapshot(cur, before, 5_200_000, 20 * 24)
    _snapshot(cur, relist, 5_000_000, 4 * 24)
    cut = _snapshot(cur, relist, 4_900_000, 24)
    assert _recomputed_canonical(cur, pid) == relist
    cur.execute(
        "SELECT price_drop_count, price_rise_count, price_change_count, price_change_count_30d, "
        "       round(max_price_drop_pct, 2), round(total_price_change_pct, 2) "
        "FROM properties WHERE id = %s", (pid,))
    assert cur.fetchone() == (3, 0, 3, 3, Decimal("3.85"), Decimal("-7.55"))
    drops = [d for d in _recent_price_drops(cur.connection, window_days=40) if d[0] == pid]
    assert drops == [(pid, cut, 4_900_000, 5_000_000)]


@pytest.mark.parametrize(("chain", "figures"), [
    pytest.param([(60, 40, [(5_500_000, 50)]), (30, 10, [(5_300_000, 25), (5_200_000, 15)]),
                  (5, 0, [(5_000_000, 4)])], (3, 3, 3, 3, Decimal("-9.09")), id="three-adverts"),
    pytest.param([(40, 10, [(5_300_000, 30), (5_200_000, 20)]),
                  (5, 0, [(5_200_000, 4), (5_000_000, 1)])], (2, 2, 2, 2, Decimal("-5.66")),
                 id="same-price-relist"),
    pytest.param([(40, 10, [(5_300_000, 30)]), (5, 0, [(5_000_000, 4)])],
                 (1, 1, 1, 1, Decimal("-5.66")), id="one-priced-snapshot-each"),
    pytest.param([(100, 60, [(5_300_000, 80)]), (50, 0, [(5_000_000, 45), (4_900_000, 1)])],
                 (2, 2, 1, 2, Decimal("-7.55")), id="handover-before-the-30-days"),
    pytest.param([(40, 5, [(5_300_000, 30)]), (5, 0, [(5_000_000, 4), (4_900_000, 1)])],
                 (1, 1, 1, 1, Decimal("-2.00")), id="ended-as-the-next-began"),
])
def test_the_lineage_walks_every_predecessor_and_dates_each_handover(cur, chain, figures):
    """MS10 on one portal, oldest advert first as (first seen, last seen, [(price, at)]) in days
    ago, the last one live: every predecessor counts, a handover is a step only when the price
    moves, dated at the newer advert's first price, and the total runs from the oldest priced
    advert; one that ended the instant the next began ran beside it. Figures: drops, changes,
    30- and 90-day changes, total."""
    pid = _property(cur)
    for i, (first, last, prices) in enumerate(chain):
        lid = _advert(cur, pid, source="sreality", price=prices[-1][0], active=i == len(chain) - 1)
        _seen(cur, lid, first, last)
        for price, days in prices:
            _snapshot(cur, lid, price, days * 24)
    assert _recomputed_canonical(cur, pid) == lid
    cur.execute("SELECT price_drop_count, price_change_count, price_change_count_30d, "
                "price_change_count_90d, round(total_price_change_pct, 2) "
                "FROM properties WHERE id = %s", (pid,))
    assert cur.fetchone() == figures


def test_adverts_that_ran_at_the_same_time_never_form_a_step(cur):
    """A same-portal advert that overlapped the canonical one, or another portal's, adds no step."""
    pid = _property(cur)
    canon = _advert(cur, pid, source="sreality", price=4_900_000)
    overlapping = _advert(cur, pid, source="sreality", price=5_500_000, active=False)
    other_portal = _advert(cur, pid, source="idnes", price=5_800_000, active=False)
    for lid, first, last in ((canon, 5, 0), (overlapping, 20, 3), (other_portal, 30, 10)):
        _seen(cur, lid, first, last)
    for lid, older, newer, at in ((canon, 5_000_000, 4_900_000, 4),
                                  (overlapping, 5_600_000, 5_500_000, 15),
                                  (other_portal, 6_000_000, 5_800_000, 25)):
        _snapshot(cur, lid, older, at * 24)
        _snapshot(cur, lid, newer, (at - 3) * 24)
    assert _recomputed_canonical(cur, pid) == canon
    cur.execute("SELECT price_drop_count, price_change_count, round(max_price_drop_pct, 2), "
                "round(total_price_change_pct, 2) FROM properties WHERE id = %s", (pid,))
    assert cur.fetchone() == (1, 1, Decimal("2.00"), Decimal("-2.00"))


def test_a_canonical_change_clears_the_city_figure_stamp(cur):
    """The city stamp survives a kept canonical advert, clears on a takeover and when older than it."""
    pid = _property(cur)
    first = _advert(cur, pid, source="sreality")
    _recompute(cur, pid)

    def city(stamp_sql: str | None = None) -> tuple[int, bool]:
        if stamp_sql:
            cur.execute(f"UPDATE properties SET city_proximity_computed_at = {stamp_sql} "
                        "WHERE id = %s", (pid,))
        _recompute(cur, pid)
        cur.execute("SELECT repr_listing_ref_id, city_proximity_computed_at IS NOT NULL "
                    "FROM properties WHERE id = %s", (pid,))
        return cur.fetchone()

    assert city("now() - interval '1 hour'") == (first, True)
    takeover = _advert(cur, pid, source="idnes")
    _located(cur, takeover)
    assert city() == (takeover, False)
    assert city("now()") == (takeover, True)
    assert city("repr_since - interval '1 minute'") == (takeover, False)
