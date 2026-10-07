"""The split by letters (MS18), `toolkit.property_split`, and `GET`/`POST /properties/{id}/split`.

Over tests/_property_ledger.py's stateful fake, so every move replays through the real
`detach_listings` / `merge_property_set` and every ruling lands in a verdict store that reads back;
the curation plan is canned per test (`_plans`) and what reaches the router is recorded
(`db.routed`). The screenshot case: property 10 holds its own advert s=1, b=2 merged from 20 and
i=3 merged from 30; the operator gives b the letter B. Executed on Postgres, with two accounts'
curation routed: tests/test_property_split_live.py.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import timedelta
from typing import Any

import pytest

import toolkit.property_identity as pi
import toolkit.property_split as ps
from tests._property_ledger import OP, T0, _Ledger, ledger_carriers  # noqa: F401 — the fixture
from toolkit import property_carriers as carriers
from toolkit.property_carriers import Route
from toolkit.property_identity import merge_property_set
from toolkit.property_split import SplitRefused, split_preview, split_property

pytestmark = pytest.mark.usefixtures("ledger_carriers")

OTHER = "someone.else@example.com"
ME, THEM = uuid.UUID(int=1), uuid.UUID(int=2)


def _screenshot(**kw: Any) -> _Ledger:
    db = _Ledger({1: 10, 2: 20, 3: 30}, **kw)
    merge_property_set(db, [10, 20, 30], source="autodedup", reason="r")
    db.log.clear()
    return db


def _preview(db: _Ledger, letters: dict[int, str], *, pid: int = 10,
             account: uuid.UUID | None = ME) -> dict[str, Any]:
    return split_preview(db, pid, letters=letters, account=account)


def _split(db: _Ledger, letters: dict[int, str], *, pid: int = 10,
           account: uuid.UUID | None = ME, expect: str | None = None,
           **kw: Any) -> dict[str, Any]:
    if expect is None:
        expect = _preview(db, letters, pid=pid, account=account)["plan"]
    return split_property(db, pid, letters=letters, decided_by=OP, account=account,
                          expect=expect, **kw)


def _state(db: _Ledger) -> tuple:
    return (dict(db.listings), dict(db.props), [dict(e) for e in db.events],
            [dict(r) for r in db.verdicts], dict(db.mnl), list(db.carried), list(db.routed))


def _refused(fn: Any, *args: Any, **kw: Any) -> SplitRefused:
    with pytest.raises(SplitRefused) as exc:
        fn(*args, **kw)
    return exc.value


def test_the_screenshot_case_b_goes_home_and_only_different_is_written_across_letters():
    db = _screenshot()
    preview = _preview(db, {1: "A", 2: "B", 3: "A"})
    assert preview["letters"] == [
        {"letter": "A", "listing_ids": [1, 3], "lands": "kept", "property_id": 10, "joins": 0,
         "refused": None},
        {"letter": "B", "listing_ids": [2], "lands": "origin", "property_id": 20, "joins": 0,
         "refused": None}]
    assert preview["rulings"] == {"different": 2, "taken_back": 0, "inside": []}
    assert db.listings == {1: 10, 2: 10, 3: 10}, "the preview changes nothing"
    out = _split(db, {1: "A", 2: "B", 3: "A"}, reason=" jiná dispozice ")
    assert db.listings == {1: 10, 2: 20, 3: 10} and db.props[20] == "active"
    note = f"operator split {out['call_id']} · A: 1,3 | B: 2 · jiná dispozice"
    assert (db.word(1, 2), db.word(2, 3), db.word(1, 3)) == (
        ("different", note), ("different", note), None)
    assert set(db.mnl) == {(1, 2), (2, 3)} and {v[0] for v in db.mnl.values()} == {"operator"}
    assert out == {"property_id": 10, "call_id": out["call_id"], "curation": [],
                   "letters": [
                       {"letter": "A", "listing_ids": [1, 3], "lands": "kept",
                        "property_id": 10, "joined": None},
                       {"letter": "B", "listing_ids": [2], "lands": "origin",
                        "property_id": 20, "joined": None}],
                   "rulings": {"different": 2, "same": 0, "taken_back": 0}}


def test_the_rulings_are_appended_through_the_one_pair_writer_and_nothing_inside_a_letter():
    """E920: every word is a new row by `record_ruling`'s own statement; a split rules only
    "different", only across letters, and leaves another decider's word inside a letter."""
    db = _screenshot()
    db.rule(1, 3, "different", by=OTHER, note="their veto")
    preview = _preview(db, {1: "A", 2: "B", 3: "A"})
    assert preview["rulings"]["inside"] == [
        {"letter": "A", "pairs": [[1, 3]], "sets": 0, "taken_back": False}]
    out = _split(db, {1: "A", 2: "B", 3: "A"})
    note = f"operator split {out['call_id']} · A: 1,3 | B: 2"
    assert db.history(1, 3) == [("different", "their veto", OTHER)]
    appended = " ".join(pi.usql.VERDICT_PAIR_APPEND_SQL.split())
    assert {(p["listing_lo"], p["listing_hi"], p["verdict"]) for s, p in db.log
            if s == appended} == {(1, 2, "different"), (2, 3, "different")}
    assert {p["note"] for s, p in db.log if s == appended} == {note}
    assert not any("UPSERT" in s or "ON CONFLICT (kind" in s for s, _p in db.log)


def test_a_letter_of_native_adverts_leaves_as_one_new_record_the_oldest_of_its_births():
    """Five own adverts: A = {1, 2, 3} keeps 10 (most own adverts); B's two are each born a
    record and joined by the one merge into the oldest (decision 17; the first born on a tie).
    The join is a merge, so it rules its two canonical ads "same" (MS12) and the receipt says so."""
    db = _Ledger({1: 10, 2: 10, 3: 10, 4: 10, 5: 10})
    letters = {1: "A", 2: "A", 3: "A", 4: "B", 5: "B"}
    (a, b) = _preview(db, letters)["letters"]
    assert (a["lands"], b["lands"], b["property_id"], b["joins"]) == ("kept", "new", None, 2)
    out = _split(db, letters)
    born = out["letters"][1]["property_id"]
    assert born not in (None, 10) and db.listings == {1: 10, 2: 10, 3: 10, 4: born, 5: born}
    assert out["letters"][1]["joined"] is not None
    assert sorted(p for p, st in db.props.items() if st == "merged_away") == [born + 1]
    assert {e["reason"] for e in db.events if e.get("reason")} == {"ingest_grouping"}
    assert db.word(4, 5)[0] == "same" and {db.word(lo, hi)[0] for lo in (1, 2, 3)
                                            for hi in (4, 5)} == {"different"}
    assert out["rulings"] == {"different": 6, "same": 1, "taken_back": 0}


def test_the_join_keeps_the_landing_its_ads_date_oldest():
    """What the recompute will date each landing (its ads' earliest first seen), decision 17
    over it, unknown last; a tie goes to an existing property, then to the first born."""
    old, new = T0 - timedelta(days=3), T0
    at = {("origin", 30): [3], ("new", 2): [2], ("new", 4): [4]}
    assert ps._keeps(at, {2: new, 3: new, 4: old}) == ("new", 4)
    assert ps._keeps(at, {2: new, 3: new, 4: new}) == ("origin", 30)
    assert ps._keeps({("new", 2): [2], ("new", 4): [4]}, {2: None, 4: new}) == ("new", 4)
    assert ps._keeps({("new", 2): [2], ("new", 4): [4]}, {}) == ("new", 2)


def test_three_letters_land_apart():
    db = _screenshot()
    out = _split(db, {1: "A", 2: "B", 3: "C"})
    assert db.listings == {1: 10, 2: 20, 3: 30}
    assert [(x["letter"], x["property_id"], x["lands"]) for x in out["letters"]] == [
        ("A", 10, "kept"), ("B", 20, "origin"), ("C", 30, "origin")]
    assert {db.word(*p)[0] for p in ((1, 2), (1, 3), (2, 3))} == {"different"}
    assert out["rulings"] == {"different": 3, "same": 0, "taken_back": 0}


def test_the_staying_letter_is_the_rules():
    """Most own adverts; the earliest letter on a tie; the canonical advert's letter when no
    letter holds one of the property's own adverts."""
    db = _screenshot()
    out = _split(db, {1: "B", 2: "A", 3: "A"})
    assert out["letters"][1] == {"letter": "B", "listing_ids": [1], "lands": "kept",
                                 "property_id": 10, "joined": None}
    joined = out["letters"][0]
    assert (joined["lands"], joined["property_id"]) == ("origin", 20) and joined["joined"]
    assert db.listings == {1: 10, 2: 20, 3: 20} and db.props[30] == "merged_away"
    db = _Ledger({1: 10, 2: 10})
    out = _split(db, {1: "B", 2: "A"})
    assert out["letters"][0]["lands"] == "kept" and db.listings[2] == 10 and db.listings[1] != 10
    db = _Ledger({1: 20, 2: 30}, props={10: "active"}, canonical={10: 2},
                 first_seen={10: T0 - timedelta(days=9)})
    merge_property_set(db, [10, 20, 30], source="autodedup", reason="r")
    assert pi.letter_landings(db, 10, {1: "A", 2: "B"}).staying == "B"


def test_two_letters_from_one_origin_the_one_holding_more_of_its_adverts_gets_it():
    """P = {2, 3, 4} was merged into 10 beside its own 1. B holds two of P's adverts and goes
    home to 20; C's one is born a new record, its ledger row closed (`split_new`)."""
    db = _Ledger({1: 10, 2: 20, 3: 20, 4: 20})
    merge_property_set(db, [10, 20], source="autodedup", reason="r")
    land = pi.letter_landings(db, 10, {1: "A", 2: "C", 3: "B", 4: "B"})
    assert land == pi.Landings("A", {3: 20, 4: 20}, frozenset({2}))
    out = _split(db, {1: "A", 2: "C", 3: "B", 4: "B"})
    assert db.listings[3] == db.listings[4] == 20 and db.listings[2] not in (10, 20)
    assert out["letters"][2]["lands"] == "new"
    assert [e["reason"] for e in db.events if e.get("reason")] == ["split_new"]
    assert all(e["undone_by"] for e in db.events if e["listing"] == 2)
    # a tie goes to the earliest letter
    db = _Ledger({1: 10, 2: 20, 3: 20})
    merge_property_set(db, [10, 20], source="autodedup", reason="r")
    assert pi.letter_landings(db, 10, {1: "A", 2: "C", 3: "B"}) == pi.Landings(
        "A", {3: 20}, frozenset({2}))


def test_an_advert_whose_origin_is_active_again_goes_to_a_new_property():
    """20 got 3 back from a detach: it is not merged into 10 any more, so 2, which came from it,
    is born a new record rather than joining an advert nobody lettered."""
    db = _Ledger({1: 10, 2: 20, 3: 20})
    merge_property_set(db, [10, 20], source="autodedup", reason="r")
    pi.detach_listings(db, [3], decided_by=OP)
    assert pi.letter_landings(db, 10, {1: "A", 2: "B"}) == pi.Landings("A", {}, frozenset({2}))
    _split(db, {1: "A", 2: "B"})
    assert db.listings[3] == 20 and db.listings[2] not in (10, 20)
    assert [e["reason"] for e in db.events if e.get("reason")] == ["split_new"]


def test_a_newcomer_on_the_property_or_an_advert_elsewhere_is_stale_and_writes_nothing():
    db = _screenshot()
    expect = _preview(db, {1: "A", 2: "B", 3: "A"})["plan"]
    db.listings[4] = 10                     # the lane merged 4 in after the page loaded
    before = _state(db)
    stale = _refused(_split, db, {1: "A", 2: "B", 3: "A"}, expect=expect)
    assert (stale.status, stale.code, stale.ids, stale.message) == (
        409, "stale", [4], "Nemovitost se mezitím změnila; nic se nezapsalo.")
    assert _state(db) == before
    db = _screenshot()
    db.listings.update({3: 70, 7: 70})
    db.props[70] = "active"
    assert _refused(_split, db, {1: "A", 2: "B", 3: "A"}, expect="x").code == "stale"
    # the newcomer lands between the unlocked read and the lock: the locked re-read decides
    db = _screenshot()
    expect = _preview(db, {1: "A", 2: "B", 3: "A"})["plan"]
    dispatch = db.dispatch
    db.dispatch = lambda s, p: (db.listings.update({4: 10}) if "FOR UPDATE" in s
                                else None) or dispatch(s, p)
    before = _state(db)
    assert _refused(_split, db, {1: "A", 2: "B", 3: "A"}, expect=expect).code == "stale"
    assert _state(db) == before and not db.sql("UPDATE listings"), "refused under the lock"


def _plans(monkeypatch: pytest.MonkeyPatch, rows: list[Route]) -> list[dict[str, Any]]:
    """`curation_plan` answering `rows` (a list a test may change), with each call's
    arguments recorded; the preselection and the choices are the real plan's (tested live)."""
    calls: list[dict[str, Any]] = []

    def plan(cur: Any, **kw: Any) -> list[Route]:
        calls.append(kw)
        out = list(rows)
        for item, (anchor, copies) in (kw.get("choices") or {}).items():
            out = [r._replace(anchor=anchor) if r.item == item else r for r in out]
            out += [Route(r.table, r.key, r.account_id, "copy", c, None, r.stage_id, (),
                          r.item, r.label) for r in rows if r.item == item for c in copies]
        return out

    monkeypatch.setattr(carriers, "curation_plan", plan)
    return calls


def _note(account: uuid.UUID, nid: int, anchor: int | None) -> Route:
    return Route("property_notes", nid, account, "move", anchor, None, None, (), f"note:{nid}",
                 f"poznámka {nid}")


def test_the_plan_is_what_the_click_wrote_or_nothing(monkeypatch):
    """The digest covers the acting account's items: its new note makes the click stale;
    another account's does not (and is never shown)."""
    rows = [_note(ME, 11, 2), _note(THEM, 12, 3)]
    _plans(monkeypatch, rows)
    db = _screenshot()
    preview = _preview(db, {1: "A", 2: "B", 3: "A"})
    assert preview["curation"] == [{"item": "note:11", "kind": "note", "label": "poznámka 11",
                                    "letter": "B", "why": "ad"}]
    rows.append(_note(THEM, 13, 1))
    assert _preview(db, {1: "A", 2: "B", 3: "A"})["plan"] == preview["plan"]
    rows.append(_note(ME, 14, 1))
    before = _state(db)
    stale = _refused(_split, db, {1: "A", 2: "B", 3: "A"}, expect=preview["plan"])
    assert (stale.code, _state(db)) == ("stale", before)
    assert _split(db, {1: "A", 2: "B", 3: "A"})["curation"][0]["item"] == "note:11"


def test_a_plan_read_for_other_letters_is_stale():
    """The digest covers the letters: the same ads lettered otherwise are another plan, though
    nothing else about it (its counts, the letter that stays) changed."""
    db = _screenshot()
    expect = _preview(db, {1: "A", 2: "B", 3: "A"})["plan"]
    before = _state(db)
    stale = _refused(_split, db, {1: "A", 2: "B", 3: "B"}, expect=expect)
    assert (stale.code, _state(db)) == ("stale", before)
    # the same shape (A stays, B born twice and joined, four pairs ruled) over other ads
    db = _Ledger({1: 10, 2: 10, 3: 10, 4: 10})
    shown = _preview(db, {1: "A", 2: "A", 3: "B", 4: "B"})
    other = _preview(db, {1: "A", 2: "B", 3: "A", 4: "B"})
    assert {k: v for k, v in shown.items() if k not in ("letters", "plan")} == {
        k: v for k, v in other.items() if k not in ("letters", "plan")}
    assert _refused(_split, db, {1: "A", 2: "B", 3: "A", 4: "B"},
                    expect=shown["plan"]).code == "stale"


def test_a_plan_whose_landing_alone_changed_is_stale():
    """The digest covers where each letter lands: 20 active again since the preview sends B to a
    new property instead, with the same ads, letters, items and ruling counts, so the click on
    the old plan is refused."""
    db = _screenshot()
    letters = {1: "A", 2: "B", 3: "A"}
    shown = _preview(db, letters)
    db.props[20] = "active"
    db.into.pop(20, None)
    again = _preview(db, letters)
    assert (again["letters"][1]["lands"], shown["letters"][1]["lands"]) == ("new", "origin")
    assert {k: again[k] for k in ("curation", "rulings")} == {
        k: shown[k] for k in ("curation", "rulings")}
    before = _state(db)
    assert _refused(_split, db, letters, expect=shown["plan"]).code == "stale"
    assert _state(db) == before


def _picked_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """`curation_plan` as the real one answers my card going home to 20 with a copy picked for
    the staying letter: the copy lands where the fold of my own card would be re-made, which the
    conflict pass then skips ('held'); without picks, the fold is re-made."""
    card = Route("property_pipeline", None, ME, "move", 2, 20, 4, (5,), "pipeline", "lp 3")
    fold = Route("property_pipeline", None, ME, "recreate", None, 10, 3, (13,), "fold:13",
                 "lp 2")

    def plan(cur: Any, **kw: Any) -> list[Route]:
        if not kw.get("choices"):
            return [card, fold]
        return [card, card._replace(action="copy", anchor=None, carry_ids=()),
                fold._replace(skipped="held", carry_ids=())]

    monkeypatch.setattr(carriers, "curation_plan", plan)


def test_the_preview_with_the_picks_says_what_the_click_would_skip_and_keeps_its_plan(
        monkeypatch):
    """Picks change what the preview says of the acting account's items (a copy, and the fold it
    takes the place of), never `plan`: the digest is the preselection's, so the click made on
    the picked preview holds; the receipt names the fold that was not re-made, and why."""
    _picked_plan(monkeypatch)
    db = _screenshot()
    letters = {1: "A", 2: "B", 3: "A"}
    plain = _preview(db, letters)
    picked = split_preview(db, 10, letters=letters, account=ME,
                           choices={"pipeline": ("B", ("A",))})
    assert picked["plan"] == plain["plan"]
    shown = [(i["item"], i["letter"], i.get("skipped"), i.get("copies"))
             for i in plain["curation"]]
    assert shown == [("pipeline", "B", None, None), ("fold:13", "A", None, None)]
    assert [(i["item"], i["letter"], i.get("skipped"), i.get("copies"))
            for i in picked["curation"]] == [
        ("pipeline", "B", None, [{"letter": "A", "skipped": None}]),
        ("fold:13", "A", "held", None)]
    out = _split(db, letters, expect=picked["plan"], choices={"pipeline": ("B", ("A",))})
    assert [(c["item"], c.get("skipped"), c["copies"]) for c in out["curation"]] == [
        ("pipeline", None, [{"letter": "A", "property_id": 10}]), ("fold:13", "held", [])]


def test_the_preview_route_reads_the_picks_as_the_click_sends_them(client, monkeypatch):
    from api import dependencies as deps
    from api import main as api_main
    from api import property_merge as pm

    http, _db = client
    _picked_plan(monkeypatch)
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {
        "is_admin": True, "email": OP, "sub": "me"}
    monkeypatch.setattr(pm.tenant_pool, "resolve_account_id", lambda conn, claims: ME)
    params = {"letters": "1:A,2:B,3:A"}
    plain = http.get("/properties/10/split", params=params).json()
    picked = http.get("/properties/10/split", params={
        **params, "choices": '{"pipeline": {"to": "B", "copies": ["A"]}}'}).json()
    assert picked["plan"] == plain["plan"] and picked["curation"][1]["skipped"] == "held"
    for raw, message in (("nope", "Volby se nepodařilo přečíst."),
                         ('{"pipeline": "B"}', "Volby se nepodařilo přečíst."),
                         ('{"note:9": {"to": "A"}}', "Volba patří položce, kterou náhled neukázal."),
                         ('{"pipeline": {"to": "Z"}}',
                          "Volba míří na písmeno, které žádný inzerát nemá.")):
        refused = http.get("/properties/10/split", params={**params, "choices": raw})
        assert (refused.status_code, refused.json()["detail"]["message"]) == (400, message)


def test_choices_move_and_copy_the_acting_accounts_items_only(monkeypatch):
    """A letter becomes the ad whose landing its join keeps; the staying letter stays (None);
    an unchanged letter keeps the preselected anchor; the receipt names only my items."""
    calls = _plans(monkeypatch, [_note(ME, 11, 2), _note(THEM, 12, 2)])
    db = _screenshot()
    out = _split(db, {1: "A", 2: "B", 3: "C"},
                 choices={"note:11": ("A", ("C",))})
    assert calls[-1]["choices"] == {"note:11": (None, (3,))}
    assert calls[-1]["account"] == ME and calls[-1]["movers"] == {2: 20, 3: 30}
    (left, routes, landed) = db.routed[0]
    assert left == 10 and landed == {2: 20, 3: 30}
    assert [(r.item, r.action, r.anchor, r.account_id) for r in routes] == [
        ("note:11", "move", None, ME), ("note:12", "move", 2, THEM), ("note:11", "copy", 3, ME)]
    assert out["curation"] == [{"item": "note:11", "kind": "note", "label": "poznámka 11",
                                "letter": "A", "property_id": 10,
                                "copies": [{"letter": "C", "property_id": 30}]}]
    out = _split(_screenshot(), {1: "A", 2: "B", 3: "A"}, choices={"note:11": ("B", ())})
    assert calls[-1]["choices"] == {"note:11": (2, ())}


def test_the_statement_and_its_choices_are_validated_before_anything_is_written(monkeypatch):
    _plans(monkeypatch, [_note(ME, 11, 2)])
    db = _screenshot()
    for letters, why in (
        ({}, "Rozdělení jmenuje 1 až 100 inzerátů."),
        (dict.fromkeys(range(1, 102), "A"), "Rozdělení jmenuje 1 až 100 inzerátů."),
        ({1: "a", 2: "B"}, "Písmeno musí být A–Z."),
        ({1: "AB", 2: "B"}, "Písmeno musí být A–Z."),
        ({1: "A", 2: "A", 3: "A"}, "Všechny inzeráty mají stejné písmeno, není co rozdělit."),
    ):
        refused = _refused(_split, db, letters, expect="x")
        assert (refused.status, refused.code, refused.message) == (400, "invalid", why)
    letters = {1: "A", 2: "B", 3: "A"}
    for kw, why in (
        ({"reason": "x" * 501}, "Důvod má nejvýš 500 znaků."),
        ({"expect": ""}, "Chybí náhled rozdělení."),
        ({"choices": {"note:11": ("A", ("A",))}},
         "Kopie nemůže jít do písmene, které položku dostane."),
        ({"choices": {"note:11": ("Z", ())}}, "Volba míří na písmeno, které žádný inzerát nemá."),
        ({"choices": {"note:99": ("A", ())}}, "Volba patří položce, kterou náhled neukázal."),
    ):
        kw = {"expect": _preview(db, letters)["plan"], **kw}
        refused = _refused(split_property, db, 10, letters=letters, decided_by=OP, account=ME,
                           **kw)
        assert (refused.status, refused.code, refused.message) == (400, "invalid", why)
    assert db.listings == {1: 10, 2: 10, 3: 10} and db.verdicts == []
    assert _refused(_preview, db, {1: "A", 2: "B", 3: "A"}, pid=404).message == (
        "Nemovitost #404 neexistuje.")
    assert _refused(_preview, db, {1: "A", 2: "B", 3: "A", 99: "B"}).message == (
        "Inzerát #99 neexistuje.")
    assert ps.parse_letters("1:A, 2:B,3:A") == letters
    assert _refused(ps.parse_letters, "1:A,1:B").message == "Inzerát je uveden dvakrát."
    assert _refused(ps.parse_letters, "x:A,2:B").code == "invalid"


def test_a_letter_whose_join_rule_15_refuses_is_named_in_the_preview_and_raises_on_the_click():
    """2 and 3 are born apart (11, 12), and the merge joining them refuses a sale and a rental:
    the preview names the letter, and the click's `CategoryClash` reaches the route, which says
    it in Czech (the route test below). Nothing moves."""
    db = _Ledger({1: 10, 2: 10, 3: 10, 4: 10}, ad_cats={3: ("pronajem", "byt")})
    letters = {1: "A", 4: "A", 2: "B", 3: "B"}
    (_a, b) = _preview(db, letters)["letters"]
    assert b["refused"] == {"code": "refused", "field": "category_type", "a": "prodej",
                            "b": "pronajem", "ids": [2, 3]}
    before = _state(db)
    with pytest.raises(pi.CategoryClash) as clash:
        _split(db, letters)
    assert (clash.value.field, clash.value.properties, clash.value.ads) == (
        "category_type", (11, 12), (2, 3))
    assert _state(db) == before
    # a contentless record never counts, in the preview as at the gate: nothing refused
    db = _Ledger({1: 10, 2: 10, 3: 10, 4: 10}, ad_cats={3: ("pronajem", "byt")}, contentless={3})
    assert _preview(db, letters)["letters"][1]["refused"] is None
    assert _split(db, letters)["letters"][1]["joined"] is not None


def test_a_letters_join_takes_back_the_different_rulings_between_its_landings():
    """MS12 inside a split: C lands on 20 and 30 and is joined; the "different" between its two
    adverts is taken back (the preview says so), a negative inside A stands."""
    db = _Ledger({1: 10, 4: 10, 2: 20, 3: 30}, canonical={20: 2, 30: 3})
    merge_property_set(db, [10, 20, 30], source="autodedup", reason="r")
    db.rule(2, 3, "different")
    db.rule(1, 4, "different")
    preview = _preview(db, {1: "A", 4: "A", 2: "C", 3: "C"})
    assert preview["rulings"] == {"different": 4, "taken_back": 1, "inside": [
        {"letter": "A", "pairs": [[1, 4]], "sets": 0, "taken_back": False},
        {"letter": "C", "pairs": [[2, 3]], "sets": 0, "taken_back": True}]}
    out = _split(db, {1: "A", 4: "A", 2: "C", 3: "C"})
    assert out["rulings"] == {"different": 4, "same": 1, "taken_back": 1}
    assert db.word(2, 3)[0] == "same" and db.word(1, 4)[0] == "different"


def test_the_e52_helper_is_the_one_both_verdict_split_routes_call():
    import api.routes.autodedup as routes

    assert routes.reversed_pairs is ps.reversed_pairs
    assert routes.newest_pair_rulings is ps.newest_pair_rulings
    assert not hasattr(routes, "_split_summary") and not hasattr(ps, "operator_pair_verdicts")
    src = inspect.getsource(routes)
    assert src.count("newest_pair_rulings(conn, member_ids)") == 2
    assert ps.reversed_pairs({(1, 2): "different", (1, 3): "same", (2, 3): None},
                             [(1, 2), (1, 3), (2, 3)]) == [(1, 2)]


def test_a_mover_that_does_not_move_under_the_lock_is_stale_and_rolls_back_every_move():
    db = _screenshot()
    expect = _preview(db, {1: "A", 2: "B", 3: "C"})["plan"]
    dispatch = db.dispatch

    def refuse_three(s: str, p: Any) -> list[tuple]:
        if s.startswith("UPDATE listings SET property_id = %s WHERE id = %s AND") and p[1] == 3:
            db.count = 0
            return []
        return dispatch(s, p)

    db.dispatch = refuse_three
    before = _state(db)
    stuck = _refused(_split, db, {1: "A", 2: "B", 3: "C"}, expect=expect)
    assert (stuck.code, stuck.ids) == ("stale", [3])
    db.dispatch = dispatch
    assert _state(db) == before, "the move of 2 rolled back with the rest"


def test_a_lock_timeout_or_deadlock_is_busy():
    import psycopg

    db = _screenshot()
    expect = _preview(db, {1: "A", 2: "B", 3: "A"})["plan"]
    dispatch = db.dispatch

    def locked(s: str, p: Any) -> list[tuple]:
        if "FOR UPDATE" in s:
            raise psycopg.errors.LockNotAvailable("lock timeout")
        return dispatch(s, p)

    db.dispatch = locked
    before = _state(db)
    busy = _refused(_split, db, {1: "A", 2: "B", 3: "A"}, expect=expect)
    assert (busy.code, busy.message) == ("busy", "Nemovitost se právě mění, zkuste to za chvíli.")
    assert _state(db) == before


def test_the_split_writes_only_through_the_chokepoint_and_the_preview_writes_nothing():
    """Rule 15: the split moves adverts through `detach_listings` / `merge_property_set` and rules
    through `record_rulings`, and holds no write statement of its own; the preview runs in a
    read-only transaction that is rolled back."""
    src = inspect.getsource(ps)
    for statement in ("UPDATE listings", "UPDATE properties", "INSERT INTO property_merge_events",
                      "INSERT INTO autodedup", "DELETE FROM", "INSERT INTO properties",
                      "SELECT id, property_id FROM listings WHERE id"):
        assert statement not in src, statement
    for writer in ("detach_listings(", "merge_property_set(", "record_rulings("):
        assert writer in inspect.getsource(ps.split_property) + inspect.getsource(ps._join)
    assert src.count("detach_listings(") == 1, "the split detaches its movers as ONE set"
    for gone in ("undo_split", "keep_together", "restore_must_not_link", "detach_outcomes"):
        assert gone not in src, gone
    db = _screenshot()
    _preview(db, {1: "A", 2: "B", 3: "A"})
    assert db.log[0] == (ps._READ_ONLY, None) and db.log[-1] == ("rollback", None)
    assert not any(s.startswith(("UPDATE", "INSERT", "DELETE")) for s, _p in db.log)


@pytest.mark.parametrize(("listings", "letters", "joins"), [
    ({1: 10, 2: 20, 3: 30}, {1: "A", 2: "B", 3: "C"}, 0),     # two merged adverts go home apart
    ({1: 10, 2: 10, 3: 10, 4: 10}, {1: "A", 2: "A", 3: "B", 4: "C"}, 0),   # each born apart
    ({1: 10, 2: 10, 3: 10, 4: 10}, {1: "A", 2: "A", 3: "B", 4: "B"}, 1),   # born, then joined
    (dict.fromkeys(range(1, 7), 10), dict(zip(range(1, 7), "AABBCC")), 2),
])
def test_a_split_brings_derived_state_current_once_per_writer_call(listings, letters, joins):
    """M movers and J joined letters: ONE `properties_changed` for the set detach (over the
    record and every property reached), then one per join — 1 + J, not M + J."""
    db = _Ledger(listings)
    if len(set(listings.values())) > 1:
        merge_property_set(db, sorted(set(listings.values())), source="autodedup", reason="r")
    expect = _preview(db, letters)["plan"]
    for seen in (db.changed, db.browse, db.broker):
        seen.clear()
    _split(db, letters, expect=expect)
    assert len(db.changed) == 1 + joins and db.changed == db.browse == db.broker
    assert 10 in db.changed[0] and len(db.routed) == 1


def test_one_pair_verdict_vocabulary():
    """Review finding 5: the store's vocabulary is `ui_sql.VERDICT_VALUES`, once."""
    from autodedup import ui_sql as usql

    assert not hasattr(pi, "PAIR_VERDICTS")
    assert set(usql.VERDICT_VALUES) == {"same", "unsure", *usql.NEGATIVE_VERDICTS}
    with pytest.raises(ValueError):
        pi.record_rulings(_Ledger({1: 10}), {(1, 2)}, verdict="maybe", decided_by=OP, note=None)


# --- the routes ------------------------------------------------------------------------------


fastapi = pytest.importorskip("fastapi")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from api import dependencies as deps
    from api import main as api_main

    db = _screenshot()
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: db
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {
        "is_admin": True, "email": OP}
    yield TestClient(api_main.app), db
    api_main.app.dependency_overrides.clear()


def test_the_routes_preview_then_split_by_letters(client):
    http, db = client
    preview = http.get("/properties/10/split", params={"letters": "1:A,2:B,3:A"})
    assert preview.status_code == 200 and db.listings == {1: 10, 2: 10, 3: 10}
    body = preview.json()
    assert [(x["letter"], x["lands"], x["property_id"]) for x in body["letters"]] == [
        ("A", "kept", 10), ("B", "origin", 20)]
    res = http.post("/properties/10/split", json={
        "letters": {"1": "A", "2": "B", "3": "A"}, "reason": " jiné patro ",
        "expect": body["plan"]})
    assert res.status_code == 200 and db.listings == {1: 10, 2: 20, 3: 10}
    assert res.json()["rulings"] == {"different": 2, "same": 0, "taken_back": 0}
    assert db.word(1, 2)[1].endswith("· jiné patro")
    # the property changed since: the same click is stale, in Czech
    again = http.post("/properties/10/split", json={
        "letters": {"1": "A", "2": "B", "3": "A"}, "expect": body["plan"]})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "stale"


def test_the_route_vocabulary_is_the_stores(client):
    import api.routes.autodedup as routes
    from autodedup import ui_sql as usql

    assert routes.VERDICT_VALUES is usql.VERDICT_VALUES


def test_the_routes_answer_refusals_with_code_message_and_ids(client):
    from api import dependencies as deps
    from api import main as api_main

    http, db = client
    body = {"letters": {"1": "A", "2": "B", "3": "A"}, "expect": "x"}
    assert http.post("/properties/404/split", json=body).json()["detail"] == {
        "code": "not_found", "message": "Nemovitost #404 neexistuje.", "ids": [404]}
    assert http.post("/properties/10/split", json={
        **body, "letters": {"1": "A", "2": "B", "3": "A", "99": "B"}}).status_code == 404
    assert http.post("/properties/10/split", json=body).json()["detail"]["code"] == "stale"
    # the statement's limits are the toolkit's, answered in Czech, never pydantic's English 422
    for over, message in (
        ({"reason": "x" * 501}, "Důvod má nejvýš 500 znaků."),
        ({"letters": {"1": "a", "2": "B"}}, "Písmeno musí být A–Z."),
        ({"letters": {str(i): "A" for i in range(101)}}, "Rozdělení jmenuje 1 až 100 inzerátů."),
        ({"letters": {}}, "Rozdělení jmenuje 1 až 100 inzerátů."),
        ({"choices": {"note:1": {"to": "b"}}}, "Volba míří na písmeno, které žádný inzerát nemá."),
    ):
        refused = http.post("/properties/10/split", json={**body, **over})
        assert (refused.status_code, refused.json()["detail"]) == (
            400, {"code": "invalid", "message": message, "ids": refused.json()["detail"]["ids"]})
    assert http.post("/properties/10/split", json={
        "letters": {"1": "A", "2": "B", "3": "A"}}).json()["detail"]["message"] == (
        "Chybí náhled rozdělení.")
    gone = http.post("/properties/10/split", json={"adverts": [1, 2, 3], "separate": [[2]],
                                                    "keep_together": True})
    assert gone.status_code == 422, "the old statement is gone"
    assert http.get("/properties/10/split", params={"letters": "1:A,2:A,3:A"}).json()[
        "detail"]["message"] == "Všechny inzeráty mají stejné písmeno, není co rozdělit."
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    assert http.post("/properties/10/split", json=body).status_code == 403
    assert db.listings == {1: 10, 2: 10, 3: 10}


def test_a_leaving_letter_that_mixes_categories_is_refused_in_czech(client):
    """The join of a letter that landed on two records goes through the merge chokepoint, whose
    rule-15 gate over their ads raises `CategoryClash`; the routes print E925's Czech sentence
    (the pair page's words, the Browse labels) naming its two ads, never the chokepoint's
    English. Nothing moves."""
    from api.category_clash_text import LETTER_ENDING, SAME_ENDING, clash_sentence

    http, db = client
    db.ad_cats[3] = ("prodej", "ostatni")  # re-filed since the merge
    sentence = (
        "Inzerát v kategorii Byty a inzerát v kategorii Ostatní systém nikdy nespojí do jedné "
        "nemovitosti (výjimkou jsou jen dvojice dům – komerční objekt, dům – pozemek, komerční "
        "objekt – pozemek a byt – komerční objekt), proto nemohou mít stejné písmeno. Dejte jim "
        "různá písmena.")
    preview = http.get("/properties/10/split", params={"letters": "1:A,2:B,3:B"}).json()
    assert preview["letters"][1]["refused"] == {"code": "refused", "ids": [2, 3],
                                                "message": sentence}
    res = http.post("/properties/10/split", json={
        "letters": {"1": "A", "2": "B", "3": "B"}, "expect": preview["plan"]})
    assert res.status_code == 409 and db.listings == {1: 10, 2: 10, 3: 10}
    assert res.json()["detail"] == {"code": "refused", "ids": [2, 3], "message": sentence}
    # one sentence, two endings: the pair page's refusal differs only in its ending
    assert clash_sentence("category_main", "byt", "ostatni", ending=SAME_ENDING).endswith(
        "proto je nelze označit jako stejné.")
    assert clash_sentence("category_type", "prodej", "pronajem", ending=LETTER_ENDING) == (
        "Inzerát typu Prodej a inzerát typu Pronájem systém nikdy nespojí do jedné nemovitosti"
        + LETTER_ENDING)
