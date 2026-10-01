"""The stateful fake of `listings.property_id`, `properties`, the merge ledger and the operator's
ruling store (`autodedup.verdicts` + `autodedup.must_not_link`) that the one merge, the one undo
and the split statement run against end to end: tests/test_detach_listing.py,
tests/test_property_merge_set.py and tests/test_property_split.py; executed:
tests/test_merge_safety_live.py and tests/test_property_carriers_live.py.

STRICT: it answers each statement by its exact text and raises on one it does not model. The
carriers it does not model (all but the asset link) are swapped for `RecordingCarrier`s by the
`ledger_carriers` fixture, which every suite over this fake opts into; `db.carried` then holds
each (merge|detach, carrier name, step) the writers handed the seam, `db.changed` each recompute
and `db.browse` each Browse patch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

import scripts.recompute_property_stats as rps
import toolkit.browse_read_model as brm
import toolkit.property_identity as pi
import toolkit.property_split as ps
from autodedup import ui_sql as usql
from scraper.db import NEW_SINGLETONS_SQL
from toolkit import property_carriers as carriers

OP = "operator@example.com"
T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _Tx:
    def __init__(self, db: "_Ledger") -> None:
        self.db = db

    def __enter__(self) -> "_Tx":
        self.saved = (dict(self.db.listings), dict(self.db.props), [dict(e) for e in self.db.events],
                      dict(self.db.into), dict(self.db.assets),
                      [dict(r) for r in self.db.verdicts], dict(self.db.mnl),
                      list(self.db.carried), list(self.db.changed), list(self.db.browse))
        return self

    def __exit__(self, exc_type: Any, *exc: Any) -> bool:
        if exc_type is not None:
            (self.db.listings, self.db.props, self.db.events, self.db.into,
             self.db.assets, self.db.verdicts, self.db.mnl,
             self.db.carried, self.db.changed, self.db.browse) = self.saved
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
    membership log; verdicts: the pair rulings LEDGER (migration 574), appended on change like
    `VERDICT_PAIR_APPEND_SQL`, the newest row per pair the ruling; mnl: (lo, hi) -> (source,
    reason)."""

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
        self.verdicts: list[dict[str, Any]] = []
        self.mnl: dict[tuple[int, int], tuple[str, str]] = {}
        self.clock = 0
        self.carried: list[tuple[str, str, Any]] = []
        self.changed: list[list[int]] = []
        self.browse: list[list[int]] = []

    def rule(self, lo: int, hi: int, verdict: str, *, by: str = OP, note: str | None = None,
             reasons: list[str] | None = None) -> None:
        """Seed a stored pair ruling, as the verdict routes would have written it."""
        self._append({"listing_lo": lo, "listing_hi": hi, "verdict": verdict, "note": note,
                      "reasons": reasons or [], "decided_by": by})
        if verdict in usql.NEGATIVE_VERDICTS:
            self.mnl[(lo, hi)] = ("operator", note or "")

    def newest(self, lo: int, hi: int, by: str | None = None) -> dict[str, Any] | None:
        """The pair's newest row (`decided_at desc, id desc`), or `by`'s newest."""
        rows = [r for r in self.verdicts if (r["listing_lo"], r["listing_hi"]) == (lo, hi)
                and (by is None or r["decided_by"] == by)]
        return max(rows, key=lambda r: (r["decided_at"], r["id"])) if rows else None

    def word(self, lo: int, hi: int, by: str | None = None) -> tuple[str, str | None] | None:
        """The pair's ruling (its newest row), or the newest row `by` wrote."""
        row = self.newest(lo, hi, by)
        return (row["verdict"], row["note"]) if row else None

    def history(self, lo: int, hi: int) -> list[tuple[str, str | None, str]]:
        """Every row of the pair, oldest first: (verdict, note, decided_by)."""
        return [(r["verdict"], r["note"], r["decided_by"])
                for r in sorted(self.verdicts, key=lambda r: (r["decided_at"], r["id"]))
                if (r["listing_lo"], r["listing_hi"]) == (lo, hi)]

    def _row(self, p: dict[str, Any], *, decided_at: int | None = None) -> dict[str, Any]:
        if decided_at is None:
            self.clock += 1
        row = {"id": len(self.verdicts) + 1, "kind": "pair", "cluster_key": None,
               "weight": None, "generation": None, "member_ids": None,
               "listing_lo": p["listing_lo"], "listing_hi": p["listing_hi"],
               "verdict": p["verdict"], "note": p["note"], "reasons": list(p["reasons"]),
               "decided_by": p["decided_by"],
               "decided_at": self.clock if decided_at is None else decided_at}
        self.verdicts.append(row)
        return row

    def _append(self, p: dict[str, Any]) -> list[tuple]:
        """`VERDICT_PAIR_APPEND_SQL`: a new row unless the newest already says exactly this."""
        row = self.newest(p["listing_lo"], p["listing_hi"])
        if row is None or (row["verdict"], row["note"], row["reasons"]) != (
                p["verdict"], p["note"], list(p["reasons"])):
            row = self._row(p)
        return [tuple(row.get(c) for c in usql.VERDICT_COLUMNS)]

    def _veto_as_ruling(self, p: dict[str, Any]) -> None:
        """`VERDICT_PAIR_FROM_VETO_SQL`: a bare operator veto written down as its `different`,
        dated before anything the call writes."""
        key = (p["listing_lo"], p["listing_hi"])
        veto = self.mnl.get(key)
        if veto and veto[0] == "operator" and self.newest(*key) is None:
            self._row({"listing_lo": key[0], "listing_hi": key[1], "verdict": "different",
                       "note": veto[1], "reasons": [], "decided_by": "operator"}, decided_at=0)

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Tx:
        return _Tx(self)

    def sql(self, needle: str) -> list[Any]:
        return [p for s, p in self.log if needle in s]

    def dispatch(self, s: str, p: Any) -> list[tuple]:
        """Answer one statement by its exact (whitespace-normalized) text; anything this fake
        does not model is a test failure, never a silent empty result."""
        handler = _HANDLERS.get(s)
        if handler is None:
            raise AssertionError(f"_Ledger does not model: {s[:160]}")
        return handler(self, p) or []

    # --- the handlers, one per statement (`_HANDLERS` below maps the text to each) ---------

    def _chain(self, p: Any) -> list[tuple]:
        out = []
        for root in p["ids"]:
            at, hops = root, 0
            while at in self.props and self.props[at] != "active" and hops < 20:
                at, hops = self.into.get(at), hops + 1
            if at in self.props and self.props[at] == "active":
                out.append((root, at))
        return out

    def _member_verdicts(self, p: Any) -> list[tuple]:
        ids = set(p["ids"])
        rows = [r for r in self.verdicts if r["listing_lo"] in ids and r["listing_hi"] in ids]
        rows.sort(key=lambda r: (r["decided_at"], r["id"]), reverse=True)
        return [tuple(r.get(c) for c in usql.VERDICT_COLUMNS) for r in rows]

    def _mnl_upsert(self, p: Any) -> None:
        self.mnl[(p["listing_lo"], p["listing_hi"])] = ("operator", p["reason"])

    def _mnl_retract(self, p: Any) -> None:
        key = (p["listing_lo"], p["listing_hi"])
        if self.mnl.get(key, ("",))[0] == "operator":
            del self.mnl[key]

    def _mnl_pairs(self, p: Any) -> list[tuple]:
        ids = set(p["ids"])
        return [(lo, hi, src, why) for (lo, hi), (src, why) in sorted(self.mnl.items())
                if lo in ids and hi in ids]

    def _mnl_restore(self, p: Any) -> None:
        self.mnl[(p["listing_lo"], p["listing_hi"])] = (p["source"], p["reason"])

    def _adverts_on(self, p: Any) -> list[tuple]:
        return [(lid, pid) for lid, pid in sorted(self.listings.items()) if pid in p["ids"]]

    def _lock_set(self, p: Any) -> list[tuple]:
        cat = lambda pid: self.cats.get(pid, ("prodej", "byt"))  # noqa: E731
        return [(pid, self.props[pid], self.first_seen.get(pid, T0), self.assets.get(pid),
                 *cat(pid)) for pid in sorted(p["ids"]) if pid in self.props]

    def _status(self, p: Any) -> list[tuple]:
        return [(pid, self.props[pid], self.into.get(pid))
                for pid in sorted(p["ids"]) if pid in self.props]

    def _retired_asset(self, p: Any) -> list[tuple]:
        return [(self.assets.get(p["retired"]),)]

    def _canonical_adverts(self, p: Any) -> list[tuple]:
        return [(self.canonical[pid],) for pid in p["ids"] if pid in self.canonical]

    def _ingest_grouping(self, p: Any) -> list[tuple]:
        self.events.append({"id": len(self.events) + 1, "group": p["group"],
                            "survivor": p["left"], "listing": p["listing"], "prev": p["born"],
                            "source": "operator", "undone_by": p["by"]})
        return [(len(self.events),)]

    def _new_singletons(self, p: Any) -> list[tuple]:
        born = []
        for lid in p["ids"]:
            if lid in self.listings and self.listings[lid] is None:
                pid = max(self.props) + 1
                self.props[pid], self.listings[lid] = "active", pid
                born.append((pid,))
        return born

    def _sizes(self, p: Any) -> list[tuple]:
        live = {e["listing"] for e in self.events if e["undone_by"] is None}
        sizes = {pid: [lid for lid, at in self.listings.items() if at == pid] for pid in p["ids"]}
        return [(pid, len(ids), len(set(ids) - live)) for pid, ids in sizes.items() if ids]

    def _ledger(self, p: Any) -> None:
        moved = sorted(lid for lid, pid in self.listings.items() if pid == p["retired"])
        self.events += [{"id": len(self.events) + i + 1, "group": p["group"],
                         "survivor": p["survivor"], "listing": lid, "prev": p["retired"],
                         "source": p["source"], "undone_by": None}
                        for i, lid in enumerate(moved)]
        self.count = len(moved)

    def _repoint(self, p: Any) -> None:
        self.listings = {lid: p[0] if pid == p[1] else pid for lid, pid in self.listings.items()}

    def _retire(self, p: Any) -> None:
        self.props[p[1]], self.into[p[1]] = "merged_away", p[0]

    def _places(self, p: Any) -> list[tuple]:
        return [(lid, self.listings[lid]) for lid in p["ids"] if lid in self.listings]

    def _property_of(self, p: Any) -> list[tuple]:
        return [(self.listings[p[0]],)] if p[0] in self.listings else []

    def _live_moves(self, p: Any) -> list[tuple]:
        return [(e["listing"], e["id"], e["group"], e["survivor"], e["prev"], e["source"], T0)
                for e in sorted(self.events, key=lambda e: (e["listing"], e["id"]))
                if e["listing"] in p["ids"] and e["undone_by"] is None]

    def _move_advert(self, p: Any) -> None:
        self.count = int(self.listings.get(p[1]) == p[2])
        if self.count:
            self.listings[p[1]] = p[0]

    def _undo(self, p: Any) -> None:
        for e in self.events:
            if e["id"] in p[1] and e["undone_by"] is None:
                e["undone_by"] = p[0]

    def _reactivate(self, p: Any) -> None:
        self.count = int(self.props.get(p["pid"]) == "merged_away")
        if self.count:
            self.props[p["pid"]] = "active"
            self.into.pop(p["pid"], None)

    def _staying(self, p: Any) -> list[tuple]:
        return [(lid,) for lid, pid in sorted(self.listings.items()) if pid == p[0]]

    def _recompute_one(self, p: Any) -> None:
        self.changed.append([p["pid"]])

    def _browse_delete(self, p: Any) -> None:
        self.browse.append(list(p[0]))

    def _nothing(self, p: Any) -> None:
        return None

    def _carry(self, p: dict[str, Any]) -> None:
        for pid, want in ((p["survivor"], p["asset"]), (p["retired"], None)):
            if self.assets.get(pid) != want:
                self.assets[pid] = want
                self.asset_events.append((p["asset"], pid, "linked" if want else "unlinked",
                                          p["reason"]))
        return None

    def _restore(self, p: dict[str, Any]) -> None:
        carried = [a for a, pid, act, why in self.asset_events
                   if pid == p["restored"] and act == "unlinked" and why == p["merge"]]
        holders = [h for h in p["path"] if carried and self.assets.get(h) == carried[-1]]
        if not holders:
            return None
        asset = carried[-1]
        moved = [(p["restored"], asset)] if self.assets.get(p["restored"]) is None else []
        moved += [(h, None) for h in holders
                  if any((asset, h, "linked", m) in self.asset_events for m in p["merges"])]
        for pid, want in moved:
            self.assets[pid] = want
            self.asset_events.append((asset, pid, "linked" if want else "unlinked", p["detach"]))
        return None


def _n(sql: str) -> str:
    return " ".join(sql.split())


# Exact statement text -> the handler that models it. Matched by equality on the constants the
# code executes, so a changed statement fails here instead of falling through to "no rows".
_HANDLERS: dict[str, Callable[[_Ledger, Any], Any]] = {
    _n(sql): handler for sql, handler in (
        (pi._RESOLVE_SURVIVORS_SQL, _Ledger._chain),
        (usql.MEMBER_PAIR_VERDICTS_SQL, _Ledger._member_verdicts),
        (usql.VERDICT_PAIR_FROM_VETO_SQL, _Ledger._veto_as_ruling),
        (usql.VERDICT_PAIR_APPEND_SQL, _Ledger._append),
        (usql.MUST_NOT_LINK_UPSERT_SQL, _Ledger._mnl_upsert),
        (usql.MUST_NOT_LINK_RETRACT_SQL, _Ledger._mnl_retract),
        (usql.MUST_NOT_LINK_PAIRS_SQL, _Ledger._mnl_pairs),
        (usql.MUST_NOT_LINK_RESTORE_SQL, _Ledger._mnl_restore),
        (ps._ADVERTS_ON_SQL, _Ledger._adverts_on),
        *((timeout, _Ledger._nothing) for timeout in ps._TIMEOUTS),
        (pi._LOCK_SET_SQL, _Ledger._lock_set),
        (pi._STATUS_SQL, _Ledger._status),
        (pi._STATUS_SQL + " FOR UPDATE", _Ledger._status),
        (carriers.AssetLink.RETIRED_ASSET_SQL, _Ledger._retired_asset),
        (carriers._CARRY_ASSET_LINK_SQL, _Ledger._carry),
        (carriers._RESTORE_ASSET_LINK_SQL, _Ledger._restore),
        (pi._CANONICAL_ADVERTS_SQL, _Ledger._canonical_adverts),
        (pi._INGEST_GROUPING_SQL, _Ledger._ingest_grouping),
        (NEW_SINGLETONS_SQL, _Ledger._new_singletons),
        (pi._SIZES_SQL, _Ledger._sizes),
        (pi._LEDGER_SQL, _Ledger._ledger),
        (pi._REPOINT_SQL, _Ledger._repoint),
        (pi._RETIRE_SQL, _Ledger._retire),
        (pi._PLACES_SQL, _Ledger._places),
        ("SELECT property_id FROM listings WHERE id = %s", _Ledger._property_of),
        (pi._LIVE_MOVES_SQL, _Ledger._live_moves),
        (pi._MOVE_ADVERT_SQL, _Ledger._move_advert),
        (pi._UNDO_SQL, _Ledger._undo),
        (pi._REACTIVATE_SQL, _Ledger._reactivate),
        (pi._STAYING_SQL, _Ledger._staying),
        (rps._RECOMPUTE_ONE_SQL, _Ledger._recompute_one),
        (brm._DELETE_SQL, _Ledger._browse_delete),
        (brm._INSERT_SQL, _Ledger._nothing),
    )
}


@dataclass
class RecordingCarrier:
    """A carrier that only records the step it was handed (in `db.carried` and `db.log`), so a
    suite over this fake asserts at the carrier seam instead of each carrier's SQL."""

    name: str
    columns: tuple = ()
    sql: tuple = ()

    def on_merge(self, cur: _Cur, step: carriers.MergeStep) -> None:
        cur.db.carried.append(("merge", self.name, step))
        cur.db.log.append((f"carrier:{self.name}", step))

    def on_detach(self, cur: _Cur, step: carriers.DetachStep) -> None:
        cur.db.carried.append(("detach", self.name, step))
        cur.db.log.append((f"carrier:{self.name}", step))


@pytest.fixture()
def ledger_carriers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every carrier but the asset link (which this fake models) recorded, not executed: a suite
    that forgets it fails on the first carrier statement the strict fake does not model."""
    monkeypatch.setattr(carriers, "PROPERTY_CARRIERS", tuple(
        c if c.name == "asset_link" else RecordingCarrier(c.name)
        for c in carriers.PROPERTY_CARRIERS))
