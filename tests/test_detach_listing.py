"""The one undo: `toolkit.property_identity.detach_listing` and the routes over it (decision 8).

`_Ledger` is a stateful fake of `listings.property_id`, `properties` and the merge ledger that
serves the statements the set merge and the detach run, so a merge and its undo replay end to
end here (and in tests/test_property_merge_set.py); executed: tests/test_merge_safety_live.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import toolkit.property_identity as pi
from toolkit.property_identity import MergeError, detach_listing, merge_property_set

OP = "operator@example.com"
T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _Tx:
    def __init__(self, db: "_Ledger") -> None:
        self.db = db

    def __enter__(self) -> "_Tx":
        self.saved = (dict(self.db.listings), dict(self.db.props), [dict(e) for e in self.db.events],
                      dict(self.db.into), dict(self.db.assets))
        return self

    def __exit__(self, exc_type: Any, *exc: Any) -> bool:
        if exc_type is not None:
            (self.db.listings, self.db.props, self.db.events, self.db.into,
             self.db.assets) = self.saved
            self.db.log.append(("rollback", None))
        return False


class _Cur:
    def __init__(self, db: "_Ledger") -> None:
        self.db, self.rows, self.rowcount = db, [], 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self.db.log.append((s, params))
        self.db.count = None
        self.rows = self.db.dispatch(s, params)
        self.rowcount = len(self.rows) if self.db.count is None else self.db.count

    def executemany(self, sql: str, seq: Any) -> None:
        for params in seq:
            self.execute(sql, params)

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple]:
        return list(self.rows)


class _Ledger:
    """listings: id -> property_id; props: id -> status; into: id -> merged_into; first_seen /
    assets / cats / canonical: per property; events: the merge ledger; asset_events: the asset
    membership log."""

    def __init__(self, listings: dict[int, int], *, first_seen: dict[int, datetime] | None = None,
                 assets: dict[int, int] | None = None, canonical: dict[int, int] | None = None,
                 props: dict[int, str] | None = None,
                 cats: dict[int, tuple[str | None, str | None]] | None = None) -> None:
        self.listings = dict(listings)
        self.props = {pid: "active" for pid in set(listings.values())} | (props or {})
        self.into: dict[int, int] = {}
        self.first_seen, self.assets = first_seen or {}, dict(assets or {})
        self.cats = cats or {}
        self.canonical = canonical or {}
        self.events: list[dict[str, Any]] = []
        self.asset_events: list[tuple[int, int, str, str]] = []
        self.log: list[tuple[str, Any]] = []
        self.count: int | None = None

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Tx:
        return _Tx(self)

    def sql(self, needle: str) -> list[Any]:
        return [p for s, p in self.log if needle in s]

    def dispatch(self, s: str, p: Any) -> list[tuple]:  # noqa: C901
        cat = lambda pid: self.cats.get(pid, ("prodej", "byt"))  # noqa: E731
        if s.startswith("SELECT id, status, first_seen_at, asset_id, category_type"):
            return [(pid, self.props[pid], self.first_seen.get(pid, T0), self.assets.get(pid),
                     *cat(pid)) for pid in sorted(p["ids"]) if pid in self.props]
        if s.startswith("SELECT id, status, category_type, category_main, asset_id"):
            return [(pid, self.props[pid], *cat(pid), self.assets.get(pid)) for pid in p]
        if s.startswith("SELECT id, status, merged_into FROM properties"):
            return [(pid, self.props[pid], self.into.get(pid))
                    for pid in sorted(p["ids"]) if pid in self.props]
        if s.startswith("WITH moved AS ( UPDATE properties SET asset_id"):
            self._carry(p)
        if s.startswith("WITH carried AS ("):
            self._restore(p)
        if s.startswith("SELECT p.repr_listing_ref_id FROM properties p"):
            return [(self.canonical[pid],) for pid in p["ids"] if pid in self.canonical]
        if s.startswith("INSERT INTO property_merge_events") and "born" in p:
            self.events.append({"id": len(self.events) + 1, "group": p["group"],
                                "survivor": p["left"], "listing": p["listing"], "prev": p["born"],
                                "source": "operator", "undone_by": p["by"]})
            return [(len(self.events),)]
        if s.startswith("WITH born AS ( INSERT INTO properties"):
            born = []
            for lid in p["ids"]:
                if lid in self.listings and self.listings[lid] is None:
                    pid = max(self.props) + 1
                    self.props[pid], self.listings[lid] = "active", pid
                    born.append((pid,))
            return born
        if s.startswith("SELECT l.property_id, count(*), count(*) FILTER"):
            live = {e["listing"] for e in self.events if e["undone_by"] is None}
            sizes = {pid: [lid for lid, at in self.listings.items() if at == pid]
                     for pid in p["ids"]}
            return [(pid, len(ids), len(set(ids) - live)) for pid, ids in sizes.items() if ids]
        if s.startswith("INSERT INTO property_merge_events"):
            moved = sorted(lid for lid, pid in self.listings.items() if pid == p["retired"])
            self.events += [{"id": len(self.events) + i + 1, "group": p["group"],
                             "survivor": p["survivor"], "listing": lid, "prev": p["retired"],
                             "source": p["source"], "undone_by": None}
                            for i, lid in enumerate(moved)]
            self.count = len(moved)
        elif s == "UPDATE listings SET property_id = %s WHERE property_id = %s":
            self.listings = {lid: p[0] if pid == p[1] else pid for lid, pid in self.listings.items()}
        elif s.startswith("UPDATE properties SET status = 'merged_away'"):
            self.props[p[1]], self.into[p[1]] = "merged_away", p[0]
        elif s.startswith("SELECT id, property_id FROM listings WHERE id = ANY("):
            return [(lid, self.listings[lid]) for lid in p["ids"] if lid in self.listings]
        elif s == "SELECT property_id FROM listings WHERE id = %s":
            return [(self.listings[p[0]],)] if p[0] in self.listings else []
        elif s.startswith("SELECT e.listing_ref_id, e.id, e.merge_group_id::text"):
            return [(e["listing"], e["id"], e["group"], e["survivor"], e["prev"], e["source"], T0)
                    for e in sorted(self.events, key=lambda e: (e["listing"], e["id"]))
                    if e["listing"] in p["ids"] and e["undone_by"] is None]
        elif s.startswith("UPDATE listings SET property_id = %s WHERE id = %s AND"):
            self.count = int(self.listings.get(p[1]) == p[2])
            if self.count:
                self.listings[p[1]] = p[0]
        elif s.startswith("UPDATE property_merge_events SET undone_at = now()"):
            for e in self.events:
                if e["id"] in p[1] and e["undone_by"] is None:
                    e["undone_by"] = p[0]
        elif s.startswith("UPDATE properties p SET status = 'active'"):
            self.count = int(self.props.get(p["pid"]) == "merged_away")
            if self.count:
                self.props[p["pid"]] = "active"
                self.into.pop(p["pid"], None)
        elif s == "SELECT id FROM listings WHERE property_id = %s":
            return [(lid,) for lid, pid in sorted(self.listings.items()) if pid == p[0]]
        return []

    def _carry(self, p: dict[str, Any]) -> None:
        for pid, want in ((p["survivor"], p["asset"]), (p["retired"], None)):
            if self.assets.get(pid) != want:
                self.assets[pid] = want
                self.asset_events.append((p["asset"], pid, "linked" if want else "unlinked",
                                          p["reason"]))

    def _restore(self, p: dict[str, Any]) -> None:
        carried = [a for a, pid, act, why in self.asset_events
                   if pid == p["restored"] and act == "unlinked" and why == p["merge"]]
        holders = [h for h in p["path"] if carried and self.assets.get(h) == carried[-1]]
        if not holders:
            return
        asset = carried[-1]
        moved = [(p["restored"], asset)] if self.assets.get(p["restored"]) is None else []
        moved += [(h, None) for h in holders
                  if any((asset, h, "linked", m) in self.asset_events for m in p["merges"])]
        for pid, want in moved:
            self.assets[pid] = want
            self.asset_events.append((asset, pid, "linked" if want else "unlinked", p["detach"]))


def _merged(db: _Ledger, ids: list[int], *, source: str = "operator") -> dict[str, Any]:
    return merge_property_set(db, ids, source=source, reason="manual_subset",
                              decided_by=OP)["data"]


def _verdicts(db: _Ledger) -> list[tuple[int, int, str, str]]:
    return [(r["listing_lo"], r["listing_hi"], r["verdict"], r["note"])
            for r in db.sql("INSERT INTO autodedup.verdicts")]


def test_one_advert_goes_back_to_the_merged_away_property_it_came_from():
    db = _Ledger({1: 10, 2: 20, 3: 20})
    group = _merged(db, [10, 20])["merge_group_id"]
    out = detach_listing(db, 2, decided_by=OP, reason="jiné patro")["data"]
    assert out == {**out, "detached": True, "outcome": "detached", "listing_id": 2,
                   "survivor_property_id": 10, "restored_property_id": 20,
                   "reactivated": True, "merge_group_ids": [group]}
    assert db.listings == {1: 10, 2: 20, 3: 10} and db.props[20] == "active"
    # the ledger keeps every row; the restored property's card comes from THAT merge's snapshot
    assert [(e["listing"], e["undone_by"]) for e in db.events] == [(2, OP), (3, None)]
    assert db.sql("INSERT INTO property_pipeline (account_id") == [{"g": group, "r": 20, "s": 10}]
    # its sibling joins it without reactivating (or restoring) anything a second time
    assert not detach_listing(db, 3, decided_by=OP)["data"]["reactivated"]
    assert db.listings == {1: 10, 2: 20, 3: 20}
    assert len(db.sql("INSERT INTO property_pipeline (account_id")) == 1


def test_a_second_detach_and_a_native_advert_are_no_ops_that_say_so():
    db = _Ledger({1: 10, 2: 20})
    _merged(db, [10, 20])
    detach_listing(db, 2, decided_by=OP)
    rulings = len(_verdicts(db))
    for lid, where in ((2, 20), (1, 10)):
        again = detach_listing(db, lid, decided_by=OP)["data"]
        assert (again["detached"], again["outcome"], again["rulings_written"]) == (
            False, "not_merged", 0)
        assert again["restored_property_id"] == again["survivor_property_id"] == where
    assert len(_verdicts(db)) == rulings


def test_an_advert_goes_back_to_its_origin_across_a_chain_of_merges():
    """2 came from 20 into 10, then 10 merged into the older 5: the advert returns to the
    record it sat on before every merge that still stands."""
    db = _Ledger({1: 10, 2: 20, 9: 5}, first_seen={5: T0 - timedelta(days=9)})
    _merged(db, [10, 20])
    _merged(db, [5, 10], source="autodedup")
    # with the merge that took each advert from its origin: the oldest that stands
    assert pi.listing_origins(db, [1, 2, 9]) == {1: (10, "autodedup", T0),
                                                 2: (20, "operator", T0)}
    out = detach_listing(db, 2, decided_by=OP)["data"]
    assert out["restored_property_id"] == 20 and len(out["merge_group_ids"]) == 2
    assert db.listings == {1: 5, 2: 20, 9: 5}
    assert [e["undone_by"] for e in db.events if e["listing"] == 2] == [OP, OP]
    # 5 never held the card of the merge that retired 20: restored, never cleaned off 5
    assert [r["s"] for r in db.sql("INSERT INTO property_pipeline (account_id")] == [None]
    assert db.sql("DELETE FROM property_pipeline WHERE property_id = %(s)s") == []


def test_a_group_scoped_detach_undoes_only_that_merge_while_it_is_the_newest():
    db = _Ledger({1: 10, 2: 20, 9: 5}, first_seen={5: T0 - timedelta(days=9)})
    first = _merged(db, [10, 20], source="autodedup")["merge_group_id"]
    second = _merged(db, [5, 10], source="autodedup")["merge_group_id"]
    scoped = {"decided_by": "autodedup-unapply:r", "source": "autodedup"}
    moved_on = detach_listing(db, 2, merge_group_id=first, **scoped)["data"]
    assert moved_on["outcome"] == "moved_on" and db.listings[2] == 5
    back = detach_listing(db, 2, merge_group_id=second, **scoped)["data"]
    assert back["restored_property_id"] == 10 and back["merge_group_ids"] == [second]
    assert db.props[10] == "active" and db.listings[2] == 10
    assert back["rulings_written"] == 0 and db.sql("autodedup.") == [], "an engine undo rules nothing"


def test_an_advert_moved_outside_the_ledger_stays_and_a_missing_one_is_an_error():
    db = _Ledger({1: 10, 2: 20, 7: 70})
    _merged(db, [10, 20])
    db.listings[2] = 70
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "moved_since"
    assert db.listings[2] == 70
    # moved by a concurrent merge between the ledger read and the property locks: left there
    db.listings[2], dispatch = 10, db.dispatch
    db.dispatch = lambda s, p: (db.listings.update({2: 70}) if "FOR UPDATE" in s
                                else None) or dispatch(s, p)
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "moved_since"
    assert db.listings[2] == 70 and db.sql("UPDATE property_merge_events SET undone_at") == []
    with pytest.raises(MergeError):
        detach_listing(db, 99, decided_by=OP)


def test_an_origin_a_later_merge_retired_elsewhere_is_left_merged_and_the_advert_stays():
    """x1, x2 were 30's own. {10, 30} merged; x1 detached (30 back); the engine then merged 30
    into the older 5. Detaching x2 from 10 would half-undo that merge and restore 30's card a
    second time: nothing moves, and the outcome says why (rule 22)."""
    db = _Ledger({1: 10, 31: 30, 32: 30, 9: 5}, first_seen={5: T0 - timedelta(days=9)})
    _merged(db, [10, 30])
    assert detach_listing(db, 31, decided_by=OP)["data"]["reactivated"]
    _merged(db, [5, 30], source="autodedup")
    restores = len(db.sql("INSERT INTO property_pipeline (account_id"))
    # x1 may go back to 30 from 5; x2 may not from 10 while that merge stands
    assert pi.detach_outcomes(db, [31, 32]) == {31: "detached", 32: "origin_moved_on"}
    out = detach_listing(db, 32, decided_by=OP)["data"]
    assert (out["detached"], out["outcome"], out["rulings_written"]) == (
        False, "origin_moved_on", 0)
    assert db.listings[32] == 10 and db.props[30] == "merged_away" and db.into[30] == 5
    assert len(db.sql("INSERT INTO property_pipeline (account_id")) == restores
    assert [e["undone_by"] for e in db.events if e["listing"] == 32] == [None]
    # retired elsewhere between the unlocked read and the lock: the lock's read decides
    dispatch = db.dispatch
    db.dispatch = lambda s, p: (db.into.update({30: 7}) if "FOR UPDATE" in s
                                else None) or dispatch(s, p)
    assert detach_listing(db, 31, decided_by=OP)["data"]["outcome"] == "origin_moved_on"
    assert db.listings[31] == 5


@pytest.mark.parametrize("same_asset", [False, True])
def test_the_asset_link_a_merge_carried_goes_back_with_its_property(same_asset):
    """Decision 17's carry, inverted: 20 (asset 7) merged into the older 10; the detach that
    brings 20 back gives it its link, and 10 keeps one only if it held it before the merge."""
    db = _Ledger({1: 10, 2: 20, 3: 20}, assets={20: 7, **({10: 7} if same_asset else {})})
    group = _merged(db, [10, 20])["merge_group_id"]
    assert db.assets == {10: 7, 20: None}
    detach_listing(db, 2, decided_by=OP)
    assert db.assets == {10: 7 if same_asset else None, 20: 7}
    assert (7, 20, "linked", f"detach {group}") in db.asset_events
    detach_listing(db, 3, decided_by=OP)
    assert db.assets == {10: 7 if same_asset else None, 20: 7}


def test_an_operator_detach_rules_the_advert_different_from_every_advert_that_stays():
    db = _Ledger({1: 10, 2: 10, 3: 20})
    _merged(db, [10, 20])
    db.log.clear()
    out = detach_listing(db, 3, decided_by=OP, reason="jiné patro")["data"]
    note = "operator detach from 10: jiné patro"
    assert _verdicts(db) == [(1, 3, "different", note), (2, 3, "different", note)]
    assert out["rulings_written"] == 2
    assert {(r["listing_lo"], r["listing_hi"]) for r in
            db.sql("INSERT INTO autodedup.must_not_link")} == {(1, 3), (2, 3)}
    assert {r["decided_by"] for r in db.sql("INSERT INTO autodedup.verdicts")} == {OP}


def test_a_detach_leaves_curation_where_it_is_and_recomputes_both_once():
    db = _Ledger({1: 10, 2: 20})
    _merged(db, [10, 20])
    db.log.clear()
    detach_listing(db, 2, decided_by=OP)
    written = " ".join(s for s, _p in db.log)
    for table in ("collection_properties", "property_tags", "property_notes",
                  "notification_dispatches", "property_dismissals", "property_status_events",
                  "DELETE FROM properties", "DELETE FROM property_merge_events"):
        assert table not in written, f"a detach touched {table}"
    assert [p["pid"] for p in db.sql("WITH batch AS")] == [10, 20]
    assert db.sql("DELETE FROM browse_list") == [([10, 20],)]
    (reactivate,) = [s for s, _p in db.log if "SET status = 'active'" in s]
    assert "merged_into = NULL, merged_at = NULL, is_active = EXISTS (" in reactivate


@pytest.mark.parametrize("backwards", [False, True])
def test_merging_then_detaching_every_advert_restores_every_original_property(backwards):
    """W3's gate at unit scale — a refactor of the mechanics changes no grouping: three set
    merges, one built on another; every advert a merge moved detached in turn, in either order,
    returns to its own record, and every asset link to its own property — 30's rode 30 -> 10 ->
    60 (the oldest)."""
    original = {1: 10, 2: 10, 3: 20, 4: 30, 5: 30, 6: 40, 7: 50, 8: 60, 9: 70, 11: 80,
                12: 80, 13: 20}
    links = {30: 7, 50: 8}
    db = _Ledger(original, first_seen={pid: T0 + timedelta(days=pid % 60)
                                       for pid in set(original.values())}, assets=links)
    _merged(db, [10, 20, 30])
    _merged(db, [40, 50], source="autodedup")
    _merged(db, [60, 10, 80])
    assert len(set(db.listings.values())) == 3 and db.assets[60] == 7
    for lid in sorted(pi.listing_origins(db, list(original)), reverse=backwards):
        detach_listing(db, lid, decided_by=OP)
    assert db.listings == original and set(db.props.values()) == {"active"}
    assert {pid: a for pid, a in db.assets.items() if a} == links
    assert all(e["undone_by"] for e in db.events), "a detach left a live ledger row behind"


# --- a native advert (grouped at ingest, no merge moved it) --------------------------------


def test_a_native_advert_is_born_a_new_record_through_the_one_birth_path():
    """1, 2, 3 were grouped at ingest: no ledger row moved any of them. Splitting 2 gives it a
    NEW record by `scraper.db.NEW_SINGLETONS_SQL` and writes ONE ledger row — the ingest grouping
    as the merge it amounts to (the new record into 10), closed as undone by this split."""
    from scraper.db import NEW_SINGLETONS_SQL

    db = _Ledger({1: 10, 2: 10, 3: 10})
    assert pi.detach_outcomes(db, [1, 2, 3]) == dict.fromkeys((1, 2, 3), "split_native")
    out = detach_listing(db, 2, decided_by=OP, reason="jiné patro")["data"]
    born = out["restored_property_id"]
    assert born not in (None, 10) and db.listings == {1: 10, 2: born, 3: 10}
    assert out == {**out, "detached": True, "outcome": "split_native", "listing_id": 2,
                   "survivor_property_id": 10, "reactivated": False, "rulings_written": 2}
    (birth,) = [(s, p) for s, p in db.log if "INSERT INTO properties" in s]
    assert birth == (" ".join(NEW_SINGLETONS_SQL.split()), {"ids": [2]})
    (row,) = db.events
    assert row == {**row, "survivor": 10, "prev": born, "listing": 2, "source": "operator",
                   "undone_by": OP}
    assert out["merge_group_ids"] == [row["group"]]
    (written,) = [p for s, p in db.log if s.startswith("INSERT INTO property_merge_events")]
    assert (written["left"], written["born"], written["by"]) == (10, born, OP)
    # the advert's origin is its new record: no live row, alone there -> a second split is a no-op
    assert pi.listing_origins(db, [2]) == {} and pi.detach_outcomes(db, [2]) == {2: "not_merged"}
    note = "operator detach from 10: jiné patro"
    assert _verdicts(db) == [(1, 2, "different", note), (2, 3, "different", note)]
    again = detach_listing(db, 2, decided_by=OP)["data"]
    assert (again["detached"], again["outcome"], again["restored_property_id"]) == (
        False, "not_merged", born)
    assert len(db.events) == 1 and len(_verdicts(db)) == 2


def test_a_native_split_takes_the_merges_lock_order_and_leaves_curation_behind():
    db = _Ledger({1: 10, 2: 10})
    detach_listing(db, 2, decided_by=OP)
    order = [s for s, _p in db.log]
    lock = next(i for i, s in enumerate(order) if s.endswith("FOR UPDATE"))
    move = next(i for i, s in enumerate(order) if s.startswith("UPDATE listings SET property_id"))
    assert lock < move < next(i for i, s in enumerate(order) if "INSERT INTO properties" in s)
    assert db.sql("SELECT id, status, merged_into FROM properties")[-1] == {"ids": [10]}
    written = " ".join(order)
    for table in ("collection_properties", "property_tags", "property_notes",
                  "notification_dispatches", "property_dismissals", "property_pipeline",
                  "asset_membership_events", "DELETE FROM properties"):
        assert table not in written, f"a native split touched {table} (rules 18, 22)"
    born = db.listings[2]
    assert [p["pid"] for p in db.sql("WITH batch AS")] == [10, born]
    assert db.sql("DELETE FROM browse_list") == [([10, born],)]


def test_a_split_advert_merged_back_is_detached_to_its_new_record():
    """The round trip: the operator (or the engine) merges the two records again through the
    one merge; a detach then returns the advert to the record it was born on."""
    db = _Ledger({1: 10, 2: 10})
    born = detach_listing(db, 2, decided_by=OP)["data"]["restored_property_id"]
    merged = _merged(db, [10, born], source="autodedup")
    assert merged["survivor_id"] == 10 and db.listings == {1: 10, 2: 10}
    assert pi.listing_origins(db, [1, 2]) == {2: (born, "autodedup", T0)}
    back = detach_listing(db, 2, decided_by=OP)["data"]
    assert (back["outcome"], back["restored_property_id"], back["reactivated"]) == (
        "detached", born, True)
    assert db.listings == {1: 10, 2: born} and db.props[born] == "active"


def test_only_the_operator_splits_a_native_advert_and_a_group_undo_never_does():
    """Decision 9: an engine caller gets `propose_only` — nothing born, nothing written — and a
    group-scoped undo leaves a native advert where it is."""
    db = _Ledger({1: 10, 2: 10, 3: 30})
    group = _merged(db, [10, 30], source="autodedup")["merge_group_id"]
    scoped = {"decided_by": "autodedup-unapply:r", "source": "autodedup"}
    assert pi.detach_outcomes(db, [1, 2], merge_group_id=group) == {1: "not_merged",
                                                                    2: "not_merged"}
    assert detach_listing(db, 1, merge_group_id=group, **scoped)["data"]["outcome"] == "not_merged"
    events, db.log[:] = list(db.events), []
    out = detach_listing(db, 1, decided_by="autodedup-reconcile", source="autodedup")["data"]
    assert (out["detached"], out["outcome"], out["restored_property_id"]) == (
        False, "propose_only", 10)
    assert db.events == events and db.listings == {1: 10, 2: 10, 3: 10}
    assert not any("INSERT INTO properties" in s or s.startswith("UPDATE") for s, _p in db.log)
    assert detach_listing(db, 1, decided_by=OP)["data"]["outcome"] == "split_native"


def test_the_last_own_advert_stays_while_merged_adverts_share_its_property():
    """The review's reproduction: 10 = {1 its own, 2 merged from 20}. Splitting 1 would leave 10
    with only 2, and 2's detach (the operator's, or `unapply`'s group loop) would then leave 10
    active with no advert: a ghost card holding its notes and pipeline card. So 1 stays
    (`last_native`) and the merged advert goes home instead; the property keeps its own."""
    for source in ("operator", "autodedup"):
        db = _Ledger({1: 10, 2: 20})
        group = _merged(db, [10, 20], source=source)["merge_group_id"]
        assert pi.detach_outcomes(db, [1, 2]) == {1: "last_native", 2: "detached"}
        out = detach_listing(db, 1, decided_by=OP)["data"]
        assert (out["detached"], out["outcome"], out["rulings_written"]) == (
            False, "last_native", 0)
        assert db.listings == {1: 10, 2: 10} and len(db.events) == 1
        undo = {"merge_group_id": group, "source": "autodedup"} if source == "autodedup" else {}
        assert detach_listing(db, 2, decided_by=OP, **undo)["data"]["outcome"] == "detached"
        assert db.listings == {1: 10, 2: 20} and db.props == {10: "active", 20: "active"}
        assert pi.detach_outcomes(db, [1]) == {1: "not_merged"}


def test_a_native_split_replans_under_the_lock():
    """Between the unlocked plan and the property lock, the only sibling left, the other own
    advert turned out merged in, or the advert itself moved: nothing is born, nothing ruled."""
    db = _Ledger({1: 10, 2: 10, 7: 70})
    dispatch = db.dispatch

    def meanwhile(change: Any) -> None:
        db.dispatch = lambda s, p: (change() if "FOR UPDATE" in s else None) or dispatch(s, p)

    meanwhile(lambda: db.listings.update({1: 70}))
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "not_merged"
    db.listings[1] = 10
    meanwhile(lambda: db.events.append({"id": 99, "group": "g", "survivor": 10, "listing": 1,
                                        "prev": 70, "source": "operator", "undone_by": None}))
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "last_native"
    db.events.clear()
    meanwhile(lambda: db.listings.update({2: 70}))
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "moved_since"
    assert db.events == [] and _verdicts(db) == [] and set(db.props) == {10, 70}


# --- the routes --------------------------------------------------------------------------


fastapi = pytest.importorskip("fastapi")


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import api.property_merge as pm
    from api import dependencies as deps
    from api import main as api_main

    db = _Ledger({1: 10, 2: 20, 3: 30})
    _merged(db, [10, 20])
    db.log.clear()
    monkeypatch.setattr(pm, "resolve_active_property_id",
                        lambda conn, pid: {10: 10, 20: 10}.get(pid))
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: db
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {
        "is_admin": True, "email": OP}
    yield TestClient(api_main.app), db
    api_main.app.dependency_overrides.clear()


def test_the_detach_route_returns_the_contract_and_a_second_click_moves_nothing(client):
    http, db = client
    res = http.post("/properties/10/detach", json={"listing_id": 2, "reason": "jiné patro"})
    assert res.status_code == 200
    assert res.json() == {"listing_id": 2, "detached": True, "outcome": "detached",
                          "survivor_property_id": 10, "restored_property_id": 20,
                          "rulings_written": 1}
    assert _verdicts(db) == [(1, 2, "different", "operator detach from 10: jiné patro")]
    # a stale property id follows merged_into; the advert is no longer on it
    again = http.post("/properties/20/detach", json={"listing_id": 2})
    assert again.status_code == 200 and again.json()["outcome"] == "not_on_property"
    assert again.json()["survivor_property_id"] == 10 and db.listings[2] == 20


def test_the_detach_route_splits_the_propertys_own_advert_off_to_a_new_record(client):
    http, db = client
    # 10 holds its own 1 and 2 merged from 20: its last own advert stays
    last = http.post("/properties/10/detach", json={"listing_id": 1}).json()
    assert (last["detached"], last["outcome"]) == (False, "last_native") and db.listings[1] == 10
    db.listings[4] = 10  # a second advert of its own (grouped at ingest)
    res = http.post("/properties/10/detach", json={"listing_id": 1})
    assert res.status_code == 200
    born = db.listings[1]
    assert res.json() == {"listing_id": 1, "detached": True, "outcome": "split_native",
                          "survivor_property_id": 10, "restored_property_id": born,
                          "rulings_written": 2}
    assert born not in (10, 20, 30) and db.listings[2] == db.listings[4] == 10


def test_the_detach_route_validates_its_input_and_needs_an_identity(client):
    from api import dependencies as deps
    from api import main as api_main

    http, db = client
    assert http.post("/properties/10/detach",
                     json={"listing_id": 2, "reason": "x" * 501}).status_code == 422
    assert http.post("/properties/10/detach", json={"listing_id": 99}).status_code == 404
    assert http.post("/properties/404/detach", json={"listing_id": 2}).status_code == 404
    assert http.post("/properties/merges/grp/unmerge").status_code in (404, 405)
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    assert http.post("/properties/10/detach", json={"listing_id": 2}).status_code == 403
    assert http.post("/properties/merge", json={"property_ids": [1, 2]}).status_code == 403
    assert db.listings[2] == 10


def test_the_origins_route_says_where_each_advert_came_from(client):
    http, _db = client
    res = http.get("/properties/10/origins")
    assert res.status_code == 200
    assert res.json() == {"property_id": 10, "adverts": [
        {"listing_id": 1, "origin_property_id": None, "merge_source": None, "merged_at": None,
         "detach_outcome": "last_native", "splittable": False},
        {"listing_id": 2, "origin_property_id": 20, "merge_source": "operator",
         "merged_at": T0.isoformat().replace("+00:00", "Z"), "detach_outcome": "detached",
         "splittable": True}]}
