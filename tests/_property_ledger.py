"""The stateful fake of `listings.property_id`, `properties`, the merge ledger and the operator's
ruling store (`autodedup.verdicts` + `autodedup.must_not_link`) that the one merge, the one undo
and the split statement run against end to end: tests/test_detach_listing.py,
tests/test_property_merge_set.py and tests/test_property_split.py; executed:
tests/test_merge_safety_live.py."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from autodedup import ui_sql as usql

OP = "operator@example.com"
T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _Tx:
    def __init__(self, db: "_Ledger") -> None:
        self.db = db

    def __enter__(self) -> "_Tx":
        self.saved = (dict(self.db.listings), dict(self.db.props), [dict(e) for e in self.db.events],
                      dict(self.db.into), dict(self.db.assets),
                      [dict(r) for r in self.db.verdicts], dict(self.db.mnl))
        return self

    def __exit__(self, exc_type: Any, *exc: Any) -> bool:
        if exc_type is not None:
            (self.db.listings, self.db.props, self.db.events, self.db.into,
             self.db.assets, self.db.verdicts, self.db.mnl) = self.saved
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

    def dispatch(self, s: str, p: Any) -> list[tuple]:  # noqa: C901
        cat = lambda pid: self.cats.get(pid, ("prodej", "byt"))  # noqa: E731
        if s.startswith("SET LOCAL"):
            return []
        if s.startswith("WITH RECURSIVE chain"):
            out = []
            for root in p["ids"]:
                at, hops = root, 0
                while at in self.props and self.props[at] != "active" and hops < 20:
                    at, hops = self.into.get(at), hops + 1
                if at in self.props and self.props[at] == "active":
                    out.append((root, at))
            return out
        if s == " ".join(usql.MEMBER_PAIR_VERDICTS_SQL.split()):
            ids = set(p["ids"])
            rows = [r for r in self.verdicts
                    if r["listing_lo"] in ids and r["listing_hi"] in ids]
            rows.sort(key=lambda r: (r["decided_at"], r["id"]), reverse=True)
            return [tuple(r.get(c) for c in usql.VERDICT_COLUMNS) for r in rows]
        if s == " ".join(usql.VERDICT_PAIR_FROM_VETO_SQL.split()):
            self._veto_as_ruling(p)
            return []
        if s == " ".join(usql.VERDICT_PAIR_APPEND_SQL.split()):
            return self._append(p)
        if s == " ".join(usql.MUST_NOT_LINK_UPSERT_SQL.split()):
            self.mnl[(p["listing_lo"], p["listing_hi"])] = ("operator", p["reason"])
            return []
        if s == " ".join(usql.MUST_NOT_LINK_RETRACT_SQL.split()):
            key = (p["listing_lo"], p["listing_hi"])
            if self.mnl.get(key, ("",))[0] == "operator":
                del self.mnl[key]
            return []
        if s == " ".join(usql.MUST_NOT_LINK_PAIRS_SQL.split()):
            ids = set(p["ids"])
            return [(lo, hi, src, why) for (lo, hi), (src, why) in sorted(self.mnl.items())
                    if lo in ids and hi in ids]
        if s == " ".join(usql.MUST_NOT_LINK_RESTORE_SQL.split()):
            self.mnl[(p["listing_lo"], p["listing_hi"])] = (p["source"], p["reason"])
            return []
        if s.startswith("SELECT id, property_id FROM listings WHERE property_id = ANY("):
            return [(lid, pid) for lid, pid in sorted(self.listings.items()) if pid in p["ids"]]
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
