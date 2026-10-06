"""Every property-anchored operator-state row follows a merge, executed through the public writer
(`merge_property_set`) against the replayed schema: one test per carrier, each over two accounts,
each proving the retired property is left holding nothing, every merge checked against MS14's
count invariant over the carry record (`_merged`), and a detach moving nothing back; the receipt
and the brake's dry run over that record; then the live census — every foreign key to
`properties` and every `%property_id%` column of the replayed schema is carried
(`PROPERTY_CARRIERS`) or named (`NOT_CARRIED`); then the after-step (`properties_changed`:
rollup, Browse row, broker queue) both writers run; then a set detach's rulings. Runs in CI's
migrations job with DB_RAILS_REQUIRED=1; every test rolls back."""

from __future__ import annotations

import itertools
import uuid
from collections import Counter
from typing import Any

import pytest

from tests._live_property import (  # `cur` is the fixture
    OP,
    REQUIRED_DB,
    _account,
    _advert,
    _collection,
    _property,
    _recompute,
    _snapshot,
    _stage,
    _tag,
    cur,
)
from toolkit.property_carriers import NOT_CARRIED, carried_columns, curation_preview
from toolkit.property_identity import detach_listings, merge_property_set

pytestmark = REQUIRED_DB

# The current-state tables a merge re-points (rule 18, 22, migration 536).
_CARRIED = (
    "collection_properties",
    "property_tags",
    "property_notes",
    "notification_dispatches",
    "property_pipeline",
    "property_dismissals",
)


@pytest.fixture(autouse=True)
def accounts(cur: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """Two tenants per test, so every carry is shown partitioned by account (B9)."""
    return _account(cur), _account(cur)


def _require(ok: bool, why: str) -> None:
    # Not `assert`: a seed that did not take is a broken test, not a failed claim.
    if not ok:
        pytest.fail(why)


def _pair(cur: Any) -> tuple[int, int, int, int]:
    """(S, R, S's advert, R's advert), recomputed: first_seen_at ties, so S (lower id) survives."""
    s, r = _property(cur), _property(cur)
    s_advert, r_advert = _advert(cur, s, source="sreality"), _advert(cur, r, source="idnes")
    _recompute(cur, s)
    _recompute(cur, r)
    return s, r, s_advert, r_advert


def _left_on(cur: Any, pid: int) -> dict[str, int]:
    out = {}
    for table in _CARRIED:
        cur.execute(f"SELECT count(*) FROM {table} WHERE property_id = %s", (pid,))
        out[table] = int(cur.fetchone()[0])
    return out


# A curation row's identity in the carry record: (table, row key, own timestamp, account); the
# key of a pipeline card is its account and property.
_IDENTITY = {
    "property_notes": ("id", "created_at"),
    "property_pipeline": ("NULL::bigint", "added_at"),
    "collection_properties": ("collection_id", "added_at"),
    "property_tags": ("tag_id", "attached_at"),
    "property_dismissals": ("id", "dismissed_at"),
}
_FOLD_DELETES = ("property_pipeline", "collection_properties", "property_tags")
_AGO = itertools.count(1)  # now() is one instant per transaction: seeded rows get their own


def _curation(cur: Any, pids: list[int]) -> dict[tuple, tuple[int, bool, dict[str, Any]]]:
    """Every curation row on these properties: identity -> (property, lifted, the row as JSON)."""
    out = {}
    for table, (key, at) in _IDENTITY.items():
        lifted = "lifted_at IS NOT NULL" if table == "property_dismissals" else "false"
        cur.execute(f"SELECT {key}, {at}, account_id, property_id, {lifted}, to_jsonb({table}) "
                    f"FROM {table} WHERE property_id = ANY(%s)", (pids,))
        out |= {(table, k, t, acc): (int(pid), bool(gone), row)
                for k, t, acc, pid, gone, row in cur}
    return out


def _unplaced(row: dict[str, Any]) -> dict[str, Any]:
    """A row without the property it sat on, which an earlier step of the merge may have moved."""
    return {k: v for k, v in row.items() if k != "property_id"}


def _census(cur: Any) -> Counter:
    """Each account's rows per curation table, lifted dismissals included."""
    out: Counter = Counter()
    for table in _IDENTITY:
        cur.execute(f"SELECT account_id, count(*) FROM {table} GROUP BY 1")
        out.update({(table, acc): int(n) for acc, n in cur.fetchall()})
    return out


def _carries(cur: Any, group: str) -> list[tuple]:
    """The merge's carry rows in write order: table, key, at, account, from, to, kind, snapshot."""
    cur.execute("SELECT table_name, row_key, row_at, account_id, from_property_id, "
                "to_property_id, kind, snapshot FROM property_merge_carries "
                "WHERE merge_group_id = %s::uuid ORDER BY id", (group,))
    return cur.fetchall()


def _carried_whole(cur: Any, group: str, survivor: int, retired: tuple[int, ...],
                   before: dict[tuple, tuple[int, bool, dict[str, Any]]], census: Counter) -> None:
    """MS14 over one merge: each account's count per table holds but for one fewer per folded
    card, collection entry or tag; each retired row has carry rows, each fold is one row deleted
    or lifted, once, its snapshot the row as it stood before the merge; every carry row names the
    survivor and the row's own timestamp, a snapshot exactly when folded, never an alert event."""
    carries = _carries(cur, group)
    assert {c[5] for c in carries} <= {survivor}, "a carry row names another survivor"
    assert all((c[6] == "folded") == (c[7] is not None) for c in carries)
    assert {c[0] for c in carries} <= set(_IDENTITY), "an alert event entered the carry record"
    folded = [tuple(c[:4]) for c in carries if c[6] == "folded"]
    assert len(folded) == len(set(folded)), "a row folded twice"
    after = _curation(cur, [survivor])
    ended = {row for row, (_pid, lifted, _row) in before.items()
             if row not in after or (after[row][1] and not lifted)}
    assert set(folded) == ended, "a fold without its deletion or lift, or the other way"
    assert all(_unplaced(c[7]) == _unplaced(before[tuple(c[:4])][2])
               for c in carries if c[6] == "folded"), "a fold's snapshot is not its row before"
    assert {row for row, (pid, _l, _r) in before.items() if pid in retired} == {
        tuple(c[:4]) for c in carries if c[4] in retired}, "a retired row with no carry row"
    deleted = Counter((t, acc) for t, _k, _at, acc in folded if t in _FOLD_DELETES)
    now = _census(cur)
    keys = now.keys() | census.keys()
    assert {k: now[k] for k in keys} == {k: census[k] - deleted[k] for k in keys}, (
        "a curation count moved by more than its folds")


def _merged(cur: Any, survivor: int, *retired: int) -> str:
    """Merge the set through the public writer: the orphan probe runs on the retired side and
    MS14's invariant over the whole merge (`_carried_whole`)."""
    before, census = _curation(cur, [survivor, *retired]), _census(cur)
    out = merge_property_set(cur.connection, [survivor, *retired], source="operator",
                             reason="manual_subset", decided_by=OP)["data"]
    _require((out["survivor_id"], out["retired_ids"]) == (survivor, list(retired)),
             f"the seed order did not pick the survivor: {out}")
    for pid in retired:
        left = _left_on(cur, pid)
        _require(not any(left.values()), f"rows left on the merged_away property {pid}: {left}")
    group = str(out["merge_group_id"])
    _carried_whole(cur, group, survivor, retired, before, census)
    return group


def _carried(cur: Any, group: str) -> list[tuple]:
    """(table, account, kind, from) per carry row, sorted for comparison."""
    return sorted(((t, acc, kind, int(frm)) for t, _k, _at, acc, frm, _to, kind, _s
                   in _carries(cur, group)), key=str)


def _detached(cur: Any, listing_id: int, origin: int) -> None:
    (out,) = detach_listings(cur.connection, [listing_id], decided_by=OP)["data"]["adverts"]
    _require((out["outcome"], out["restored_property_id"], out["reactivated"])
             == ("detached", origin, True), f"the detach did not bring {origin} back: {out}")


# --- curation tables (SET and APPEND) ---------------------------------------------------


def _memberships(cur: Any, colls: list[int], tags: list[int]) -> dict[str, list[tuple]]:
    cur.execute("SELECT account_id, collection_id, property_id FROM collection_properties "
                "WHERE collection_id = ANY(%s) ORDER BY collection_id, property_id", (colls,))
    in_colls = cur.fetchall()
    cur.execute("SELECT account_id, tag_id, property_id FROM property_tags "
                "WHERE tag_id = ANY(%s) ORDER BY tag_id, property_id", (tags,))
    return {"collection_properties": in_colls, "property_tags": cur.fetchall()}


def test_collections_and_tags_union_onto_the_survivor_per_account(cur, accounts):
    a, b = accounts
    s, r, _s_advert, advert = _pair(cur)
    ca, cb, ta, tb = _collection(cur, a), _collection(cur, b), _tag(cur, a), _tag(cur, b)
    for acc, coll, tag, pid in ((a, ca, ta, s), (a, ca, ta, r), (b, cb, tb, r)):
        cur.execute("INSERT INTO collection_properties (account_id, collection_id, property_id, "
                    "added_at) VALUES (%s, %s, %s, now() - make_interval(secs => %s))",
                    (acc, coll, pid, next(_AGO)))
        cur.execute("INSERT INTO property_tags (account_id, property_id, tag_id, attached_at) "
                    "VALUES (%s, %s, %s, now() - make_interval(secs => %s))",
                    (acc, pid, tag, next(_AGO)))
    union = {"collection_properties": [(a, ca, s), (b, cb, s)],
             "property_tags": [(a, ta, s), (b, tb, s)]}

    _merged(cur, s, r)
    assert _memberships(cur, [ca, cb], [ta, tb]) == union

    _detached(cur, advert, r)
    assert _memberships(cur, [ca, cb], [ta, tb]) == union, "a detach moved curation back"


def test_notes_all_move(cur, accounts):
    """Each account's, and one with no account, which moves as it is."""
    a, b = accounts
    s, r, _s_advert, advert = _pair(cur)
    notes: dict[int, uuid.UUID | None] = {}
    for acc, pid in ((a, s), (a, r), (b, s), (b, r), (None, r)):
        cur.execute("INSERT INTO property_notes (account_id, property_id, body) "
                    "VALUES (%s, %s, %s) RETURNING id", (acc, pid, f"lp {uuid.uuid4()}"))
        notes[int(cur.fetchone()[0])] = acc

    def placed() -> dict[int, tuple[uuid.UUID | None, int]]:
        cur.execute("SELECT id, account_id, property_id FROM property_notes WHERE id = ANY(%s)",
                    (list(notes),))
        return {int(nid): (acc, int(pid)) for nid, acc, pid in cur.fetchall()}

    _merged(cur, s, r)
    assert placed() == {nid: (acc, s) for nid, acc in notes.items()}

    _detached(cur, advert, r)
    assert placed() == {nid: (acc, s) for nid, acc in notes.items()}


# --- notification dispatches ------------------------------------------------------------


def _dispatch(cur: Any, coll: int, pid: int, listing_id: int, kind: str, *,
              snapshot: int | None = None, price: int | None = None,
              prev: int | None = None) -> tuple[uuid.UUID, str]:
    """A collection_monitor event, columns as the producer writes them (api/notifications.py)."""
    cur.execute(
        "INSERT INTO notification_dispatches "
        "  (source_kind, collection_id, property_id, sreality_id, listing_id, change_kind, "
        "   status, target_channels, trigger_snapshot_id, trigger_price_czk, "
        "   prev_price_czk, dedupe_key) "
        "VALUES ('collection_monitor', %s, %s, NULL, %s, %s, 'sent', %s::text[], %s, %s, %s, %s) "
        "RETURNING id, dedupe_key",
        (coll, pid, listing_id, kind, ["email"], snapshot, price, prev,
         f"cm:{coll}:{kind}:lp-{uuid.uuid4()}"),
    )
    nid, key = cur.fetchone()
    return nid, str(key)


def _seed_dispatches(cur: Any, accounts: tuple[uuid.UUID, uuid.UUID]) -> dict[str, Any]:
    """A's collection alerted 'new' on S and on R plus a price drop on R; B's 'new' on R only."""
    a, b = accounts
    s, r, s_advert, r_advert = _pair(cur)
    ca, cb = _collection(cur, a), _collection(cur, b)
    drop = _snapshot(cur, r_advert, 4_900_000, 1)
    return {
        "s": s, "r": r, "advert": r_advert, "colls": [ca, cb],
        "s_new": _dispatch(cur, ca, s, s_advert, "new"),
        "r_new": _dispatch(cur, ca, r, r_advert, "new"),
        "r_drop": _dispatch(cur, ca, r, r_advert, "price_drop", snapshot=drop,
                            price=4_900_000, prev=5_000_000),
        "b_new": _dispatch(cur, cb, r, r_advert, "new"),
    }


def _dispatches(cur: Any, colls: list[int]) -> dict[uuid.UUID, tuple[int, str]]:
    cur.execute("SELECT id, property_id, dedupe_key FROM notification_dispatches "
                "WHERE collection_id = ANY(%s)", (colls,))
    return {nid: (int(pid), key) for nid, pid, key in cur.fetchall()}


def test_dispatches_collapse_null_safe(cur, accounts):
    """The two 'new' rows of one collection collapse (subscription and snapshot both NULL); the
    price drop and the other account's row move, every dedupe_key as it was written."""
    seed = _seed_dispatches(cur, accounts)
    s, (s_new, s_key), (r_drop, drop_key), (b_new, b_key) = (
        seed["s"], seed["s_new"], seed["r_drop"], seed["b_new"])
    kept = {s_new: (s, s_key), r_drop: (s, drop_key), b_new: (s, b_key)}

    _merged(cur, s, seed["r"])
    assert _dispatches(cur, seed["colls"]) == kept

    _detached(cur, seed["advert"], seed["r"])
    assert _dispatches(cur, seed["colls"]) == kept


def _send(cur: Any, nid: uuid.UUID, coll: int) -> int:
    """A delivered email, columns as api/channel_client.py claims a send (the outbox's
    consumer/source/dedupe shape)."""
    cur.execute(
        "INSERT INTO channel_sends "
        "  (consumer, notification_id, outreach_message_id, source_kind, source_id, "
        "   channel, recipient, category, status, dedupe_key) "
        "VALUES ('collection_monitor', %s, NULL, 'collection_monitor', %s, 'email', %s, "
        "        'transactional', 'sent', %s) RETURNING id",
        (nid, str(coll), "ci@replay.local", f"notif:{nid}:email"),
    )
    return int(cur.fetchone()[0])


def test_a_delivered_alert_does_not_abort_the_merge(cur, accounts):
    """The collapse DELETE would null the send's notification_id (ON DELETE SET NULL), which
    channel_sends_check refuses, aborting the merge: the send moves to S's twin first. A send
    on a row that moves (the price drop) keeps pointing at it."""
    seed = _seed_dispatches(cur, accounts)
    (r_new, _key), (s_new, _s_key), (r_drop, _d_key) = (
        seed["r_new"], seed["s_new"], seed["r_drop"])
    sends = {_send(cur, r_new, seed["colls"][0]): s_new,
             _send(cur, r_drop, seed["colls"][0]): r_drop}

    _merged(cur, seed["s"], seed["r"])
    cur.execute("SELECT id, notification_id FROM channel_sends WHERE id = ANY(%s)", (list(sends),))
    assert dict(cur.fetchall()) == sends, "a delivery record lost its event"


# --- the deal pipeline ------------------------------------------------------------------


def _card(cur: Any, acc: uuid.UUID, pid: int, stage: int) -> None:
    cur.execute("INSERT INTO property_pipeline (account_id, property_id, stage_id, added_at) "
                "VALUES (%s, %s, %s, now() - make_interval(secs => %s))",
                (acc, pid, stage, next(_AGO)))


def _cards(cur: Any, acc: uuid.UUID) -> dict[int, int]:
    cur.execute("SELECT property_id, stage_id FROM property_pipeline WHERE account_id = %s",
                (acc,))
    return {int(pid): int(stage) for pid, stage in cur.fetchall()}


def _added(cur: Any, acc: uuid.UUID) -> dict[int, Any]:
    cur.execute("SELECT property_id, added_at FROM property_pipeline WHERE account_id = %s",
                (acc,))
    return {int(pid): at for pid, at in cur.fetchall()}


def test_pipeline_terminal_aware_merge_folds_the_losing_card(cur, accounts):
    """A's card on S, further along, stays and A's card on R folds; B's live card on R beats B's
    closed card on S, though the closed stage sits further right: S's folds and R's moves as
    itself, its own dates kept. Each fold is in the carry record; no pipeline history row is
    written; a detach moves no card back (W4)."""
    a, b = accounts
    s, r, _s_advert, advert = _pair(cur)
    near_a, far_a = _stage(cur, a, position=2), _stage(cur, a, position=3)
    live_b, closed_b = _stage(cur, b, position=2), _stage(cur, b, position=5, terminal=True)
    _card(cur, a, s, far_a)
    _card(cur, a, r, near_a)
    _card(cur, b, s, closed_b)
    _card(cur, b, r, live_b)
    winner = _added(cur, b)[r]

    group = _merged(cur, s, r)
    assert _cards(cur, a) == {s: far_a}
    assert _cards(cur, b) == {s: live_b}, "a closed card buried a live deal"
    assert _added(cur, b) == {s: winner}
    assert _carried(cur, group) == sorted([
        ("property_pipeline", a, "folded", r), ("property_pipeline", b, "folded", s),
        ("property_pipeline", b, "moved", r)], key=str)
    cur.execute("SELECT count(*) FROM property_pipeline_events WHERE merge_group_id = %s::uuid",
                (group,))
    assert cur.fetchone()[0] == 0

    _detached(cur, advert, r)
    assert (_cards(cur, a), _cards(cur, b)) == ({s: far_a}, {s: live_b})


def test_a_card_a_later_step_overwrites_folds_from_where_it_came(cur, accounts):
    """{S, R1, R2} is one merge in two steps. A's closed card on R1 moves to S in the first; in
    the second R2's live card wins, so the card on S folds from R1, not from S, and lifts A's own
    dismissal of S, folded from S though B's dismissal of R1 (lifted before the merge, it stays
    lifted) moved to S in the first step. B's note on R2 moves."""
    a, b = accounts
    s, r1, r2 = _property(cur), _property(cur), _property(cur)
    for pid, source in ((s, "sreality"), (r1, "idnes"), (r2, "remax")):
        _advert(cur, pid, source=source)
        _recompute(cur, pid)
    closed, live = _stage(cur, a, position=2, terminal=True), _stage(cur, a, position=3)
    _card(cur, a, r1, closed)
    _card(cur, a, r2, live)
    cur.execute("INSERT INTO property_notes (account_id, property_id, body) "
                "VALUES (%s, %s, 'lp') RETURNING id", (b, r2))
    note = int(cur.fetchone()[0])
    cur.execute("INSERT INTO property_dismissals (account_id, property_id, lifted_at, lift_reason) "
                "VALUES (%s, %s, now(), 'operator') RETURNING id", (b, r1))
    dismissal = int(cur.fetchone()[0])
    cur.execute("INSERT INTO property_dismissals (account_id, property_id) VALUES (%s, %s) "
                "RETURNING id", (a, s))
    own = int(cur.fetchone()[0])

    group = _merged(cur, s, r1, r2)
    assert _cards(cur, a) == {s: live}
    assert [(t, k, acc, kind, frm) for t, k, _at, acc, frm, _to, kind, _s
            in _carries(cur, group)] == [
        ("property_pipeline", None, a, "moved", r1),
        ("property_dismissals", dismissal, b, "moved", r1),
        ("property_notes", note, b, "moved", r2),
        ("property_pipeline", None, a, "folded", r1),
        ("property_pipeline", None, a, "moved", r2),
        ("property_dismissals", own, a, "folded", s)]


def test_a_fold_names_only_its_own_standing_carry_row(cur, accounts):
    """{S, R1, R1x}: A's and B's cards come from R1, C's closed card and dismissal from R1x. A
    then removes its card and adds it again, dated like B's, and R1x's ad is split off (until W4
    nothing goes back with it). In {S, R2} each card and the dismissal folded on S names S: A's
    new card is neither the row A's carry row names nor B's, and C's carry rows belong to a
    merge step the split undid (`pipeline_identity.STANDING_CARRY`)."""
    a, b = accounts
    c = _account(cur)
    s, r1, r1x, r2 = (_property(cur) for _ in range(4))
    ads = {pid: _advert(cur, pid, source=source) for pid, source in
           ((s, "sreality"), (r1, "idnes"), (r1x, "remax"), (r2, "bazos"))}
    for pid in ads:
        _recompute(cur, pid)
    _card(cur, a, r1, _stage(cur, a, position=1))
    _card(cur, b, r1, _stage(cur, b, position=1))
    _card(cur, c, r1x, _stage(cur, c, position=9, terminal=True))
    cur.execute("INSERT INTO property_dismissals (account_id, property_id) VALUES (%s, %s)",
                (c, r1x))
    _merged(cur, s, r1, r1x)
    cur.execute("DELETE FROM property_pipeline WHERE account_id = %s AND property_id = %s "
                "RETURNING stage_id", (a, s))
    cur.execute("INSERT INTO property_pipeline (account_id, property_id, stage_id, added_at) "
                "SELECT %s, %s, %s, added_at FROM property_pipeline "
                "WHERE account_id = %s AND property_id = %s", (a, s, cur.fetchone()[0], b, s))
    _detached(cur, ads[r1x], r1x)
    _card(cur, a, r2, _stage(cur, a, position=2))
    _card(cur, c, r2, _stage(cur, c, position=1))

    group = _merged(cur, s, r2)
    assert _carried(cur, group) == sorted([
        ("property_pipeline", a, "folded", s), ("property_pipeline", a, "moved", r2),
        ("property_pipeline", c, "folded", s), ("property_pipeline", c, "moved", r2),
        ("property_dismissals", c, "folded", s)], key=str)


# --- dismissals -------------------------------------------------------------------------


def test_dismissals_lift_never_delete_and_follow_the_pipeline(cur, accounts):
    """A's dismissal of R is lifted 'merge' where A's of S stands; B's dismissal of S is lifted
    'pipeline' by the live card the pipeline carry just put there, so Pipeline ran first; C's of
    R moves and is lifted 'pipeline' by C's own card on S. Each lift is folded, from where the
    dismissal sat."""
    a, b = accounts
    c = _account(cur)
    s, r, _s_advert, advert = _pair(cur)
    _card(cur, b, r, _stage(cur, b, position=2))
    _card(cur, c, s, _stage(cur, c, position=2))
    ids = {}
    for key, acc, pid in (("a_s", a, s), ("a_r", a, r), ("b_s", b, s), ("c_r", c, r)):
        cur.execute("INSERT INTO property_dismissals (account_id, property_id) VALUES (%s, %s) "
                    "RETURNING id", (acc, pid))
        ids[key] = int(cur.fetchone()[0])

    def rows() -> dict[int, tuple[uuid.UUID, int, str | None]]:
        cur.execute("SELECT id, account_id, property_id, lift_reason FROM property_dismissals "
                    "WHERE account_id = ANY(%s)", ([a, b, c],))
        return {int(i): (acc, int(pid), why) for i, acc, pid, why in cur.fetchall()}

    after = {ids["a_s"]: (a, s, None), ids["a_r"]: (a, s, "merge"),
             ids["b_s"]: (b, s, "pipeline"), ids["c_r"]: (c, s, "pipeline")}

    group = _merged(cur, s, r)
    assert rows() == after, "a dismissal was deleted, or not lifted, or not moved"
    assert sorted((k, acc, kind, frm) for t, k, _at, acc, frm, _to, kind, _s
                  in _carries(cur, group) if t == "property_dismissals") == sorted([
        (ids["a_r"], a, "folded", r), (ids["b_s"], b, "folded", s),
        (ids["c_r"], c, "folded", r)])

    _detached(cur, advert, r)
    assert rows() == after, "a detach moved a dismissal back"


# --- what the operator and the brake read from the carry record ----------------------------


def _named(cur: Any, table: str, acc: uuid.UUID, name: str) -> int:
    """A collection or a tag (whose colour is a named-palette CHECK) the receipt names."""
    cur.execute(f"INSERT INTO {table} (account_id, name, color) VALUES (%s, %s, 'slate') "
                "RETURNING id" if table == "tags" else
                f"INSERT INTO {table} (account_id, name) VALUES (%s, %s) RETURNING id",
                (acc, name))
    return int(cur.fetchone()[0])


def test_the_receipt_names_only_the_acting_accounts_moved_items(cur, accounts):
    """MS15 and MS13 over the carry record: A's moved note, card stage, collection and tag by
    name, never the collection entry that folded, never B's (B's own stage on S included); B is
    told the survivor is hidden from it, A not, its dismissal lifted by its card; no account, no
    receipt."""
    from api.property_merge import merge_receipt

    a, b = accounts
    s, r, _s_advert, _advert = _pair(cur)
    brno, both = _named(cur, "collections", a, "Brno 2+kk"), _named(cur, "collections", a, "Oba")
    view, theirs = _named(cur, "tags", a, "výhled"), _named(cur, "collections", b, "Jejich")
    for acc, table, column, key, pid in ((a, "collection_properties", "collection_id", brno, r),
                                         (a, "collection_properties", "collection_id", both, r),
                                         (a, "collection_properties", "collection_id", both, s),
                                         (a, "property_tags", "tag_id", view, r),
                                         (b, "collection_properties", "collection_id", theirs, r)):
        at = _IDENTITY[table][1]
        cur.execute(f"INSERT INTO {table} (account_id, {column}, property_id, {at}) "
                    "VALUES (%s, %s, %s, now() - make_interval(secs => %s))",
                    (acc, key, pid, next(_AGO)))
    _card(cur, a, r, _stage(cur, a, position=2))
    _card(cur, b, s, _stage(cur, b, position=1, terminal=True))  # "lp 1" sorts before "lp 2"
    for _ in range(2):
        cur.execute("INSERT INTO property_notes (account_id, property_id, body) "
                    "VALUES (%s, %s, 'lp')", (a, r))
    cur.execute("INSERT INTO property_dismissals (account_id, property_id) "
                "VALUES (%s, %s), (%s, %s)", (a, s, b, s))  # A's is lifted by A's card coming

    merged = {"merge_group_id": _merged(cur, s, r), "survivor_id": s}
    assert merge_receipt(cur.connection, merged, a) == {"carried": {
        "notes": 2, "pipeline": "lp 2", "collections": ["Brno 2+kk"], "tags": ["výhled"]},
        "hidden_for_you": False}
    assert merge_receipt(cur.connection, merged, b) == {"carried": {
        "notes": 0, "pipeline": None, "collections": ["Jejich"], "tags": []},
        "hidden_for_you": True}
    assert merge_receipt(cur.connection, merged, None)["carried"]["collections"] == []


def test_the_brakes_dry_run_counts_the_carry_rows_its_undo_would_give_back(cur, accounts):
    """The standing carry rows whose from-property gets one of the ads back (MS17): R's two notes
    and live card, never S's own card the merge folded; not a carry a split undid, nor one whose
    ad already went back."""
    a, _b = accounts
    s, r, s_advert, r_advert = _pair(cur)
    for pid in (r, r, s):
        cur.execute("INSERT INTO property_notes (account_id, property_id, body) "
                    "VALUES (%s, %s, 'lp')", (a, pid))
    _card(cur, a, s, _stage(cur, a, position=5, terminal=True))
    _card(cur, a, r, _stage(cur, a, position=1))
    group = _merged(cur, s, r)
    assert curation_preview(cur.connection, group, [r_advert]) == {"carry_rows": 3}
    assert curation_preview(cur.connection, group, [s_advert]) == {"carry_rows": 0}
    cur.execute("UPDATE property_merge_carries SET undone_at = now() WHERE id = ("
                "SELECT min(id) FROM property_merge_carries WHERE merge_group_id = %s::uuid)",
                (group,))
    assert curation_preview(cur.connection, group, [r_advert, s_advert]) == {"carry_rows": 2}
    _detached(cur, r_advert, r)
    assert curation_preview(cur.connection, group, [r_advert]) == {"carry_rows": 0}


# --- the census, over the replayed schema ---------------------------------------------------

_HOW_TO_FIX = ("carry it (one `CurationTable(...)` line, or one `Carrier` class) or name it in "
               "`NOT_CARRIED` with the reason a merge leaves it: toolkit/property_carriers.py")

_FOREIGN_KEYS_SQL = """
SELECT c.conrelid::regclass::text, a.attname
FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
WHERE c.contype = 'f' AND c.confrelid = 'public.properties'::regclass
"""

# `*_next` tables exist only while a rebuild is in flight; other schemas (backups) are out.
_PROPERTY_COLUMNS_SQL = """
SELECT CASE WHEN c.table_schema = 'public' THEN c.table_name
            ELSE c.table_schema || '.' || c.table_name END, c.column_name
FROM information_schema.columns c
JOIN information_schema.tables t USING (table_schema, table_name)
WHERE t.table_type = 'BASE TABLE' AND c.table_schema IN ('public', 'autodedup')
  AND c.column_name LIKE '%property_id%' AND c.table_name NOT LIKE '%\\_next'
"""

_ALL_COLUMNS_SQL = """
SELECT CASE WHEN table_schema = 'public' THEN table_name
            ELSE table_schema || '.' || table_name END, column_name
FROM information_schema.columns WHERE table_schema IN ('public', 'autodedup')
"""


def _classified() -> set[tuple[str, str]]:
    return set(carried_columns()) | set(NOT_CARRIED)


def test_every_foreign_key_to_properties_is_classified(cur):
    cur.execute(_FOREIGN_KEYS_SQL)
    fks = {(str(t), str(c)) for t, c in cur.fetchall()}
    _require(("listings", "property_id") in fks, f"the FK query found no listings link: {fks}")
    unclassified = sorted(fks - _classified())
    assert not unclassified, f"unclassified foreign keys {unclassified}; {_HOW_TO_FIX}"


def test_every_property_id_column_is_classified(cur):
    cur.execute(_PROPERTY_COLUMNS_SQL)
    columns = {(str(t), str(c)) for t, c in cur.fetchall()}
    _require(("listings", "property_id") in columns, f"the column query is broken: {columns}")
    unclassified = sorted(columns - _classified())
    assert not unclassified, f"unclassified property columns {unclassified}; {_HOW_TO_FIX}"
    cur.execute(_ALL_COLUMNS_SQL)
    existing = {(str(t), str(c)) for t, c in cur.fetchall()}
    stale = sorted(_classified() - existing)
    assert not stale, f"carried or NOT_CARRIED names a column the schema no longer has: {stale}"


# --- the after-step, `properties_changed`, inside both writers ------------------------------


def _derived(cur: Any, pid: int) -> tuple[int | None, bool, bool]:
    """(source_count, listed in browse_list, present in browse_projection) for one property."""
    cur.execute("SELECT source_count FROM properties WHERE id = %s", (pid,))
    (count,) = cur.fetchone()
    cur.execute("SELECT EXISTS (SELECT 1 FROM browse_list WHERE property_id = %s), "
                "EXISTS (SELECT 1 FROM browse_projection WHERE property_id = %s)", (pid, pid))
    listed, projected = cur.fetchone()
    return count, bool(listed), bool(projected)


def _queued(cur: Any, listing_id: int) -> bool:
    cur.execute("SELECT EXISTS (SELECT 1 FROM dirty_broker_listings WHERE listing_id = %s)",
                (listing_id,))
    return bool(cur.fetchone()[0])


def _located(cur: Any, *listing_ids: int) -> None:
    """A resolved point per advert: `browse_projection` serves only a located property (rule 25),
    so without one Browse holds neither side and every Browse assertion is vacuous."""
    for lid in listing_ids:
        cur.execute(
            "INSERT INTO listing_location (listing_id, geom, match_confidence, granularity, "
            "  uncertainty_radius_m, country_status, resolver_version, claim_set_hash, "
            "  registry_version) VALUES (%s, ST_SetSRID(ST_MakePoint(17.91, 49.01), 4326), "
            "  'exact', 'building', 5, 'cz', 'test', '\\x00'::bytea, 'test')", (lid,))


def test_merge_and_detach_bring_derived_state_current(cur):
    """The rollup counts the moved advert (counts, not timestamps: now() is fixed inside the test
    transaction), Browse lists the survivor and never the retired property, and the
    broker-attributed advert is queued for the broker drain — after the merge, and again after
    the detach, which lists both properties again with the target's rollup recomputed."""
    s, r, s_advert, advert = _pair(cur)
    _located(cur, s_advert, advert)
    for pid in (s, r):
        _recompute(cur, pid)
    cur.execute("INSERT INTO broker_identities (source, source_broker_id_native, display_name) "
                "VALUES ('idnes', %s, 'lp') RETURNING id", (f"lp-{uuid.uuid4()}",))
    cur.execute("UPDATE listings SET broker_identity_id = %s WHERE id = %s",
                (int(cur.fetchone()[0]), advert))
    # R listed as the last rebuild left it, S not yet; nothing queued before the writer runs.
    cur.execute("INSERT INTO browse_list SELECT * FROM browse_projection WHERE property_id = %s",
                (r,))
    cur.execute("DELETE FROM dirty_broker_listings WHERE listing_id = %s", (advert,))
    _require(_derived(cur, s) == (1, False, True),
             f"S is not seeded as one projected, unlisted advert: {_derived(cur, s)}")
    _require(_derived(cur, r) == (1, True, True),
             f"R is not seeded as one projected, listed advert: {_derived(cur, r)}")

    _merged(cur, s, r)
    assert _derived(cur, s) == (2, True, True), "the survivor's rollup or Browse row is stale"
    assert not _derived(cur, r)[1], "Browse still lists the merged-away property"
    assert _queued(cur, advert), "the merge did not queue the attributed advert for brokers"

    # The merge's recompute skips R (no advert left), so R still holds its seeded 1: make it
    # stale (the column is NOT NULL), so a final 1 proves the detach recomputed its target.
    cur.execute("UPDATE properties SET source_count = 0 WHERE id = %s", (r,))
    cur.execute("DELETE FROM dirty_broker_listings WHERE listing_id = %s", (advert,))
    _detached(cur, advert, r)
    for pid in (s, r):
        assert _derived(cur, pid) == (1, True, True), f"property {pid} not brought current"
    assert _queued(cur, advert), "the detach did not queue the attributed advert for brokers"


# --- the set detach: rulings once, movers against the stayers ----------------------------------


def _ruled(cur: Any, ids: list[int]) -> list[tuple[int, int, str, str]]:
    """Every pair ruling among these adverts, oldest first: (lo, hi, verdict, note)."""
    cur.execute("SELECT listing_lo, listing_hi, verdict, note FROM autodedup.verdicts "
                "WHERE kind = 'pair' AND listing_lo = ANY(%(ids)s) AND listing_hi = ANY(%(ids)s) "
                "ORDER BY decided_at, id", {"ids": ids})
    return [(int(lo), int(hi), v, n) for lo, hi, v, n in cur.fetchall()]


def _source_count(cur: Any, pid: int) -> int:
    cur.execute("SELECT source_count FROM properties WHERE id = %s", (pid,))
    return int(cur.fetchone()[0])


def test_a_set_detach_rules_once_and_changes_once(cur):
    """S holds three adverts from two origins after the merge: its own and R's two. Detaching
    R's two in ONE call sends both home and reactivates R once; each is ruled `different` from
    S's advert only and never from the other (they sit together again), and every touched
    property's rollup counts its adverts. That the after-step runs once is the fake's
    (`db.changed`, tests/test_detach_listing.py)."""
    s, r = _property(cur), _property(cur)
    stay = _advert(cur, s, source="sreality")
    movers = [_advert(cur, r, source="idnes"), _advert(cur, r, source="remax")]
    for pid in (s, r):
        _recompute(cur, pid)
    _merged(cur, s, r)
    _require(_source_count(cur, s) == 3, "the merge did not bring S's rollup current")

    out = detach_listings(cur.connection, movers, decided_by=OP, reason="jiné patro")["data"]
    assert [(a["listing_id"], a["outcome"], a["left_property_id"], a["restored_property_id"],
             a["reactivated"]) for a in out["adverts"]] == [
        (movers[0], "detached", s, r, True), (movers[1], "detached", s, r, False)]
    note = f"operator detach from {s}: jiné patro"
    ruled = _ruled(cur, [stay, *movers])
    assert sorted((lo, hi, v) for lo, hi, v, n in ruled if n == note) == sorted(
        (*sorted((stay, m)), "different") for m in movers)
    assert out["rulings_written"] == 2
    assert not [row for row in ruled if {row[0], row[1]} == set(movers)], (
        "two adverts that moved together were ruled against each other")
    assert (_source_count(cur, s), _source_count(cur, r)) == (1, 2)
