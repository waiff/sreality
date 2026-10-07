"""The split by letters (MS18), executed: the W4 gate. Two accounts curate both sides of merged
properties; a split into two letters and one into three route every account's notes, cards,
collection entries, tags and dismissals by the rule (a note with its ad, the rest back where the
carry record says it came from, folds re-created while their twin stands) and the acting
account's choices (a moved note, copies), stamp the carry rows they spent, keep MS14's count
invariant per account and table (copies and re-created folds added, a letter's join folding), show
and answer nothing of the other account, and rule only "different", only across letters; a
letter's join takes back the "different" inside it (MS12). Then the staying letter's rule, a
merged ad born a new property, the digest's staleness and the routing's dismissal lift. Runs in
CI's migrations job (`TEST_DATABASE_URL`, DB_RAILS_REQUIRED=1); every test rolls back.
"""

from __future__ import annotations

import itertools
import uuid
from collections import Counter
from typing import Any

import pytest

from autodedup.incremental_sql import RT_MUST_LINK_SQL
from tests._live_property import (  # `cur` is the fixture
    OP,
    REQUIRED_DB,
    _account,
    _advert,
    _collection,
    _property,
    _recompute,
    _stage,
    _tag,
    cur,
)
from toolkit.property_identity import letter_landings, merge_property_set
from toolkit.property_split import SplitRefused, split_preview, split_property

pytestmark = REQUIRED_DB

_AGO = itertools.count(1)  # now() is one instant per transaction: seeded rows get their own
_TABLES = {"property_notes": "created_at", "property_pipeline": "added_at",
           "collection_properties": "added_at", "property_tags": "attached_at",
           "property_dismissals": "dismissed_at"}


@pytest.fixture()
def accounts(cur: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """The acting account A and another, B, whose items follow the rule unseen."""
    return _account(cur), _account(cur)


def _pair(x: int, y: int) -> tuple[int, int]:
    return (min(x, y), max(x, y))


def _seen(cur: Any, listing_id: int, days_ago: float) -> None:
    cur.execute("UPDATE listings SET first_seen_at = now() - make_interval(secs => %s) "
                "WHERE id = %s", (days_ago * 86400, listing_id))


def _props(cur: Any, *sources: str) -> tuple[list[int], list[int]]:
    """One property per advert, recomputed (so each has its first seen date and canonical ad)."""
    pids = [_property(cur) for _ in sources]
    ads = [_advert(cur, pid, source=src) for pid, src in zip(pids, sources)]
    return pids, ads


def _recomputed(cur: Any, *pids: int) -> None:
    for pid in pids:
        _recompute(cur, pid)


def _note(cur: Any, acc: uuid.UUID | None, pid: int, ad: int | None) -> int:
    cur.execute("INSERT INTO property_notes (account_id, property_id, body, origin_listing_ref_id, "
                "created_at) VALUES (%s, %s, %s, %s, now() - make_interval(secs => %s)) "
                "RETURNING id", (acc, pid, f"poznámka {uuid.uuid4().hex[:6]}", ad, next(_AGO)))
    return int(cur.fetchone()[0])


def _card(cur: Any, acc: uuid.UUID, pid: int, stage: int) -> None:
    cur.execute("INSERT INTO property_pipeline (account_id, property_id, stage_id, added_at) "
                "VALUES (%s, %s, %s, now() - make_interval(secs => %s))",
                (acc, pid, stage, next(_AGO)))


def _member(cur: Any, table: str, acc: uuid.UUID, key: int, pid: int) -> None:
    column = "collection_id" if table == "collection_properties" else "tag_id"
    cur.execute(f"INSERT INTO {table} (account_id, {column}, property_id, {_TABLES[table]}) "
                "VALUES (%s, %s, %s, now() - make_interval(secs => %s))",
                (acc, key, pid, next(_AGO)))


def _dismiss(cur: Any, acc: uuid.UUID, pid: int) -> int:
    cur.execute("INSERT INTO property_dismissals (account_id, property_id, dismissed_at) "
                "VALUES (%s, %s, now() - make_interval(secs => %s)) RETURNING id",
                (acc, pid, next(_AGO)))
    return int(cur.fetchone()[0])


def _census(cur: Any) -> Counter:
    """Each account's rows per curation table, lifted dismissals included (MS14)."""
    out: Counter = Counter()
    for table in _TABLES:
        cur.execute(f"SELECT account_id, count(*) FROM {table} GROUP BY 1")
        out.update({(table, acc): int(n) for acc, n in cur.fetchall()})
    return out


def _delta(before: Counter, after: Counter) -> Counter:
    return Counter({k: after[k] - before[k] for k in after.keys() | before.keys()
                    if after[k] != before[k]})


def _stranded(cur: Any) -> int:
    """Curation rows left on a merged-away property (MS14: never)."""
    total = 0
    for table in _TABLES:
        cur.execute(f"SELECT count(*) FROM {table} t JOIN properties p ON p.id = t.property_id "
                    "WHERE p.status = 'merged_away'")
        total += int(cur.fetchone()[0])
    return total


def _where(cur: Any, ids: list[int]) -> dict[int, int]:
    cur.execute("SELECT id, property_id FROM listings WHERE id = ANY(%s)", (ids,))
    return {int(a): int(b) for a, b in cur.fetchall()}


def _words(cur: Any, ids: list[int]) -> dict[tuple[int, int], tuple[str, str | None]]:
    """Each pair's ruling: its newest row, whoever wrote it (the ledger, migration 574)."""
    cur.execute(
        "SELECT DISTINCT ON (listing_lo, listing_hi) listing_lo, listing_hi, verdict, note "
        "FROM autodedup.verdicts WHERE kind = 'pair' AND listing_lo = ANY(%s) "
        "AND listing_hi = ANY(%s) ORDER BY listing_lo, listing_hi, decided_at DESC, id DESC",
        (ids, ids))
    return {(int(lo), int(hi)): (v, n) for lo, hi, v, n in cur.fetchall()}


def _vetoes(cur: Any, ids: list[int]) -> dict[tuple[int, int], str]:
    cur.execute(
        "SELECT listing_lo, listing_hi, source FROM autodedup.must_not_link "
        "WHERE listing_lo = ANY(%s) AND listing_hi = ANY(%s)", (ids, ids))
    return {(int(lo), int(hi)): src for lo, hi, src in cur.fetchall()}


def _folds(cur: Any, group: str) -> dict[tuple[str, Any, Any], int]:
    """(table, row key, account) -> carry id of each fold the merge wrote."""
    cur.execute("SELECT table_name, row_key, account_id, id FROM property_merge_carries "
                "WHERE merge_group_id = %s::uuid AND kind = 'folded'", (group,))
    return {(t, k, acc): int(i) for t, k, acc, i in cur.fetchall()}


def _spent(cur: Any, group: str) -> set[tuple[str, str, Any, Any]]:
    """(kind, table, row key, account) of the merge's carry rows a split stamped undone."""
    cur.execute("SELECT kind, table_name, row_key, account_id FROM property_merge_carries "
                "WHERE merge_group_id = %s::uuid AND undone_at IS NOT NULL", (group,))
    return {tuple(r) for r in cur.fetchall()}


def _rows(cur: Any, table: str, acc: uuid.UUID) -> list[tuple]:
    """(property, key, dated now) of the account's rows in a curation table."""
    key = {"property_notes": "id", "property_pipeline": "stage_id",
           "collection_properties": "collection_id", "property_tags": "tag_id",
           "property_dismissals": "lifted_at IS NULL"}[table]
    cur.execute(f"SELECT property_id, {key}, {_TABLES[table]} = now() FROM {table} "
                "WHERE account_id = %s ORDER BY 1, 2", (acc,))
    return [(int(pid), k, bool(fresh)) for pid, k, fresh in cur.fetchall()]


def _merge(cur: Any, ids: list[int], source: str = "operator") -> dict[str, Any]:
    return merge_property_set(cur.connection, ids, source=source, reason="manual_subset",
                              decided_by=OP)["data"]


def _split(cur: Any, pid: int, letters: dict[int, str], acc: uuid.UUID | None,
           **kw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    preview = split_preview(cur.connection, pid, letters=letters, account=acc)
    return preview, split_property(cur.connection, pid, letters=letters, decided_by=OP,
                                   account=acc, expect=preview["plan"], **kw)


def test_two_letters_route_both_accounts_curation_by_the_rule_and_the_acting_ones_choices(
        cur, accounts):
    a, b = accounts
    (s, r), (s1, r1) = _props(cur, "sreality", "idnes")
    _recomputed(cur, s, r)
    near_a, far_a, live_b = (_stage(cur, a, position=2), _stage(cur, a, position=3),
                             _stage(cur, b, position=2))
    ca, ta, tx, cb, tb = (_collection(cur, a), _tag(cur, a), _tag(cur, a), _collection(cur, b),
                          _tag(cur, b))
    note_as, note_ar, note_br = _note(cur, a, s, s1), _note(cur, a, r, r1), _note(cur, b, r, r1)
    _card(cur, a, s, near_a)
    _card(cur, a, r, far_a)                    # R's card is further along: S's folds, from S
    _card(cur, b, r, live_b)
    for table, acc, key, pid in (("collection_properties", a, ca, s),
                                 ("collection_properties", a, ca, r),
                                 ("property_tags", a, ta, r),
                                 ("property_tags", a, tx, s), ("property_tags", a, tx, r),
                                 ("collection_properties", b, cb, r),
                                 ("property_tags", b, tb, s), ("property_tags", b, tb, r)):
        _member(cur, table, acc, key, pid)
    lifted = _dismiss(cur, b, s)                # B's live card arrives: lifted, folded from S
    merged = _merge(cur, [s, r])
    group = merged["merge_group_id"]
    folds = _folds(cur, group)
    cur.execute("DELETE FROM property_tags WHERE property_id = %s AND tag_id = %s", (s, tx))
    census = _census(cur)

    picks = {f"note:{note_as}": ("B", ()), "pipeline": ("B", ("A",))}
    fold = {k[:2]: f"fold:{v}" for k, v in folds.items() if k[2] == a}
    picked = split_preview(cur.connection, s, letters={s1: "A", r1: "B"}, account=a,
                           choices=picks)
    preview, out = _split(cur, s, {s1: "A", r1: "B"}, a, reason="jiné patro", choices=picks)
    assert sorted((i["item"], i["letter"], i["why"], i.get("from_property_id"), i.get("skipped"))
                  for i in preview["curation"]) == sorted([
        (f"note:{note_as}", "A", "ad", None, None), (f"note:{note_ar}", "B", "ad", None, None),
        ("pipeline", "B", "came_from", r, None), (f"collection:{ca}", "A", "stays", None, None),
        (f"tag:{ta}", "B", "came_from", r, None),
        (fold[("property_pipeline", None)], "A", "fold", s, None),
        (fold[("collection_properties", ca)], "B", "fold", r, None),
        (fold[("property_tags", tx)], "B", "fold", r, "gone")]), "B's items are never shown"
    # with the picks, the preview says what the click then does: the copy takes the place of S's
    # own folded card, so that fold is not re-made; the digest is the preselection's
    assert picked["plan"] == preview["plan"]
    assert {i["item"]: (i.get("skipped"), i.get("copies")) for i in picked["curation"]}[
        fold[("property_pipeline", None)]] == ("held", None)
    assert [i.get("copies") for i in picked["curation"] if i["item"] == "pipeline"] == [
        [{"letter": "A", "skipped": None}]]
    assert [(x["letter"], x["lands"], x["property_id"]) for x in preview["letters"]] == [
        ("A", "kept", s), ("B", "origin", r)]
    assert str(b) not in str(preview) + str(out)

    assert _where(cur, [s1, r1]) == {s1: s, r1: r}
    # A: its note moved by choice and the note on r1 with its ad; the card home to R and a
    # copy, at the card's stage, on S (taking the place of S's own folded card); the collection
    # stays and its fold is re-made on R; the tag goes home; the tag whose twin is gone is not.
    assert _rows(cur, "property_notes", a) == sorted([(r, note_as, False), (r, note_ar, False)])
    assert _rows(cur, "property_pipeline", a) == sorted([(r, far_a, False), (s, far_a, True)])
    assert _rows(cur, "collection_properties", a) == sorted([(s, ca, False), (r, ca, True)])
    assert _rows(cur, "property_tags", a) == [(r, ta, False)]
    # B, by the rule alone: everything from R goes home, its tag's fold is re-made there, and
    # its dismissal of S, lifted when its card arrived, stands again now that the card left.
    assert _rows(cur, "property_notes", b) == [(r, note_br, False)]
    assert _rows(cur, "property_pipeline", b) == [(r, live_b, False)]
    assert _rows(cur, "collection_properties", b) == [(r, cb, False)]
    assert _rows(cur, "property_tags", b) == sorted([(s, tb, False), (r, tb, True)])
    assert _rows(cur, "property_dismissals", b) == sorted([(s, False, False), (s, True, True)])
    cur.execute("SELECT lift_reason FROM property_dismissals WHERE id = %s", (lifted,))
    assert cur.fetchone() == ("pipeline",)

    assert _delta(census, _census(cur)) == Counter({
        ("property_pipeline", a): 1, ("collection_properties", a): 1,
        ("property_tags", b): 1, ("property_dismissals", b): 1})
    assert _stranded(cur) == 0
    assert _spent(cur, group) == {
        ("moved", "property_notes", note_ar, a), ("moved", "property_notes", note_br, b),
        ("moved", "property_pipeline", None, a), ("moved", "property_pipeline", None, b),
        ("moved", "property_tags", ta, a), ("moved", "collection_properties", cb, b),
        ("folded", "collection_properties", ca, a), ("folded", "property_tags", tx, a),
        ("folded", "property_tags", tb, b), ("folded", "property_dismissals", lifted, b)}, (
        "S's own folded card stays unspent: its copy took the place")

    note = f"operator split {out['call_id']} · A: {s1} | B: {r1} · jiné patro"
    assert _words(cur, [s1, r1]) == {_pair(s1, r1): ("different", note)}
    assert _vetoes(cur, [s1, r1]) == {_pair(s1, r1): "operator"}
    assert out["rulings"] == {"different": 1, "same": 0, "taken_back": 0}
    assert sorted((c["item"], c["letter"], c["property_id"], c.get("skipped"), tuple(
        (x["letter"], x["property_id"]) for x in c["copies"])) for c in out["curation"]) == sorted([
        (f"note:{note_as}", "B", r, None, ()), (f"note:{note_ar}", "B", r, None, ()),
        ("pipeline", "B", r, None, (("A", s),)), (f"collection:{ca}", "A", s, None, ()),
        (f"tag:{ta}", "B", r, None, ()), (fold[("collection_properties", ca)], "B", r, None, ()),
        (fold[("property_pipeline", None)], "A", s, "held", ()),
        (fold[("property_tags", tx)], "B", r, "gone", ())])


def test_three_letters_one_origin_two_letters_and_a_join_that_folds(cur, accounts):
    """P{p} + Y{y1, y2} + Z{z1}, split A = {p}, B = {y1}, C = {y2, z1}: B and C each hold one of
    Y's ads, so B (the earliest) takes Y back and y2 is born a new property; C's two landings
    are joined into the older (y2's ad was first seen first), which takes back the "different"
    between y2 and z1 (MS12). Y's items go to B, Z's to C, the copy to C's kept landing; the
    join folds the card re-made on Z into that copy (MS13), so A's cards count +1 net."""
    a, b = accounts
    (p, y, z), (pa, y1, z1) = _props(cur, "sreality", "idnes", "remax")
    y2 = _advert(cur, y, source="bazos")
    _recomputed(cur, p, y, z)
    s3, s2 = _stage(cur, a, position=3), _stage(cur, a, position=2)
    cz, cy = _collection(cur, a), _collection(cur, b)
    _card(cur, a, y, s3)
    _card(cur, a, z, s2)                      # loses to Y's card: folded from Z
    note_y2, note_z = _note(cur, b, y, y2), _note(cur, a, z, None)
    _member(cur, "collection_properties", a, cz, z)
    _member(cur, "collection_properties", b, cy, y)
    group = _merge(cur, [p, y, z])["merge_group_id"]
    _seen(cur, y2, 30)                        # after the merge P survived: dates the join only
    cur.execute("INSERT INTO autodedup.verdicts (kind, listing_lo, listing_hi, verdict, note, "
                "reasons, decided_by) VALUES ('pair', %s, %s, 'different', 'jiné patro', '{}', %s)",
                (*_pair(y2, z1), OP))
    census = _census(cur)
    letters = {pa: "A", y1: "B", y2: "C", z1: "C"}
    assert letter_landings(cur.connection, p, letters).new == frozenset({y2})

    preview, out = _split(cur, p, letters, a, choices={"pipeline": ("B", ("C",))})
    (la, lb, lc) = preview["letters"]
    assert (la["lands"], lb["lands"], lb["property_id"]) == ("kept", "origin", y)
    assert (lc["lands"], lc["property_id"], lc["joins"]) == ("new", None, 2)
    assert preview["rulings"]["inside"] == [
        {"letter": "C", "pairs": [list(_pair(y2, z1))], "sets": 0, "taken_back": True}]
    assert preview["rulings"]["taken_back"] == 1 and preview["rulings"]["different"] == 5
    where = _where(cur, [pa, y1, y2, z1])
    born = where[y2]
    assert (where[pa], where[y1], where[z1]) == (p, y, born) and born not in (p, y, z)
    cur.execute("SELECT status FROM properties WHERE id = %s", (z,))
    assert cur.fetchone() == ("merged_away",), "the join kept y2's newborn, the older landing"
    cur.execute("SELECT reason, undone_at IS NOT NULL FROM property_merge_events "
                "WHERE listing_ref_id = %s AND retired_property_id = %s", (y2, born))
    assert cur.fetchall() == [("split_new", True)]
    assert [(x["letter"], x["lands"], x["property_id"], bool(x["joined"]))
            for x in out["letters"]] == [("A", "kept", p, False), ("B", "origin", y, False),
                                         ("C", "new", born, True)]
    assert out["rulings"] == {"different": 5, "same": 1, "taken_back": 1}
    words = _words(cur, [pa, y1, y2, z1])
    assert words[_pair(y2, z1)] == ("same", f"operator merge {out['letters'][2]['joined']}"), (
        "the join, a merge, took back the ruling inside C")
    assert {v for k, (v, _n) in words.items() if k != _pair(y2, z1)} == {"different"}

    assert _rows(cur, "property_pipeline", a) == sorted([(y, s3, False), (born, s3, True)])
    assert _rows(cur, "collection_properties", a) == [(born, cz, False)]
    assert _rows(cur, "collection_properties", b) == [(y, cy, False)]
    assert _rows(cur, "property_notes", b) == [(born, note_y2, False)]
    assert _rows(cur, "property_notes", a) == [(born, note_z, False)], (
        "a note written on no ad goes back where it came from, then joins C")
    assert _delta(census, _census(cur)) == Counter({("property_pipeline", a): 1})
    assert _stranded(cur) == 0
    cur.execute("SELECT count(*) FROM property_merge_carries c JOIN property_merge_events e "
                "ON e.merge_group_id = c.merge_group_id WHERE e.survivor_property_id = %s "
                "AND c.kind = 'folded' AND c.table_name = 'property_pipeline' "
                "AND c.merge_group_id <> %s::uuid", (born, group))
    assert cur.fetchone()[0] >= 1, "the join folded the card re-made on Z (MS13)"


def test_an_item_goes_back_where_its_oldest_standing_carry_row_says(cur, accounts):
    """X merged into R, then R into S: the tag X held came by two carry rows. Splitting X's ad
    off gives X its ad back, so the tag goes to X, the property it sat on before every merge
    that still stands, not to R, which gets nothing back."""
    a, _b = accounts
    (s, r, x), (s1, r1, x1) = _props(cur, "sreality", "idnes", "remax")
    _recomputed(cur, s, r, x)
    tag = _tag(cur, a)
    _member(cur, "property_tags", a, tag, x)
    _merge(cur, [r, x])
    _merge(cur, [s, r])
    _preview, out = _split(cur, s, {s1: "A", r1: "A", x1: "B"}, a)
    assert [(x_["letter"], x_["lands"], x_["property_id"]) for x_ in out["letters"]] == [
        ("A", "kept", s), ("B", "origin", x)]
    assert _rows(cur, "property_tags", a) == [(x, tag, False)]
    assert out["curation"] == [{"item": f"tag:{tag}", "kind": "tag", "label": out["curation"][0][
        "label"], "letter": "B", "property_id": x, "copies": []}]


def test_the_staying_letter_is_the_rules(cur):
    """Most own ads; the earliest letter on a tie; the canonical ad's letter when none holds one;
    and an ad whose origin is active again is born apart, its row undone, its birth `split_new`."""
    (p, r), (own1, merged) = _props(cur, "sreality", "idnes")
    own2 = _advert(cur, p, source="remax")
    _recomputed(cur, p, r)
    _merge(cur, [p, r], source="autodedup")
    assert letter_landings(cur.connection, p, {own1: "B", own2: "B", merged: "A"}).staying == "B"
    assert letter_landings(cur.connection, p, {own1: "B", own2: "A", merged: "A"}).staying == "A"
    (q1, q2, q3), (x1, x2, x3) = _props(cur, "sreality", "idnes", "bazos")
    _recomputed(cur, q1, q2, q3)
    held = _merge(cur, [q1, q2, q3], source="autodedup")["survivor_id"]
    from toolkit.property_identity import detach_listings

    detach_listings(cur.connection, [x1], decided_by=OP)   # x1 home: its record is active again
    cur.execute("UPDATE properties SET repr_listing_ref_id = %s WHERE id = %s", (x3, held))
    assert letter_landings(cur.connection, held, {x2: "A", x3: "B"}).staying == "B"


def test_a_merged_ad_whose_origin_is_active_again_is_born_a_new_property(cur):
    (p, r), (own, home) = _props(cur, "sreality", "idnes")
    other = _advert(cur, r, source="remax")
    _recomputed(cur, p, r)
    _merge(cur, [p, r], source="autodedup")
    from toolkit.property_identity import detach_listings

    detach_listings(cur.connection, [other], decided_by=OP)   # R is active again, holding it
    _preview, out = _split(cur, p, {own: "A", home: "B"}, None)
    born = out["letters"][1]["property_id"]
    assert out["letters"][1]["lands"] == "new" and born not in (p, r)
    assert _where(cur, [own, home, other]) == {own: p, home: born, other: r}
    cur.execute("SELECT reason, undone_at IS NOT NULL FROM property_merge_events "
                "WHERE listing_ref_id = %s ORDER BY id", (home,))
    assert cur.fetchall() == [("manual_subset", True), ("split_new", True)]


def test_the_click_is_refused_when_the_acting_accounts_items_changed_not_anothers(cur, accounts):
    a, b = accounts
    (s, r), (s1, r1) = _props(cur, "sreality", "idnes")
    _recomputed(cur, s, r)
    _merge(cur, [s, r])
    letters = {s1: "A", r1: "B"}
    preview = split_preview(cur.connection, s, letters=letters, account=a)
    _note(cur, b, s, r1)                                       # another account's: unseen
    assert split_preview(cur.connection, s, letters=letters, account=a)["plan"] == preview["plan"]
    _note(cur, a, s, r1)                                       # mine: the plan changed
    with pytest.raises(SplitRefused) as stale:
        split_property(cur.connection, s, letters=letters, decided_by=OP, account=a,
                       expect=preview["plan"])
    assert (stale.value.code, _where(cur, [s1, r1])) == ("stale", {s1: s, r1: s})
    _preview, out = _split(cur, s, letters, a)
    assert out["letters"][1]["property_id"] == r


def test_the_routing_lifts_a_dismissal_its_accounts_live_card_meets(cur, accounts):
    """The live-deal rule runs after routing on the property left and on every landing (rule
    22): a dismissal standing beside the account's live card (as a race could leave it) is
    lifted 'pipeline' where the card stays."""
    a, _b = accounts
    (s, r), (s1, r1) = _props(cur, "sreality", "idnes")
    _recomputed(cur, s, r)
    _merge(cur, [s, r])
    _card(cur, a, s, _stage(cur, a, position=2))
    dismissal = _dismiss(cur, a, s)
    _split(cur, s, {s1: "A", r1: "B"}, a)
    cur.execute("SELECT property_id, lift_reason FROM property_dismissals WHERE id = %s",
                (dismissal,))
    assert cur.fetchone() == (s, "pipeline")


def test_the_live_lane_reads_the_letters_and_nothing_inside_a_letter_is_ruled(cur):
    """The screenshot case: s stays, b goes home, i stays with s; the lane's must-link read sees
    nothing new, the must-not-link table holds (b, s) and (b, i) as the operator's."""
    (home, from_b, from_i), (s, b, i) = _props(cur, "sreality", "bazos", "idnes")
    _recomputed(cur, home, from_b, from_i)
    _merge(cur, [home, from_b, from_i], source="autodedup")
    ids = [s, b, i]
    _preview, out = _split(cur, home, {s: "A", b: "B", i: "A"}, None)
    assert _where(cur, ids) == {s: home, b: from_b, i: home}
    assert {k: v for k, (v, _n) in _words(cur, ids).items()} == {
        _pair(s, b): "different", _pair(b, i): "different"}
    assert _vetoes(cur, ids) == {_pair(s, b): "operator", _pair(b, i): "operator"}
    cur.execute(RT_MUST_LINK_SQL)
    assert _pair(s, i) not in {(int(lo), int(hi)) for lo, hi in cur.fetchall()}
    assert out["curation"] == []


def test_a_fold_is_re_made_only_while_its_twin_stands_in_every_table(cur, accounts):
    """S and R both hold A's collection entry, closed card and dismissal; the merge folds R's.
    A then takes the entry and the card off S and lifts its dismissal of S, so no fold has its
    twin: splitting R's ad back re-makes none of them on R (the preview says why), spends their
    carry rows and keeps every count."""
    a, _b = accounts
    (s, r), (s1, r1) = _props(cur, "sreality", "idnes")
    _recomputed(cur, s, r)
    coll = _collection(cur, a)
    dismissed = {}
    for pid, position in ((s, 3), (r, 2)):
        _member(cur, "collection_properties", a, coll, pid)
        _card(cur, a, pid, _stage(cur, a, position=position, terminal=True))
        dismissed[pid] = _dismiss(cur, a, pid)
    group = _merge(cur, [s, r])["merge_group_id"]
    assert set(_folds(cur, group)) == {("collection_properties", coll, a),
                                       ("property_pipeline", None, a),
                                       ("property_dismissals", dismissed[r], a)}
    cur.execute("DELETE FROM collection_properties WHERE property_id = %s AND account_id = %s",
                (s, a))
    cur.execute("DELETE FROM property_pipeline WHERE property_id = %s AND account_id = %s", (s, a))
    cur.execute("UPDATE property_dismissals SET lifted_at = now(), lift_reason = 'operator' "
                "WHERE property_id = %s AND account_id = %s AND lifted_at IS NULL", (s, a))
    census = _census(cur)

    preview, out = _split(cur, s, {s1: "A", r1: "B"}, a)
    assert sorted((i["kind"], i["letter"], i["why"], i["skipped"])
                  for i in preview["curation"]) == [
        ("collection", "B", "fold", "gone"), ("dismissal", "B", "fold", "gone"),
        ("pipeline", "B", "fold", "gone")]
    assert sorted((c["kind"], c["property_id"], c.get("skipped")) for c in out["curation"]) == [
        ("collection", r, "gone"), ("dismissal", r, "gone"), ("pipeline", r, "gone")]
    assert _where(cur, [s1, r1]) == {s1: s, r1: r}
    assert (_rows(cur, "collection_properties", a), _rows(cur, "property_pipeline", a)) == ([], [])
    assert [pid for pid, active, _now in _rows(cur, "property_dismissals", a) if active] == []
    assert _delta(census, _census(cur)) == Counter() and _stranded(cur) == 0
    assert _spent(cur, group) == {("folded", t, k, acc) for t, k, acc in _folds(cur, group)}


def test_an_admin_with_no_membership_is_shown_and_answered_no_item(cur, accounts):
    """With no acting account (an admin whose claims name none) the preview and the receipt list
    no item, an account's or the one with no account; every item still follows the rule."""
    a, _b = accounts
    (s, r), (s1, r1) = _props(cur, "sreality", "idnes")
    _recomputed(cur, s, r)
    mine, bare = _note(cur, a, r, r1), _note(cur, None, r, r1)
    _card(cur, a, r, _stage(cur, a, position=2))
    _merge(cur, [s, r])
    preview, out = _split(cur, s, {s1: "A", r1: "B"}, None)
    assert preview["curation"] == [] and out["curation"] == []
    said = str(preview) + str(out)
    assert str(a) not in said and f"note:{mine}" not in said and f"note:{bare}" not in said
    cur.execute("SELECT id, property_id FROM property_notes WHERE id = ANY(%s)", ([mine, bare],))
    assert dict(cur.fetchall()) == {mine: r, bare: r}
    assert _rows(cur, "property_pipeline", a)[0][0] == r


def test_a_different_ruled_inside_a_joined_letter_since_the_preview_is_stale(cur, accounts):
    """The digest covers the ruling counts: a "different" ruled between y2 and z1 after the
    preview, which C's join would take back though the preview counted none, refuses the click
    and moves nothing."""
    from toolkit.property_identity import record_ruling

    a, _b = accounts
    (p, y, z), (pa, y1, z1) = _props(cur, "sreality", "idnes", "remax")
    y2 = _advert(cur, y, source="bazos")
    _recomputed(cur, p, y, z)
    _merge(cur, [p, y, z])
    letters = {pa: "A", y1: "B", y2: "C", z1: "C"}
    preview = split_preview(cur.connection, p, letters=letters, account=a)
    assert preview["rulings"]["taken_back"] == 0
    record_ruling(cur.connection, *_pair(y2, z1), verdict="different", decided_by=OP,
                  note="jiné patro")
    with pytest.raises(SplitRefused) as stale:
        split_property(cur.connection, p, letters=letters, decided_by=OP, account=a,
                       expect=preview["plan"])
    assert stale.value.code == "stale" and set(_where(cur, list(letters)).values()) == {p}


def test_a_contentless_record_of_another_category_never_refuses_a_letter(cur):
    """The preview reads rule 15 as the merge gate does (`_CONTENTLESS`, one fragment): a
    contentless record stored as a house beside a flat in one leaving letter is not a clash, so
    the letter is not refused and its join goes through."""
    p = _property(cur)
    flats = [_advert(cur, p, source=src) for src in ("sreality", "idnes", "remax")]
    cur.execute("INSERT INTO listings (source, source_id_native, raw_json, category_main, "
                "category_type, property_id) VALUES ('bazos', %s, '{}'::jsonb, 'dum', 'prodej', "
                "%s) RETURNING id", (f"lp-{uuid.uuid4()}", p))
    record = int(cur.fetchone()[0])
    _recomputed(cur, p)
    letters = {flats[0]: "A", flats[1]: "A", flats[2]: "B", record: "B"}
    preview, out = _split(cur, p, letters, None)
    assert [(x["letter"], x["joins"], x["refused"]) for x in preview["letters"]] == [
        ("A", 0, None), ("B", 2, None)]
    assert out["letters"][1]["joined"] and _where(cur, [flats[2], record])[record] == (
        out["letters"][1]["property_id"])
