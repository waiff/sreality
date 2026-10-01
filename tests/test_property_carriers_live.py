"""Every property-anchored row follows a merge and a detach, executed through the public
writers (`merge_property_set`, `detach_listing`) against the replayed schema: one test per
carrier, each over two accounts, each proving the retired property is left holding nothing.
Runs in CI's migrations job with DB_RAILS_REQUIRED=1; every test rolls back."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
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
from toolkit.property_identity import detach_listing, merge_property_set

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
    # Not `assert`: a strict xfail(raises=AssertionError) must not swallow a broken setup.
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


def _merged(cur: Any, survivor: int, retired: int, *, source: str = "operator") -> str:
    """Merge the pair through the public writer; the orphan probe runs on the retired side."""
    out = merge_property_set(cur.connection, [survivor, retired], source=source,
                             reason="manual_subset", decided_by=OP)["data"]
    _require((out["survivor_id"], out["retired_ids"]) == (survivor, [retired]),
             f"the seed order did not pick the survivor: {out}")
    left = _left_on(cur, retired)
    _require(not any(left.values()), f"rows left on the merged_away property {retired}: {left}")
    return str(out["merge_group_id"])


def _detached(cur: Any, listing_id: int, origin: int, *, source: str = "operator") -> None:
    out = detach_listing(cur.connection, listing_id, decided_by=OP, source=source)["data"]
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


@pytest.mark.xfail(strict=True, raises=psycopg.errors.CheckViolation,
                   reason="PR 4: the collapse DELETE nulls the send's notification_id "
                          "(channel_sends_check) and the whole merge aborts")
def test_a_delivered_alert_does_not_abort_the_merge(cur, accounts):
    seed = _seed_dispatches(cur, accounts)
    (r_new, _key), (s_new, _s_key) = seed["r_new"], seed["s_new"]
    # Columns as api/channel_client.py claims a send; the outbox's consumer/source/dedupe shape.
    cur.execute(
        "INSERT INTO channel_sends "
        "  (consumer, notification_id, outreach_message_id, source_kind, source_id, "
        "   channel, recipient, category, status, dedupe_key) "
        "VALUES ('collection_monitor', %s, NULL, 'collection_monitor', %s, 'email', %s, "
        "        'transactional', 'sent', %s) RETURNING id",
        (r_new, str(seed["colls"][0]), "ci@replay.local", f"notif:{r_new}:email"),
    )
    send = int(cur.fetchone()[0])

    _merged(cur, seed["s"], seed["r"])
    cur.execute("SELECT notification_id FROM channel_sends WHERE id = %s", (send,))
    assert cur.fetchone()[0] == s_new, "the delivery record lost its event"


# --- the deal pipeline ------------------------------------------------------------------


def _card(cur: Any, acc: uuid.UUID, pid: int, stage: int) -> None:
    cur.execute("INSERT INTO property_pipeline (account_id, property_id, stage_id) "
                "VALUES (%s, %s, %s)", (acc, pid, stage))


def _cards(cur: Any, acc: uuid.UUID) -> dict[int, int]:
    cur.execute("SELECT property_id, stage_id FROM property_pipeline WHERE account_id = %s",
                (acc,))
    return {int(pid): int(stage) for pid, stage in cur.fetchall()}


def test_pipeline_terminal_aware_merge_and_lossless_detach(cur, accounts):
    """A's card moves over; B's live card on R beats B's closed card on S, though the closed
    stage sits further right. The detach gives R both cards back and takes A's off S
    (move-if-empty); B's survivor card stays (best-effort)."""
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


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="PR 5: a chained detach restores the card on B and leaves the one "
                          "the last survivor absorbed")
def test_a_chained_detach_restores_one_card(cur, accounts):
    x, y = accounts
    c, a, b = _property(cur), _property(cur), _property(cur)
    adverts = {pid: _advert(cur, pid, source=src)
               for pid, src in ((c, "sreality"), (a, "idnes"), (b, "remax"))}
    for pid in (c, a, b):
        _recompute(cur, pid)
    live_x, live_y = _stage(cur, x, position=2), _stage(cur, y, position=2)
    _card(cur, x, b, live_x)
    _card(cur, y, c, live_y)

    _merged(cur, a, b)
    _merged(cur, c, a)
    _require(_cards(cur, x) == {c: live_x}, "the card did not follow both merges")
    _detached(cur, adverts[b], b)
    assert _cards(cur, x) == {b: live_x}, "one deal, two cards"
    assert _cards(cur, y) == {c: live_y}


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
