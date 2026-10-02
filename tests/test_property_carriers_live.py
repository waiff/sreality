"""Every property-anchored operator-state row follows a merge and a detach, executed through the
public writers (`merge_property_set`, `detach_listings`) against the replayed schema: one test
per carrier, each over two accounts, each proving the retired property is left holding nothing;
then the live census — every foreign key to `properties` and every `%property_id%` column of the
replayed schema is carried (`PROPERTY_CARRIERS`) or named (`NOT_CARRIED`); then the after-step
(`properties_changed`: rollup, Browse row, broker queue) both writers run; then a set detach's
rulings. Runs in CI's migrations job with DB_RAILS_REQUIRED=1; every test rolls back."""

from __future__ import annotations

import uuid
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
from toolkit.property_carriers import NOT_CARRIED, carried_columns
from toolkit.property_identity import detach_listings, merge_property_set

pytestmark = REQUIRED_DB

# The current-state tables a merge re-points (rule 18, 22, migration 536). The two ledgers it
# also writes, property_pipeline_events and asset_membership_events, name the retired property
# by design: a detach replays them.
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
    # Not `assert`: a broken setup fails as setup, never as the behavior the test asserts.
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
    cur.execute("SELECT count(*) FROM properties WHERE id = %s AND asset_id IS NOT NULL", (pid,))
    out["properties.asset_id"] = int(cur.fetchone()[0])
    return out


def _merged(cur: Any, survivor: int, *retired: int, source: str = "operator") -> str:
    """Merge the set through the public writer; the orphan probe runs on each retired side."""
    out = merge_property_set(cur.connection, [survivor, *retired], source=source,
                             reason="manual_subset", decided_by=OP)["data"]
    _require((out["survivor_id"], out["retired_ids"]) == (survivor, list(retired)),
             f"the seed order did not pick the survivor: {out}")
    for pid in retired:
        left = _left_on(cur, pid)
        _require(not any(left.values()), f"rows left on the merged_away property {pid}: {left}")
    return str(out["merge_group_id"])


def _detached(cur: Any, listing_id: int, origin: int, *, source: str = "operator",
              group: str | None = None) -> None:
    (out,) = detach_listings(cur.connection, [listing_id], decided_by=OP, source=source,
                             merge_group_id=group)["data"]["adverts"]
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
        cur.execute("INSERT INTO collection_properties (account_id, collection_id, property_id) "
                    "VALUES (%s, %s, %s)", (acc, coll, pid))
        cur.execute("INSERT INTO property_tags (account_id, property_id, tag_id) "
                    "VALUES (%s, %s, %s)", (acc, pid, tag))
    union = {"collection_properties": [(a, ca, s), (b, cb, s)],
             "property_tags": [(a, ta, s), (b, tb, s)]}

    _merged(cur, s, r)
    assert _memberships(cur, [ca, cb], [ta, tb]) == union

    _detached(cur, advert, r)
    assert _memberships(cur, [ca, cb], [ta, tb]) == union, "a detach moved curation back"


def test_notes_all_move(cur, accounts):
    s, r, _s_advert, advert = _pair(cur)
    notes: dict[int, uuid.UUID] = {}
    for acc in accounts:
        for pid in (s, r):
            cur.execute("INSERT INTO property_notes (account_id, property_id, body) "
                        "VALUES (%s, %s, %s) RETURNING id", (acc, pid, f"lp {uuid.uuid4()}"))
            notes[int(cur.fetchone()[0])] = acc

    def placed() -> dict[int, tuple[uuid.UUID, int]]:
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
    cur.execute("INSERT INTO property_pipeline (account_id, property_id, stage_id) "
                "VALUES (%s, %s, %s)", (acc, pid, stage))


def _added(cur: Any, acc: uuid.UUID, pid: int, stage: int) -> None:
    """A card as `api.pipeline.add_card` writes it: the card plus its logged add."""
    _card(cur, acc, pid, stage)
    cur.execute("INSERT INTO property_pipeline_events (property_id, to_stage_id, reason, "
                "account_id) VALUES (%s, %s, 'operator', %s)", (pid, stage, acc))


def _removed(cur: Any, acc: uuid.UUID, pid: int) -> None:
    """A card as `api.pipeline.remove_card` ends it: gone, its stage logged."""
    cur.execute("DELETE FROM property_pipeline WHERE account_id = %s AND property_id = %s "
                "RETURNING stage_id", (acc, pid))
    (stage,) = cur.fetchone()
    cur.execute("INSERT INTO property_pipeline_events (property_id, from_stage_id, reason, "
                "account_id) VALUES (%s, %s, 'operator', %s)", (pid, stage, acc))


def _cards(cur: Any, acc: uuid.UUID) -> dict[int, int]:
    cur.execute("SELECT property_id, stage_id FROM property_pipeline WHERE account_id = %s",
                (acc,))
    return {int(pid): int(stage) for pid, stage in cur.fetchall()}


def test_pipeline_terminal_aware_merge_and_lossless_detach(cur, accounts):
    """A's card moves over; B's live card on R beats B's closed card on S, though the closed
    stage sits further right. The detach gives R both cards back, logged as `unmerge_restore`,
    and takes A's off S (move-if-empty); B's survivor card stays (best-effort)."""
    a, b = accounts
    s, r, _s_advert, advert = _pair(cur)
    live_a = _stage(cur, a, position=2)
    live_b, closed_b = _stage(cur, b, position=2), _stage(cur, b, position=5, terminal=True)
    _card(cur, a, r, live_a)
    _card(cur, b, s, closed_b)
    _card(cur, b, r, live_b)

    group = _merged(cur, s, r)
    assert _cards(cur, a) == {s: live_a}
    assert _cards(cur, b) == {s: live_b}, "a closed card buried a live deal"
    cur.execute("SELECT account_id, property_id, to_stage_id FROM property_pipeline_events "
                "WHERE merge_group_id = %s::uuid AND reason = 'merge_absorb'", (group,))
    assert sorted(cur.fetchall()) == sorted([(a, r, live_a), (b, s, closed_b), (b, r, live_b)])

    _detached(cur, advert, r)
    assert _cards(cur, a) == {r: live_a}
    assert _cards(cur, b) == {s: live_b, r: live_b}
    cur.execute("SELECT account_id, property_id, to_stage_id FROM property_pipeline_events "
                "WHERE merge_group_id = %s::uuid AND reason = 'unmerge_restore'", (group,))
    assert sorted(cur.fetchall()) == sorted([(a, r, live_a), (b, r, live_b)])


def _chain(cur: Any, *sources: str) -> tuple[list[int], dict[int, int]]:
    """Properties C < A < B (< D ...), one advert each, recomputed so the lower id survives."""
    pids = [_property(cur) for _ in sources]
    adverts = {pid: _advert(cur, pid, source=src) for pid, src in zip(pids, sources)}
    for pid in pids:
        _recompute(cur, pid)
    return pids, adverts


def test_a_chained_detach_restores_one_card(cur, accounts):
    """X's card followed B's advert A then C; neither survivor held its own, so the detach gives
    it back to B and takes it off C. Y's card on B met Y's own on C at the second hop (snapshotted
    under C there, not at the first): both of Y's cards stay."""
    x, y = accounts
    (c, a, b), adverts = _chain(cur, "sreality", "idnes", "remax")
    live_x, live_y = _stage(cur, x, position=2), _stage(cur, y, position=2)
    _card(cur, x, b, live_x)
    _card(cur, y, b, live_y)
    _card(cur, y, c, live_y)

    _merged(cur, a, b)
    _merged(cur, c, a)
    _require(_cards(cur, x) == {c: live_x}, "the card did not follow both merges")
    _detached(cur, adverts[b], b)
    assert _cards(cur, x) == {b: live_x}, "one deal, two cards"
    assert _cards(cur, y) == {b: live_y, c: live_y}, "a path survivor's own card was dropped"


def test_a_side_merge_keeps_the_card_it_joined(cur, accounts):
    """After the chain, C absorbs D, which held X's card: C's card now carries D's deal too, so
    the detach gives B its card back and leaves C's. Y's card followed the chain alone (D held
    none of Y's), so it comes off C: one card per deal, and no deal dropped."""
    x, y = accounts
    (c, a, b, d), adverts = _chain(cur, "sreality", "idnes", "remax", "bazos")
    live_x, ahead_x = _stage(cur, x, position=2), _stage(cur, x, position=3)
    live_y = _stage(cur, y, position=2)
    _card(cur, x, b, live_x)
    _card(cur, y, b, live_y)
    _card(cur, x, d, ahead_x)

    _merged(cur, a, b)
    _merged(cur, c, a)
    _merged(cur, c, d)
    _require(_cards(cur, x) == {c: ahead_x}, "D's card did not meet the chain's on C")
    _detached(cur, adverts[b], b)
    assert _cards(cur, x) == {b: live_x, c: ahead_x}, "the detach dropped D's deal"
    assert _cards(cur, y) == {b: live_y}, "one deal, two cards"


@pytest.mark.parametrize("emptied_by", ["detach", "remove"])
def test_a_side_merge_onto_an_emptied_survivor_keeps_its_deal(cur, accounts, emptied_by):
    """The chain's card leaves C (A's own advert detached, which gives A the card back, or the
    operator removes it), then C absorbs D, which held X's card: it lands on the emptied C, so
    C holds no snapshot of its own there. The detach of B's advert gives B its card back and
    leaves D's deal on C: the side merge is read off the merge ledger (D into C, still
    standing), not off C's card. The card that went back to A followed B's advert, so it comes
    off A, for Y too: one card per deal."""
    x, y = accounts
    (c, a, b, d), adverts = _chain(cur, "sreality", "idnes", "remax", "bazos")
    live_x, ahead_x = _stage(cur, x, position=2), _stage(cur, x, position=3)
    live_y = _stage(cur, y, position=2)
    _card(cur, x, b, live_x)
    _card(cur, y, b, live_y)
    _card(cur, x, d, ahead_x)

    _merged(cur, a, b)
    _merged(cur, c, a)
    if emptied_by == "detach":
        _detached(cur, adverts[a], a)
        _require(_cards(cur, x) == {a: live_x} and _cards(cur, y) == {a: live_y},
                 "A's detach did not give A the chain's card")
    else:
        cur.execute("DELETE FROM property_pipeline WHERE account_id = %s AND property_id = %s",
                    (x, c))
    _require(c not in _cards(cur, x), "the chain's card did not leave C")
    _merged(cur, c, d)
    _require(_cards(cur, x).get(c) == ahead_x, "D's card did not land on the emptied C")
    _detached(cur, adverts[b], b)
    assert _cards(cur, x) == {b: live_x, c: ahead_x}, "D's deal dropped, or B's on two cards"
    assert _cards(cur, y) == {b: live_y}, "one deal, two cards"


def test_a_one_hop_detach_keeps_the_card_a_standing_side_merge_joined(cur, accounts):
    """S absorbs R's card (S held none), then D's: S's card carries D's deal while D stays
    merged, so detaching R's advert gives R its card back and leaves S's. Before the side-merge
    keep, the one-hop drop took S's card and D's deal with it."""
    x, y = accounts
    (s, r, d), adverts = _chain(cur, "sreality", "idnes", "remax")
    live_x, ahead_x = _stage(cur, x, position=2), _stage(cur, x, position=3)
    live_y = _stage(cur, y, position=2)
    _card(cur, x, r, live_x)
    _card(cur, x, d, ahead_x)
    _card(cur, y, r, live_y)

    _merged(cur, s, r)
    _merged(cur, s, d)
    _detached(cur, adverts[r], r)
    assert _cards(cur, x) == {r: live_x, s: ahead_x}, "the detach dropped D's deal"
    assert _cards(cur, y) == {r: live_y}, "one deal, two cards"


@pytest.mark.parametrize("order", ["newest first", "oldest first", "older group first"])
def test_an_undone_side_merge_keeps_no_card(cur, accounts, order):
    """S absorbs R's card (S held none), then D's. Newest first (an operator splitting D off
    then R): D's detach gives D its card back and S keeps the one it held at that merge; R's
    then finds the side merge undone, so S's card goes. Oldest first (the operator's split: ONE
    detach_listings over its movers in listing-id order; or the engine's unapply of the older
    group first): R's detach keeps S's card, which carries D's deal while D stands merged; D's
    then finds S's card at its merge was R's, which filled the empty S and has since gone home
    (R's restore is logged), so it goes too. Either way the full revert leaves no card on S."""
    x, y = accounts
    (s, r, d), adverts = _chain(cur, "sreality", "idnes", "remax")
    live_x, ahead_x = _stage(cur, x, position=2), _stage(cur, x, position=3)
    live_y = _stage(cur, y, position=2)
    _card(cur, x, r, live_x)
    _card(cur, x, d, ahead_x)
    _card(cur, y, d, live_y)

    g1 = _merged(cur, s, r)
    g2 = _merged(cur, s, d)
    if order == "newest first":
        _detached(cur, adverts[d], d)
        _require(_cards(cur, x) == {s: ahead_x, d: ahead_x}, "D's detach did not restore D's card")
        _detached(cur, adverts[r], r)
    elif order == "oldest first":
        _require(adverts[r] < adverts[d], "the split's listing-id order is not R's advert first")
        out = detach_listings(cur.connection, [adverts[r], adverts[d]],
                              decided_by=OP)["data"]["adverts"]
        _require([(o["outcome"], o["restored_property_id"]) for o in out]
                 == [("detached", r), ("detached", d)], f"the split did not bring both back: {out}")
    else:
        _detached(cur, adverts[r], r, group=g1)
        _require(_cards(cur, x) == {s: ahead_x, r: live_x}, "R's detach dropped D's deal")
        _detached(cur, adverts[d], d, group=g2)
    assert _cards(cur, x) == {r: live_x, d: ahead_x}, "an undone side merge kept S's card"
    assert _cards(cur, y) == {d: live_y}, "one deal, two cards"


def test_a_new_deal_on_the_survivor_outlives_the_merge_that_first_filled_it(cur, accounts):
    """S took R's card empty-handed and lost it again with R's detach; the operator then starts a
    new deal on S (the API logs the add) and S absorbs D's card. D's detach keeps S's card: the
    add since R's merge makes it S's own, though the merge that first filled S is undone."""
    x, _y = accounts
    (s, r, d), adverts = _chain(cur, "sreality", "idnes", "remax")
    live_x, ahead_x, new_x = (_stage(cur, x, position=p) for p in (2, 3, 4))
    _card(cur, x, r, live_x)
    _card(cur, x, d, ahead_x)

    _merged(cur, s, r)
    _detached(cur, adverts[r], r)
    _require(_cards(cur, x) == {r: live_x, d: ahead_x}, "R's detach left a card on S")
    _added(cur, x, s, new_x)
    _merged(cur, s, d)
    _detached(cur, adverts[d], d)
    assert _cards(cur, x) == {s: new_x, r: live_x, d: ahead_x}, "S's new deal was dropped"


def test_a_side_merge_undone_in_part_keeps_no_card(cur, accounts):
    """D absorbed E, then S absorbs R's card and D's. Detaching D's own advert brings D back
    with its card while E's advert stays on S, so a ledger row of that side merge is still
    live; the side merge stands only while D is merged into S, so R's detach takes S's card
    and the later return of E's advert changes no card."""
    x, y = accounts
    (s, r, d, e), adverts = _chain(cur, "sreality", "idnes", "remax", "bazos")
    live_x, ahead_x = _stage(cur, x, position=2), _stage(cur, x, position=3)
    live_y = _stage(cur, y, position=2)
    _card(cur, x, r, live_x)
    _card(cur, x, d, ahead_x)
    _card(cur, y, r, live_y)

    _merged(cur, d, e)
    _merged(cur, s, r)
    _merged(cur, s, d)
    _detached(cur, adverts[d], d)
    _require(_cards(cur, x) == {s: ahead_x, d: ahead_x}, "D's detach did not restore D's card")
    _detached(cur, adverts[r], r)
    assert _cards(cur, x) == {r: live_x, d: ahead_x}, "a side merge undone in part kept S's card"
    assert _cards(cur, y) == {r: live_y}, "one deal, two cards"
    _detached(cur, adverts[e], e)
    assert (_cards(cur, x), _cards(cur, y)) == ({r: live_x, d: ahead_x}, {r: live_y})


def test_an_origin_merged_twice_gets_one_card(cur, accounts):
    """B (two adverts) merges into A, comes back with one, moves on a stage and merges into A
    again, its card landing on the emptied A. The second advert's detach undoes the FIRST merge
    and brings B back: the card that filled A at the second merge was B's, and B has its card
    back (nothing merged into B since), so A's card goes and B's deal is on one card, at its
    first merge's stage (the restore is the pre-merge stage). Y's card on A, which B never held,
    stays."""
    x, y = accounts
    (a, b), adverts = _chain(cur, "sreality", "idnes")
    second = _advert(cur, b, source="remax")
    _recompute(cur, b)
    live_x, ahead_x = _stage(cur, x, position=2), _stage(cur, x, position=3)
    live_y = _stage(cur, y, position=2)
    _card(cur, x, b, live_x)
    _card(cur, y, a, live_y)

    _merged(cur, a, b)
    _detached(cur, adverts[b], b)
    _require(_cards(cur, x) == {b: live_x}, "the first detach did not give B its card back")
    cur.execute("UPDATE property_pipeline SET stage_id = %s WHERE account_id = %s "
                "AND property_id = %s", (ahead_x, x, b))
    _merged(cur, a, b)
    _require(_cards(cur, x) == {a: ahead_x}, "B's card did not land on the emptied A")
    _detached(cur, second, b)
    assert _cards(cur, x) == {b: live_x}, "one deal, two cards"
    assert _cards(cur, y) == {a: live_y}, "a card B never held was dropped"


def test_an_origin_merged_back_with_a_deal_it_took_in_keeps_that_deal(cur, accounts):
    """R (two adverts) merges into S, comes back with one (its card back, S's comes off), absorbs
    D's card and merges into S again, filling the emptied S. The second advert's detach undoes
    the FIRST merge, so R's card comes back from before it took D's in: S keeps its card, the
    only one carrying D's deal (D still stands merged into R). One hop, as the engine's unapply."""
    x, _y = accounts
    (s, r, d), adverts = _chain(cur, "sreality", "idnes", "remax")
    second = _advert(cur, r, source="bazos")
    _recompute(cur, r)
    live_x, ahead_x = _stage(cur, x, position=2), _stage(cur, x, position=3)
    _card(cur, x, r, live_x)
    _card(cur, x, d, ahead_x)

    _merged(cur, s, r)
    _detached(cur, adverts[r], r)
    _require(_cards(cur, x) == {r: live_x, d: ahead_x}, "R's detach did not take its card off S")
    _merged(cur, r, d)
    _merged(cur, s, r)
    _require(_cards(cur, x) == {s: ahead_x}, "R's card did not fill the emptied S")
    _detached(cur, second, r)
    assert _cards(cur, x) == {s: ahead_x, r: live_x}, "D's deal dropped with S's card"


@pytest.mark.parametrize("a_moves_on", [False, True], ids=["A stays", "A merges into Z"])
def test_a_chain_cut_in_the_middle_drops_the_copy_where_it_went_back(cur, accounts, a_moves_on):
    """A's own advert is detached first: A gets the chain's card back and C's comes off. The
    operator then starts a new deal on C (the API logs the add). B's detach takes the card off A,
    where the one that followed B's advert went back, and leaves C's new deal: one card per deal,
    none dropped. If A has since merged into Z, off B's undo path, the detach finds C instead and
    still leaves its deal (added since the card arrived there); B's deal stays on Z and B, which
    no detach of B's advert can reach (best-effort)."""
    x, y = accounts
    (z, c, a, b), adverts = _chain(cur, "bazos", "sreality", "idnes", "remax")
    live_x, ahead_x = _stage(cur, x, position=2), _stage(cur, x, position=3)
    live_y = _stage(cur, y, position=2)
    _card(cur, x, b, live_x)
    _card(cur, y, b, live_y)

    _merged(cur, a, b)
    _merged(cur, c, a)
    _detached(cur, adverts[a], a)
    _require((_cards(cur, x), _cards(cur, y)) == ({a: live_x}, {a: live_y}),
             "A's detach did not give A the chain's card")
    _added(cur, x, c, ahead_x)
    if a_moves_on:
        _merged(cur, z, a)
    _detached(cur, adverts[b], b)
    on_z = {z} if a_moves_on else set()
    assert _cards(cur, x) == {c: ahead_x, b: live_x, **dict.fromkeys(on_z, live_x)}, (
        "C's new deal dropped, or B's on two cards")
    assert _cards(cur, y) == {b: live_y, **dict.fromkeys(on_z, live_y)}, "one deal, two cards"


def test_a_new_deal_on_the_reactivated_middle_stays(cur, accounts):
    """A's own advert is detached first: A gets the chain's card back and C's comes off. The
    operator ends that deal on A and starts a new one there (through the API, which logs both).
    B's detach gives B its card back and leaves A's new deal: an add since the card came back to
    A marks another deal. Y's card, untouched, comes off A."""
    x, y = accounts
    (c, a, b), adverts = _chain(cur, "sreality", "idnes", "remax")
    live_x, ahead_x = _stage(cur, x, position=2), _stage(cur, x, position=3)
    live_y = _stage(cur, y, position=2)
    _card(cur, x, b, live_x)
    _card(cur, y, b, live_y)

    _merged(cur, a, b)
    _merged(cur, c, a)
    _detached(cur, adverts[a], a)
    _require((_cards(cur, x), _cards(cur, y)) == ({a: live_x}, {a: live_y}),
             "A's detach did not give A the chain's card")
    _removed(cur, x, a)
    _added(cur, x, a, ahead_x)
    _detached(cur, adverts[b], b)
    assert _cards(cur, x) == {a: ahead_x, b: live_x}, "A's new deal dropped"
    assert _cards(cur, y) == {b: live_y}, "one deal, two cards"


def test_a_set_merge_counts_its_survivor_card_only_from_before_it(cur, accounts):
    """S absorbs R1 then R2 in one set merge; S held no card, so its snapshot at R2's step is
    R1's card, not its own. R1's detach takes Y's card off S (only R1 held one) and leaves X's,
    which carries R2's deal while R2 stays merged; R2's detach then takes X's off S too."""
    x, y = accounts
    (s, r1, r2), adverts = _chain(cur, "sreality", "idnes", "remax")
    live_x, ahead_x = _stage(cur, x, position=2), _stage(cur, x, position=3)
    live_y = _stage(cur, y, position=2)
    _card(cur, x, r1, live_x)
    _card(cur, x, r2, ahead_x)
    _card(cur, y, r1, live_y)

    _merged(cur, s, r1, r2)
    _require((_cards(cur, x), _cards(cur, y)) == ({s: ahead_x}, {s: live_y}),
             "the set merge did not keep the most advanced card on S")
    _detached(cur, adverts[r1], r1)
    assert _cards(cur, x) == {s: ahead_x, r1: live_x}, "the detach dropped R2's deal"
    assert _cards(cur, y) == {r1: live_y}, "one deal, two cards"
    _detached(cur, adverts[r2], r2)
    assert _cards(cur, x) == {r1: live_x, r2: ahead_x}, "one deal, two cards"


# --- dismissals -------------------------------------------------------------------------


def test_dismissals_lift_never_delete_and_follow_the_pipeline(cur, accounts):
    """A's dismissal of R is lifted 'merge' where A's of S stands; B's dismissal of S is lifted
    'pipeline' by the live card the pipeline carry just put there, so Pipeline ran first."""
    a, b = accounts
    s, r, _s_advert, advert = _pair(cur)
    live_b = _stage(cur, b, position=2)
    _card(cur, b, r, live_b)
    ids = {}
    for key, acc, pid in (("a_s", a, s), ("a_r", a, r), ("b_s", b, s)):
        cur.execute("INSERT INTO property_dismissals (account_id, property_id) VALUES (%s, %s) "
                    "RETURNING id", (acc, pid))
        ids[key] = int(cur.fetchone()[0])

    def rows() -> dict[int, tuple[uuid.UUID, int, str | None]]:
        cur.execute("SELECT id, account_id, property_id, lift_reason FROM property_dismissals "
                    "WHERE account_id = ANY(%s)", (list(accounts),))
        return {int(i): (acc, int(pid), why) for i, acc, pid, why in cur.fetchall()}

    after = {ids["a_s"]: (a, s, None), ids["a_r"]: (a, s, "merge"),
             ids["b_s"]: (b, s, "pipeline")}

    _merged(cur, s, r)
    assert _cards(cur, b) == {s: live_b}
    assert rows() == after, "a dismissal was deleted, or not lifted, or not moved"

    _detached(cur, advert, r)
    assert rows() == after, "a detach moved a dismissal back"


# --- the asset link ---------------------------------------------------------------------


@pytest.mark.parametrize(("source", "ledger"), [("operator", "operator"), ("autodedup", "auto")])
def test_the_asset_link_follows_and_comes_back(cur, source, ledger):
    s, r, _s_advert, advert = _pair(cur)
    # The asset row and its link as toolkit/asset_identity.py writes them.
    cur.execute("INSERT INTO assets (note, created_by) VALUES (%s, %s) RETURNING id", ("lp", OP))
    asset = int(cur.fetchone()[0])
    cur.execute("UPDATE properties SET asset_id = %s WHERE id = %s", (asset, r))
    cur.execute(
        "INSERT INTO asset_membership_events "
        "    (asset_id, property_id, action, reason, source, confidence, created_by) "
        "VALUES (%s, %s, 'linked', 'lp seed', 'operator', NULL, %s)", (asset, r, OP))

    def links() -> dict[int, int | None]:
        cur.execute("SELECT id, asset_id FROM properties WHERE id = ANY(%s)", ([s, r],))
        return {int(pid): aid for pid, aid in cur.fetchall()}

    def logged(reason: str) -> list[tuple[int, str, str]]:
        cur.execute("SELECT property_id, action, source FROM asset_membership_events "
                    "WHERE asset_id = %s AND reason = %s ORDER BY property_id", (asset, reason))
        return [(int(pid), action, src) for pid, action, src in cur.fetchall()]

    group = _merged(cur, s, r, source=source)
    assert links() == {s: asset, r: None}
    assert logged(f"merge {group}") == [(s, "linked", ledger), (r, "unlinked", ledger)]

    _detached(cur, advert, r, source=source)
    assert links() == {s: None, r: asset}
    assert logged(f"detach {group}") == [(s, "unlinked", ledger), (r, "linked", ledger)]


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
