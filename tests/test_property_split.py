"""The operator's split statement, `toolkit.property_split` (E919), and `POST /properties/{id}/split`.

Over tests/_property_ledger.py's stateful fake, so every move replays through the real
`detach_listing` / `merge_property_set` and every ruling lands in a verdict store that reads back.
The screenshot case: property 10 holds its own advert s=1, b=2 merged from 20 and i=3 merged from
30; the operator separates b and confirms s + i as one. Executed on Postgres:
tests/test_property_split_live.py.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import timedelta
from typing import Any

import pytest

import toolkit.property_identity as pi
import toolkit.property_split as ps
from tests._property_ledger import OP, T0, _Ledger
from toolkit.property_identity import merge_property_set
from toolkit.property_split import SplitRefused, split_property, undo_split

OTHER = "someone.else@example.com"


def _screenshot(**kw: Any) -> _Ledger:
    db = _Ledger({1: 10, 2: 20, 3: 30}, **kw)
    merge_property_set(db, [10, 20, 30], source="autodedup", reason="r")
    db.log.clear()
    return db


def _split(db: _Ledger, separate: list[list[int]], *, adverts: list[int] | None = None,
           keep_together: bool = True, pid: int = 10, **kw: Any) -> dict[str, Any]:
    return split_property(db, pid, adverts=adverts or sorted(db.listings), separate=separate,
                          keep_together=keep_together, decided_by=OP, **kw)


def _state(db: _Ledger) -> tuple:
    return (dict(db.listings), dict(db.props), [dict(e) for e in db.events],
            {k: dict(v) for k, v in db.verdicts.items()}, dict(db.mnl))


def _refused(fn: Any, *args: Any, **kw: Any) -> SplitRefused:
    with pytest.raises(SplitRefused) as exc:
        fn(*args, **kw)
    return exc.value


def test_the_screenshot_case_one_advert_leaves_and_the_rest_are_confirmed_one():
    db = _screenshot()
    db.rule(1, 3, "different", by=OTHER, note="their veto")   # someone else's word: no E52
    out = _split(db, [[2]], reason="jiná dispozice")
    assert db.listings == {1: 10, 2: 20, 3: 10} and db.props[20] == "active"
    note = f"operator split {out['call_id']} · A: 1,3 | B: 2 · jiná dispozice"
    assert (db.word(1, 2), db.word(2, 3), db.word(1, 3)) == (
        ("different", note), ("different", note), ("same", note))
    assert db.word(1, 3, OTHER) == ("different", "their veto"), "nobody else's word is touched"
    assert set(db.mnl) == {(1, 2), (2, 3)} and {v[0] for v in db.mnl.values()} == {"operator"}
    assert out["record_kept_by"] == "A" and out["property_id"] == 10 and out["moved"] == 1
    kept, separated = out["units"]
    assert kept == {"unit": "A", "role": "kept", "listing_ids": [1, 3], "property_id": 10,
                    "moved": [], "merge_group_id": None}
    assert separated == {"unit": "B", "role": "separated", "listing_ids": [2], "property_id": 20,
                         "moved": [{"listing_id": 2, "outcome": "detached", "from": 10, "to": 20}],
                         "merge_group_id": None}
    assert out["rulings"] == {"written": 3, "same": 1, "different": 2,
                              "must_not_link_written": 2, "must_not_link_retracted": 1}
    assert out["undo"] == {"call_id": out["call_id"], "placements": {1: 10, 2: 20, 3: 10},
                           "rulings": [{"listing_lo": lo, "listing_hi": hi, "verdict": None,
                                        "note": None, "reasons": [], "must_not_link": veto}
                                       for lo, hi, veto in (
                                           (1, 2, None),
                                           (1, 3, {"source": "operator", "reason": "their veto"}),
                                           (2, 3, None))]}


def test_a_whole_group_leaves_together_as_one_record_the_oldest():
    """Three own adverts (grouped at ingest): 2 and 3 are one unit. Each is born a record, the
    two records are joined by the one merge, and (2, 3) ends `same` although the detach of 2 first
    ruled it `different` from 3, still on the property then."""
    db = _Ledger({1: 10, 2: 10, 3: 10})
    out = _split(db, [[2, 3]])
    born = out["units"][1]["property_id"]
    assert born not in (None, 10) and db.listings == {1: 10, 2: born, 3: born}
    assert [m["outcome"] for m in out["units"][1]["moved"]] == ["split_native", "split_native"]
    assert out["units"][1]["merge_group_id"] is not None
    assert sorted(p for p, s in db.props.items() if s == "merged_away") == [born + 1]
    assert db.word(2, 3)[0] == "same" and (2, 3) not in db.mnl
    assert db.word(1, 2)[0] == db.word(1, 3)[0] == "different"
    assert set(db.mnl) == {(1, 2), (1, 3)}


def test_two_groups_leave_apart_from_each_other():
    db = _screenshot()
    out = _split(db, [[2], [3]])
    assert db.listings == {1: 10, 2: 20, 3: 30}
    assert [u["property_id"] for u in out["units"]] == [10, 20, 30]
    assert {db.word(*p)[0] for p in ((1, 2), (1, 3), (2, 3))} == {"different"}
    assert out["rulings"]["same"] == 0 and set(db.mnl) == {(1, 2), (1, 3), (2, 3)}


def test_confirm_as_one_moves_nothing_and_rules_every_pair_same():
    db = _screenshot()
    db.rule(1, 2, "different", by=OTHER)
    events, listings = [dict(e) for e in db.events], dict(db.listings)
    out = _split(db, [])
    assert db.events == events and db.listings == listings and out["moved"] == 0
    assert {db.word(*p)[0] for p in ((1, 2), (1, 3), (2, 3))} == {"same"}
    assert db.mnl == {}, "the operator's must-not-link is retracted"
    assert out["undo"]["placements"] == {} and len(out["undo"]["rulings"]) == 3
    assert not any(s.startswith("UPDATE") for s, _p in db.log)


def test_a_resend_changes_nothing_and_offers_no_undo():
    db = _screenshot()
    _split(db, [[2]])
    before = _state(db)
    again = _split(db, [[2]])
    assert (again["moved"], again["rulings"]["written"], again["undo"]) == (0, 0, None)
    assert _state(db) == before
    assert [u["property_id"] for u in again["units"]] == [10, 20]
    confirm = _split(db, [], adverts=[1, 3])
    assert confirm["rulings"]["written"] == 0 and _state(db) == before


def test_a_newcomer_on_the_property_or_an_advert_elsewhere_is_stale_and_writes_nothing():
    db = _screenshot()
    db.listings[4] = 10                     # the lane merged 4 in after the page loaded
    before = _state(db)
    stale = _refused(_split, db, [[2]], adverts=[1, 2, 3])
    assert (stale.status, stale.code, stale.ids) == (409, "stale", [4])
    assert _state(db) == before
    # a named advert on a property holding an advert nobody named: no back-door merge
    db = _screenshot()
    db.listings.update({3: 70, 7: 70})
    db.props[70] = "active"
    assert _refused(_split, db, [], adverts=[1, 2, 3]).code == "stale"
    # the newcomer lands between the unlocked read and the lock: the locked re-read decides
    db = _screenshot()
    dispatch = db.dispatch
    db.dispatch = lambda s, p: (db.listings.update({4: 10}) if "FOR UPDATE" in s
                                else None) or dispatch(s, p)
    before = _state(db)
    assert _refused(_split, db, [[2]], adverts=[1, 2, 3]).code == "stale"
    assert _state(db) == before and not db.sql("UPDATE listings"), "refused under the lock"


def test_confirming_over_my_own_veto_asks_first_and_names_the_pairs():
    db = _screenshot()
    db.rule(1, 3, "different", note="earlier")
    before = _state(db)
    asked = _refused(_split, db, [[2]])
    assert (asked.status, asked.code, asked.ids) == (409, "reverses_rulings", [[1, 3]])
    assert "1-3" in asked.message and _state(db) == before
    out = _split(db, [[2]], confirm_retract=True)
    assert out["reversed_pairs"] == [[1, 3]] and db.word(1, 3)[0] == "same"
    assert out["undo"]["rulings"][1] == {
        "listing_lo": 1, "listing_hi": 3, "verdict": "different", "note": "earlier", "reasons": [],
        "must_not_link": {"source": "operator", "reason": "earlier"}}


def test_the_e52_helper_is_the_one_both_verdict_split_routes_call():
    import api.routes.autodedup as routes

    assert routes.reversed_pairs is ps.reversed_pairs
    assert routes.operator_pair_verdicts is ps.operator_pair_verdicts
    assert not hasattr(routes, "_split_summary")
    src = inspect.getsource(routes)
    assert src.count("operator_pair_verdicts(conn, member_ids") == 2
    assert ps.reversed_pairs({(1, 2): "different", (1, 3): "same", (2, 3): None},
                             [(1, 2), (1, 3), (2, 3)]) == [(1, 2)]


def test_an_advert_that_cannot_move_refuses_and_rolls_back_every_detach_before_it():
    db = _screenshot()
    dispatch = db.dispatch

    def refuse_three(s: str, p: Any) -> list[tuple]:
        if s.startswith("UPDATE listings SET property_id = %s WHERE id = %s AND") and p[1] == 3:
            db.count = 0
            return []
        return dispatch(s, p)

    db.dispatch = refuse_three
    before = _state(db)
    stuck = _refused(_split, db, [[2], [3]])
    assert (stuck.code, stuck.ids) == ("cannot_move", [{"listing_id": 3, "outcome": "moved_since"}])
    db.dispatch = dispatch
    assert _state(db) == before, "the detach of 2 rolled back with the rest"
    # planned so before any lock: an origin a later merge retired elsewhere
    db = _Ledger({1: 10, 31: 30, 32: 30, 9: 5}, first_seen={5: T0 - timedelta(days=9)})
    merge_property_set(db, [10, 30], source="operator", reason="r", decided_by=OP)
    ps.detach_listing(db, 31, decided_by=OP)
    merge_property_set(db, [5, 30], source="autodedup", reason="r")
    assert _refused(_split, db, [[32]], adverts=[1, 32]).ids == [
        {"listing_id": 32, "outcome": "origin_moved_on"}]


def test_the_unit_holding_the_own_advert_keeps_the_record_when_the_kept_unit_holds_none():
    db = _screenshot()
    out = _split(db, [[1]])
    assert out["record_kept_by"] == "B" and db.listings[1] == 10
    assert db.listings[2] == db.listings[3] == out["units"][0]["property_id"] == 20
    assert out["units"][0]["merge_group_id"] is not None and db.props[30] == "merged_away"
    assert db.word(2, 3)[0] == "same" and db.word(1, 2)[0] == db.word(1, 3)[0] == "different"
    # without keep_together the property's own advert cannot leave its last
    db = _screenshot()
    refused = _refused(_split, db, [[1]], keep_together=False)
    assert (refused.code, refused.ids) == ("cannot_move", [{"listing_id": 1,
                                                             "outcome": "last_native"}])


def test_a_join_the_one_merge_refuses_is_refused_and_nothing_moves():
    db = _Ledger({1: 10, 2: 10, 3: 10}, cats={12: ("pronajem", "byt")})
    before = _state(db)
    refused = _refused(_split, db, [[2, 3]])
    assert refused.code == "refused" and "category_type" in refused.message
    assert _state(db) == before
    db = _screenshot()
    db.assets.update({20: 7, 30: 8})
    before = _state(db)
    assert _refused(_split, db, [[2, 3]]).code == "refused" and _state(db) == before


def test_a_join_that_would_drag_an_advert_nobody_named_is_refused():
    db = _Ledger({1: 10, 2: 20, 4: 20, 3: 30})
    merge_property_set(db, [10, 20, 30], source="autodedup", reason="r")
    ps.detach_listing(db, 4, decided_by=OP)            # 20 is back, holding 4
    before = _state(db)
    dragged = _refused(_split, db, [[2, 3]], adverts=[1, 2, 3])
    assert (dragged.code, dragged.ids) == ("join_would_drag", [4]) and _state(db) == before


def test_two_units_that_came_from_one_property_are_refused_not_sent_home_together():
    """Review finding 1: a legacy ingest-grouped P={2,3} merged into 10; [[2],[3]] would put both
    back on 20 while ruling them `different` with an operator must-not-link."""
    db = _Ledger({1: 10, 2: 20, 3: 20})
    merge_property_set(db, [10, 20], source="autodedup", reason="r")
    db.log.clear()
    before = _state(db)
    refused = _refused(_split, db, [[2], [3]])
    assert (refused.status, refused.code) == (409, "cannot_move")
    assert refused.ids == [{"listing_id": 2, "outcome": "shared_origin"},
                           {"listing_id": 3, "outcome": "shared_origin"}]
    assert _state(db) == before and not db.sql("UPDATE listings")
    # the keeper swap: the kept unit [2] and the separated [3] both go home to 20
    assert _refused(_split, db, [[1], [3]]).ids == [
        {"listing_id": 2, "outcome": "shared_origin"}, {"listing_id": 3, "outcome": "shared_origin"}]
    assert _state(db) == before
    # both in ONE unit is one record, as stated
    out = _split(db, [[2, 3]])
    assert db.listings == {1: 10, 2: 20, 3: 20} and out["units"][1]["merge_group_id"] is None
    assert db.word(2, 3)[0] == "same" and (2, 3) not in db.mnl


def test_an_origin_holding_another_units_or_an_unnamed_advert_is_refused():
    db = _Ledger({1: 10, 2: 20, 3: 20})
    merge_property_set(db, [10, 20], source="autodedup", reason="r")
    ps.detach_listing(db, 3, decided_by=OTHER)          # 20 is back, holding 3
    before = _state(db)
    refused = _refused(_split, db, [[2]], adverts=[1, 2, 3])    # 3 stays in the kept unit
    assert (refused.code, refused.ids) == ("cannot_move", [{"listing_id": 2,
                                                             "outcome": "shared_origin"}])
    assert _state(db) == before
    # review finding 4: a unit of ONE advert going home to an advert nobody named
    dragged = _refused(_split, db, [[2]], adverts=[1, 2])
    assert (dragged.code, dragged.ids) == ("join_would_drag", [3]) and _state(db) == before
    # named and in the same unit, it is that unit's record
    out = _split(db, [[2, 3]], adverts=[1, 2, 3])
    assert db.listings == {1: 10, 2: 20, 3: 20} and out["units"][1]["property_id"] == 20


def test_a_machine_veto_survives_the_split_and_its_undo():
    """Review finding 2: `different` rewrites a guard row as the operator's, and the undo's
    `unsure` used to delete it; `same` (a group leaving together, the keeper swap) keeps it."""
    db = _Ledger({1: 10, 2: 20})
    merge_property_set(db, [10, 20], source="autodedup", reason="r")
    db.mnl[(1, 2)] = ("guard", "floor 3 vs 7")
    out = _split(db, [[2]])
    assert db.mnl[(1, 2)][0] == "operator"
    assert out["undo"]["rulings"][0]["must_not_link"] == {"source": "guard",
                                                          "reason": "floor 3 vs 7"}
    undo = out["undo"]
    undo_split(db, 10, call_id=undo["call_id"], placements=undo["placements"],
               rulings=undo["rulings"], decided_by=OP)
    assert db.mnl == {(1, 2): ("guard", "floor 3 vs 7")} and db.word(1, 2)[0] == "unsure"
    # a group leaving together: its interim `different` is not the last word on the veto
    db = _screenshot()
    db.mnl[(2, 3)] = ("model", "m")
    _split(db, [[2, 3]])
    assert db.word(2, 3)[0] == "same" and db.mnl[(2, 3)] == ("model", "m")
    # the keeper swap: the kept unit's adverts go home one by one, then are ruled `same`
    db = _screenshot()
    db.mnl[(2, 3)] = ("llm", "l")
    out = _split(db, [[1]])
    assert out["record_kept_by"] == "B" and db.word(2, 3)[0] == "same"
    assert db.mnl[(2, 3)] == ("llm", "l")
    undo = out["undo"]
    undo_split(db, 10, call_id=undo["call_id"], placements=undo["placements"],
               rulings=undo["rulings"], decided_by=OP)
    assert db.mnl == {(2, 3): ("llm", "l")}


def test_an_undo_body_cannot_forge_a_machine_veto():
    db = _screenshot()
    undo = _split(db, [[2]])["undo"]
    forged = [dict(r) for r in undo["rulings"]]
    forged[1]["must_not_link"] = {"source": "guard", "reason": "forged"}     # (1, 3): ruled same
    before = _state(db)
    kw = {"call_id": undo["call_id"], "placements": undo["placements"], "decided_by": OP}
    refused = _refused(undo_split, db, 10, rulings=forged, **kw)
    assert (refused.code, refused.ids) == ("stale", [[1, 3]]) and _state(db) == before
    forged[1]["must_not_link"] = {"source": "robot", "reason": None}
    assert _refused(undo_split, db, 10, rulings=forged, **kw).status == 400
    # over the split's own operator row it is the pair's previous row, put back
    forged = [dict(r) for r in undo["rulings"]]
    forged[0]["must_not_link"] = {"source": "guard", "reason": "g"}        # (1, 2): ruled different
    undo_split(db, 10, rulings=forged, **kw)
    assert db.mnl == {(1, 2): ("guard", "g")}


def test_the_undo_rejoins_the_adverts_and_restores_every_pairs_previous_word():
    db = _screenshot()
    db.rule(1, 2, "different", note="old", reasons=["plocha"])
    out = _split(db, [[2]])
    undo = out["undo"]
    done = undo_split(db, 10, call_id=undo["call_id"], placements=undo["placements"],
                      rulings=undo["rulings"], decided_by=OP)
    assert set(db.listings.values()) == {10} and db.props[20] == "merged_away"
    assert done["property_id"] == 10 and done["undone"] and done["merge_group_id"]
    assert db.verdicts[(1, 2, OP)]["reasons"] == ["plocha"] and db.word(1, 2) == ("different",
                                                                                  "old")
    undone = f"operator split-undo {undo['call_id']}"
    assert db.word(1, 3) == db.word(2, 3) == ("unsure", undone)
    assert set(db.mnl) == {(1, 2)}, "unsure retracts; the restored negative vetoes again"
    assert db.mnl[(1, 2)] == ("operator", "old")
    # the undo's own words do not carry the split's prefix, so the same body is refused again
    before = _state(db)
    replay = _refused(undo_split, db, 10, call_id=undo["call_id"], placements=undo["placements"],
                      rulings=undo["rulings"], decided_by=OP)
    assert (replay.code, replay.ids) == ("stale", [[1, 2], [1, 3], [2, 3]])
    assert _state(db) == before


def test_the_undo_refuses_what_was_ruled_or_moved_again_and_follows_decision_17():
    db = _screenshot()
    undo = _split(db, [[2]])["undo"]
    db.rule(1, 3, "different", note="later")
    kw = {"call_id": undo["call_id"], "placements": undo["placements"],
          "rulings": undo["rulings"], "decided_by": OP}
    assert _refused(undo_split, db, 10, **kw).code == "stale"
    db = _screenshot()
    undo = _split(db, [[2]])["undo"]
    db.listings[2] = 30
    assert _refused(undo_split, db, 10, **{**kw, "call_id": undo["call_id"],
                                           "rulings": undo["rulings"]}).code == "stale"
    # the separated advert's record is the oldest: the re-join keeps it
    db = _screenshot(first_seen={20: T0 - timedelta(days=5)})
    undo = _split(db, [[2]])["undo"]
    done = undo_split(db, 10, call_id=undo["call_id"], placements=undo["placements"],
                      rulings=undo["rulings"], decided_by=OP)
    assert done["property_id"] == 20 and set(db.listings.values()) == {20}
    assert db.props[10] == "merged_away"
    # a forged call id matches nothing
    assert _refused(undo_split, db, 20, **{**kw, "call_id": "operator"}).status == 400


def test_the_statement_is_validated_before_anything_is_read():
    db = _screenshot()
    for separate, adverts, why in (
        ([[1, 2, 3]], [1, 2, 3], "one unit must stay"),
        ([[2], [2]], [1, 2, 3], "two separated groups"),
        ([[]], [1, 2, 3], "cannot be empty"),
        ([[9]], [1, 2, 3], "not among the adverts"),
        ([], [1, 1, 2], "named twice"),
        ([[i] for i in range(2, 28)], list(range(1, 28)), "at most 26 units"),
        ([[2] * 50_000], [1, 2, 3], "a separated group names at most 100"),
        ([[2] * 100], [1, 2, 3], "two separated groups"),
        ([list(range(1, 103))], list(range(1, 103)), "adverts names 1 to 100"),
    ):
        refused = _refused(_split, db, separate, adverts=adverts)
        assert (refused.status, refused.code) == (400, "invalid") and why in refused.message
    assert _refused(_split, db, [], keep_together=False).status == 400
    assert db.log == []


def test_a_lock_timeout_or_deadlock_is_busy():
    import psycopg

    db = _screenshot()
    dispatch = db.dispatch

    def locked(s: str, p: Any) -> list[tuple]:
        if "FOR UPDATE" in s:
            raise psycopg.errors.LockNotAvailable("lock timeout")
        return dispatch(s, p)

    db.dispatch = locked
    before = _state(db)
    assert _refused(_split, db, [[2]]).code == "busy" and _state(db) == before


def test_the_statement_writes_only_through_the_chokepoint():
    """Rule 15: the split moves adverts through `detach_listing` / `merge_property_set` and rules
    through `record_rulings`, and holds no write statement of its own."""
    src = inspect.getsource(ps)
    for statement in ("UPDATE listings", "UPDATE properties", "INSERT INTO property_merge_events",
                      "INSERT INTO autodedup", "DELETE FROM", "INSERT INTO properties",
                      "SELECT id, property_id FROM listings WHERE id"):
        assert statement not in src, statement
    for writer in ("detach_listing(", "merge_property_set(", "record_rulings(",
                   "restore_must_not_link("):
        assert writer in inspect.getsource(ps.split_property) + inspect.getsource(ps._join)
    assert ps.listing_places is pi.listing_places and not hasattr(ps, "_PLACES_SQL")


def test_one_pair_verdict_vocabulary():
    """Review finding 5: the store's vocabulary is `ui_sql.VERDICT_VALUES`, once."""
    from autodedup import ui_sql as usql

    assert not hasattr(pi, "PAIR_VERDICTS")
    assert set(usql.VERDICT_VALUES) == {"same", "unsure", *usql.NEGATIVE_VERDICTS}
    with pytest.raises(ValueError):
        pi.record_rulings(_Ledger({1: 10}), {(1, 2)}, verdict="maybe", decided_by=OP, note=None)
    assert _refused(undo_split, _screenshot(), 10, call_id=str(uuid.uuid4()), placements={},
                    rulings=[{"listing_lo": 1, "listing_hi": 2, "verdict": "maybe"}],
                    decided_by=OP).status == 400


def test_the_property_page_row_split_writes_exactly_the_detach_rulings():
    db = _screenshot()
    out = _split(db, [[2]], keep_together=False)
    assert db.listings == {1: 10, 2: 20, 3: 10}
    assert db.word(1, 2)[0] == db.word(2, 3)[0] == "different" and db.word(1, 3) is None
    assert out["rulings"] == {"written": 2, "same": 0, "different": 2,
                              "must_not_link_written": 2, "must_not_link_retracted": 0}


# --- the route -------------------------------------------------------------------------------


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


def test_the_route_states_the_split_and_posts_its_undo_back(client):
    http, db = client
    res = http.post("/properties/10/split", json={
        "adverts": [1, 2, 3], "separate": [[2]], "keep_together": True, "reason": " jiné patro "})
    assert res.status_code == 200
    body = res.json()
    assert [(u["unit"], u["property_id"]) for u in body["units"]] == [("A", 10), ("B", 20)]
    assert body["undo"]["placements"] == {"1": 10, "2": 20, "3": 10}
    assert db.word(1, 3)[1].endswith("· jiné patro")
    # a stale property id follows merged_into; the re-send moves nothing
    again = http.post("/properties/10/split", json={
        "adverts": [1, 2, 3], "separate": [[2]], "keep_together": True})
    assert again.status_code == 200 and again.json()["moved"] == 0
    undone = http.post("/properties/10/split", json={"undo": body["undo"]})
    assert undone.status_code == 200 and undone.json()["property_id"] == 10
    assert set(db.listings.values()) == {10}


def test_the_route_vocabulary_is_the_stores(client):
    import api.routes.autodedup as routes
    from autodedup import ui_sql as usql

    assert routes.VERDICT_VALUES is usql.VERDICT_VALUES


def test_the_route_answers_refusals_with_code_message_and_ids(client):
    from api import dependencies as deps
    from api import main as api_main

    http, db = client
    db.rule(1, 3, "different")
    res = http.post("/properties/10/split", json={
        "adverts": [1, 2, 3], "separate": [[2]], "keep_together": True})
    assert res.status_code == 409
    assert res.json()["detail"] == {"code": "reverses_rulings", "message": res.json()["detail"][
        "message"], "ids": [[1, 3]]}
    body = {"adverts": [1, 2, 3], "separate": [[2]], "keep_together": True}
    assert http.post("/properties/404/split", json=body).status_code == 404
    assert http.post("/properties/10/split", json={**body, "adverts": [1, 2, 3, 99],
                                                   "separate": [[99]]}).status_code == 404
    assert http.post("/properties/10/split", json={**body, "reason": "x" * 501}).status_code == 422
    assert http.post("/properties/10/split", json={"adverts": [1, 2, 3]}).status_code == 400
    assert http.post("/properties/10/split", json={**body, "separate": [[1, 2, 3]]}).status_code == 400
    assert http.post("/properties/10/split", json={
        "adverts": [1, 2, 3], "undo": {"call_id": "x", "placements": {}, "rulings": []}}
    ).status_code == 400
    assert http.post("/properties/10/detach", json={"listing_id": 2}).status_code in (404, 405)
    for bounded in ({"adverts": list(range(1, 102))}, {"separate": [[2]] * 26},
                    {"separate": [[2] * 101]}):
        assert http.post("/properties/10/split", json={**body, **bounded}).status_code == 422
    assert http.post("/properties/10/split", json={"undo": {
        "call_id": "x", "placements": {}, "rulings": [
            {"listing_lo": 1, "listing_hi": 2, "must_not_link": {"source": "robot"}}]}}
    ).status_code == 422
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    assert http.post("/properties/10/split", json=body).status_code == 403
    assert db.listings == {1: 10, 2: 10, 3: 10}
