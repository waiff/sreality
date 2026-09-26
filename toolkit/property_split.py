"""The operator's split statement (E919): one property's adverts as the operator partitions them,
made true in ONE transaction and ruled.

`split_property` takes every advert the operator was shown and the units to separate; the rest is
the kept unit. It moves adverts ONLY through rule 15's chokepoint (`detach_listing`, then
`merge_property_set` to join a unit that landed on two records) and rules ONLY through
`record_rulings`: `different` + an operator must-not-link across units, `same` inside each
separated unit, and `same` inside the kept unit when `keep_together`; a machine veto on a pair
it rules `same` stays the machine's (`restore_must_not_link`). A refusal is a `SplitRefused` and
nothing is written. The response carries the body of its own undo: `undo_split` re-joins the
adverts and restores each pair's previous word and must-not-link row.
"""

from __future__ import annotations

import string
import uuid
from collections import Counter
from typing import Any, Iterable, Mapping

import psycopg
from psycopg import errors as pg_errors

from autodedup import ui_sql as usql
from toolkit.property_identity import (
    MOVED,
    MergeError,
    detach_listing,
    detach_outcomes,
    listing_origins,
    listing_places,
    lock_properties,
    merge_property_set,
    must_not_link_rows,
    record_rulings,
    resolve_active_property_id,
    resolve_active_property_ids,
    restore_must_not_link,
)

Pair = tuple[int, int]
Veto = tuple[str, str | None]

# A letter per unit, as the Groups page's split names them; past 26 it is a rejection, not a split.
UNIT_LETTERS = string.ascii_uppercase
MAX_ADVERTS = 100
MAX_SEPARATED = len(UNIT_LETTERS) - 1
REASON_MAX = 500
# `autodedup.must_not_link.source`'s CHECK (migration 528); every one but `operator` is a machine's.
VETO_SOURCES = frozenset({"guard", "model", "llm", "operator"})
# The lane's per-group bounds (E911): a split never waits long behind the reconcile.
_TIMEOUTS = ("SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '25s'")
_ADVERTS_ON_SQL = """
SELECT id, property_id FROM listings WHERE property_id = ANY(%(ids)s::bigint[]) ORDER BY id
"""
_BUSY = (pg_errors.LockNotAvailable, pg_errors.DeadlockDetected, pg_errors.QueryCanceled)


class SplitRefused(MergeError):
    """Nothing written: `status` 400 (malformed), 404 (unknown) or 409 (`code` says why)."""

    def __init__(self, status: int, code: str, message: str, ids: list[Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.ids = status, code, message, list(ids or [])

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "ids": self.ids}


def _invalid(message: str, ids: list[Any] | None = None) -> SplitRefused:
    return SplitRefused(400, "invalid", message, ids)


def split_summary(assignment: Mapping[int, str]) -> str:
    """`A: 94020,140903 | B: 94492`, the assignment as one line for a ruling's note."""
    units: dict[str, list[int]] = {}
    for listing_id, unit in assignment.items():
        units.setdefault(unit, []).append(int(listing_id))
    return " | ".join(
        f"{unit}: {','.join(str(i) for i in sorted(ids))}" for unit, ids in sorted(units.items())
    )


def operator_pair_verdicts(
    conn: psycopg.Connection, listing_ids: Iterable[int], decided_by: str,
) -> dict[Pair, dict[str, Any]]:
    """This decider's live ruling on every pair drawn from `listing_ids` (the upsert keys on
    `decided_by`, so nobody else's word is at stake)."""
    ids = sorted({int(i) for i in listing_ids})
    out: dict[Pair, dict[str, Any]] = {}
    if len(ids) < 2:
        return out
    with conn.cursor() as cur:
        cur.execute(usql.MEMBER_PAIR_VERDICTS_SQL, {"ids": ids})
        rows = cur.fetchall()
    for row in rows:  # newest first
        v = dict(zip(usql.VERDICT_COLUMNS, row))
        if v["decided_by"] == decided_by:
            out.setdefault((int(v["listing_lo"]), int(v["listing_hi"])), v)
    return out


def reversed_pairs(stored: Mapping[Pair, str | None], same_pairs: Iterable[Pair]) -> list[Pair]:
    """E52: the pairs a `same` would take back from this decider's own stored negative."""
    return sorted(p for p in same_pairs if stored.get(p) in usql.NEGATIVE_VERDICTS)


def reversal_message(pairs: list[Pair]) -> str:
    listed = ", ".join(f"{lo}-{hi}" for lo, hi in pairs[:8])
    return (f"this split takes back your earlier ruling on {len(pairs)} pair(s) ({listed}) and "
            "drops their permanent must-not-link — re-send with confirm_retract to go ahead")


def _bound(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for sql in _TIMEOUTS:
            cur.execute(sql)


def _adverts_on(conn: psycopg.Connection, property_ids: Iterable[int]) -> dict[int, list[int]]:
    ids = sorted({int(p) for p in property_ids})
    out: dict[int, list[int]] = {pid: [] for pid in ids}
    with conn.cursor() as cur:
        cur.execute(_ADVERTS_ON_SQL, {"ids": ids})
        for lid, pid in cur.fetchall():
            out.setdefault(int(pid), []).append(int(lid))
    return {pid: sorted(lids) for pid, lids in out.items()}


def _statement_units(adverts: list[int], separate: list[list[int]]) -> list[list[int]]:
    """[U0 (the kept unit), U1..Uk (the separated ones)], or a 400."""
    if not 1 <= len(adverts) <= MAX_ADVERTS:
        raise _invalid(f"adverts names 1 to {MAX_ADVERTS} adverts")
    named = Counter(int(a) for a in adverts)
    if twice := sorted(a for a, n in named.items() if n > 1):
        raise _invalid("an advert is named twice", twice)
    if len(separate) > MAX_SEPARATED:
        raise _invalid(f"a split names at most {len(UNIT_LETTERS)} units")
    shown, seen, units = set(named), set(), []
    for group in separate:
        if not group:
            raise _invalid("a separated group cannot be empty")
        if len(group) > MAX_ADVERTS:
            raise _invalid(f"a separated group names at most {MAX_ADVERTS} adverts")
        ids = Counter(int(i) for i in group)
        if stray := sorted(set(ids) - shown):
            raise _invalid("a separated advert is not among the adverts shown", stray)
        if twice := sorted(i for i, n in ids.items() if n > 1 or i in seen):
            raise _invalid("an advert is in two separated groups", twice)
        seen |= set(ids)
        units.append(sorted(ids))
    kept = sorted(shown - seen)
    if not kept:
        raise _invalid("one unit must stay: every advert is separated")
    return [kept, *units]


def _targets(letter: Mapping[int, str], keep_together: bool) -> dict[Pair, str]:
    """The verdict each pair of named adverts ends on; the kept unit's own pairs only when
    `keep_together` (without it the statement says nothing about them)."""
    named, out = sorted(letter), {}
    for i, lo in enumerate(named):
        for hi in named[i + 1:]:
            if letter[lo] != letter[hi]:
                out[(lo, hi)] = "different"
            elif letter[lo] != UNIT_LETTERS[0] or keep_together:
                out[(lo, hi)] = "same"
    return out


def _guard(
    conn: psycopg.Connection, record: int, on_record: list[int], places: Mapping[int, int | None],
    letter: Mapping[int, str],
) -> None:
    """The operator's view is current: every advert on the record was shown, and a shown one
    elsewhere sits on a property holding only shown adverts of ONE unit (a re-send finding what
    it moved before); anything else is a 409 `stale`."""
    if unseen := [lid for lid in on_record if lid not in letter]:
        raise SplitRefused(409, "stale", f"property {record} holds adverts the statement did not "
                           f"name: {', '.join(map(str, unseen))}", unseen)
    away = {pid for lid, pid in places.items() if pid != record}
    if None in away:
        stray = sorted(lid for lid, pid in places.items() if pid is None)
        raise SplitRefused(409, "stale", "an advert has no property", stray)
    for pid, lids in _adverts_on(conn, away).items():
        if any(lid not in letter for lid in lids) or len({letter[lid] for lid in lids}) > 1:
            raise SplitRefused(409, "stale", f"property {pid} holds adverts of another unit or "
                               "adverts the statement did not name", lids)


def _keeper(units: list[list[int]], own: set[int], keep_together: bool) -> int:
    """The unit that keeps the record: the kept unit while it holds one of its own adverts (or
    none does), else — an own advert never leaves the last — the unit holding most of them."""
    counts = [len(own.intersection(u)) for u in units]
    if counts[0] or not any(counts):
        return 0
    if not keep_together:
        raise SplitRefused(409, "cannot_move", "the property's own adverts would all leave",
                           [{"listing_id": lid, "outcome": "last_native"} for lid in sorted(own)])
    return max(range(len(units)), key=lambda i: (counts[i], -i))


def _landings(
    conn: psycopg.Connection, letter: Mapping[int, str], movers: list[int],
    origins: Mapping[int, tuple[int, str, Any]],
) -> None:
    """Each unit leaves as a record of its OWN: no two units' adverts go home to one property,
    and a property one goes home to holds only named adverts of that unit; else nothing moves."""
    home: dict[int, set[str]] = {}
    for lid in movers:
        if lid in origins:
            home.setdefault(int(origins[lid][0]), set()).add(letter[lid])
    if shared := sorted(lid for lid in movers
                        if lid in origins and len(home[int(origins[lid][0])]) > 1):
        raise SplitRefused(409, "cannot_move", "adverts of different units came from one "
                           f"property and would go back to it together: {', '.join(map(str, shared))}",
                           [{"listing_id": lid, "outcome": "shared_origin"} for lid in shared])
    for pid, lids in _adverts_on(conn, home).items():
        if drag := [lid for lid in lids if lid not in letter]:
            raise SplitRefused(409, "join_would_drag", f"property {pid}, where a separated advert "
                               "goes back, holds adverts the statement did not name: "
                               f"{', '.join(map(str, drag))}", drag)
        if other := [lid for lid in lids if letter[lid] not in home[pid]]:
            back = sorted(lid for lid in movers if lid in origins and int(origins[lid][0]) == pid)
            raise SplitRefused(409, "cannot_move", f"property {pid}, where a separated advert goes "
                               f"back, holds another unit's adverts: {', '.join(map(str, other))}",
                               [{"listing_id": lid, "outcome": "shared_origin"} for lid in back])


def _join(
    conn: psycopg.Connection, record: int, units: list[list[int]], landed: set[int],
    decided_by: str,
) -> dict[int, dict[str, Any]]:
    """Each unit left on two or more records becomes one, by the one merge; only across the
    record and what this call's detaches landed on, never dragging an advert of another unit."""
    places = listing_places(conn, [lid for unit in units for lid in unit])
    joined: dict[int, dict[str, Any]] = {}
    for index, unit in enumerate(units):
        props = sorted({int(places[lid]) for lid in unit if places.get(lid) is not None})
        if len(props) < 2:
            continue
        if outside := [p for p in props if p != record and p not in landed]:
            raise SplitRefused(409, "stale", "a unit sits on a property this split did not "
                               f"produce: {', '.join(map(str, outside))}", outside)
        held = _adverts_on(conn, props)
        if drag := sorted(lid for p in props for lid in held.get(p, []) if lid not in unit):
            raise SplitRefused(409, "join_would_drag", "joining the unit would take adverts "
                               f"the statement did not name: {', '.join(map(str, drag))}", drag)
        try:
            joined[index] = merge_property_set(
                conn, props, source="operator", reason="operator_split", decided_by=decided_by,
            )["data"]
        except MergeError as exc:
            raise SplitRefused(409, "refused", str(exc), props) from exc
    return joined


def split_property(
    conn: psycopg.Connection,
    property_id: int,
    *,
    adverts: list[int],
    separate: list[list[int]],
    keep_together: bool,
    decided_by: str,
    reason: str | None = None,
    confirm_retract: bool = False,
) -> dict[str, Any]:
    """The statement: `separate`'s units leave the property (each one record), the rest stay
    (one property, ruled so when `keep_together`); all or nothing."""
    units = _statement_units(adverts, separate)
    if len(units) == 1 and not keep_together:
        raise _invalid("nothing to separate and nothing to confirm")
    reason = (reason or "").strip() or None
    if reason is not None and len(reason) > REASON_MAX:
        raise _invalid(f"a reason is at most {REASON_MAX} characters")
    letter = {lid: UNIT_LETTERS[i] for i, unit in enumerate(units) for lid in unit}
    named = sorted(letter)
    targets = _targets(letter, keep_together)
    call_id = str(uuid.uuid4())
    note = f"operator split {call_id} · {split_summary(letter)}" + (f" · {reason}" if reason else "")
    try:
        with conn.transaction():
            _bound(conn)
            record = resolve_active_property_id(conn, int(property_id))
            if record is None:
                raise SplitRefused(404, "not_found", f"property {property_id} not found")
            places = listing_places(conn, named)
            if missing := [lid for lid in named if lid not in places]:
                raise SplitRefused(404, "not_found", "no such advert", missing)
            on_record = _adverts_on(conn, [record])[record]
            _guard(conn, record, on_record, places, letter)

            own = set(on_record) - set(listing_origins(conn, on_record))
            keeper = _keeper(units, own, keep_together)
            movers = [lid for lid in on_record if letter[lid] != UNIT_LETTERS[keeper]]
            outcomes = detach_outcomes(conn, movers)
            if stuck := [{"listing_id": lid, "outcome": outcomes.get(lid)} for lid in movers
                         if outcomes.get(lid) not in MOVED]:
                raise SplitRefused(409, "cannot_move", "an advert cannot leave the property",
                                   stuck)

            stored = operator_pair_verdicts(conn, named, decided_by)
            taken_back = reversed_pairs({p: v["verdict"] for p, v in stored.items()},
                                        [p for p, t in targets.items() if t == "same"])
            if taken_back and not confirm_retract:
                raise SplitRefused(409, "reverses_rulings", reversal_message(taken_back),
                                   [list(p) for p in taken_back])

            origins = listing_origins(conn, movers)
            lock_properties(conn, [record, *(origins[m][0] for m in movers if m in origins)])
            if _adverts_on(conn, [record])[record] != on_record:
                raise SplitRefused(409, "stale", f"property {record} changed while splitting")
            _landings(conn, letter, movers, origins)
            vetoes = must_not_link_rows(conn, named)
            moves: list[dict[str, Any]] = []
            for lid in movers:
                out = detach_listing(conn, lid, decided_by=decided_by, reason=reason,
                                     source="operator")["data"]
                if out["outcome"] not in MOVED:
                    raise SplitRefused(409, "cannot_move", "an advert cannot leave the property",
                                       [{"listing_id": lid, "outcome": out["outcome"]}])
                moves.append({"listing_id": lid, "outcome": out["outcome"], "from": record,
                              "to": out["restored_property_id"]})
            joined = _join(conn, record, units, {int(m["to"]) for m in moves}, decided_by)
            final = listing_places(conn, named)

            write = targets if moves else {
                p: t for p, t in targets.items() if (stored.get(p) or {}).get("verdict") != t}
            same = {p for p, t in write.items() if t == "same"}
            different = {p for p, t in write.items() if t == "different"}
            record_rulings(conn, same, verdict="same", decided_by=decided_by, note=note)
            record_rulings(conn, different, verdict="different", decided_by=decided_by, note=note)
            # `same` leaves a machine veto standing; an interim detach ruling may have taken it.
            restore_must_not_link(conn, {p: v for p, v in vetoes.items()
                                         if v[0] != "operator" and targets.get(p) == "same"})
    except _BUSY as exc:
        raise SplitRefused(409, "busy", "the property is being changed right now; try again",
                           [property_id]) from exc

    undo = None
    if moves or write:
        undo = {
            "call_id": call_id,
            "placements": {lid: final[lid] for lid in on_record} if moves else {},
            "rulings": [{"listing_lo": lo, "listing_hi": hi,
                         "verdict": (stored.get((lo, hi)) or {}).get("verdict"),
                         "note": (stored.get((lo, hi)) or {}).get("note"),
                         "reasons": list((stored.get((lo, hi)) or {}).get("reasons") or []),
                         "must_not_link": _veto_body(vetoes.get((lo, hi)))}
                        for lo, hi in sorted(write)],
        }
    return {
        "call_id": call_id,
        "property_id": record,
        "record_kept_by": UNIT_LETTERS[keeper],
        "units": [
            {"unit": UNIT_LETTERS[i], "role": "kept" if i == 0 else "separated",
             "listing_ids": unit, "property_id": final[unit[0]],
             "moved": [m for m in moves if letter[m["listing_id"]] == UNIT_LETTERS[i]],
             "merge_group_id": joined[i]["merge_group_id"] if i in joined else None}
            for i, unit in enumerate(units)
        ],
        "moved": len(moves),
        "rulings": {"written": len(write), "same": len(same), "different": len(different),
                    "must_not_link_written": len(different),
                    "must_not_link_retracted": len(same)},
        "reversed_pairs": [list(p) for p in taken_back],
        "undo": undo,
    }


def _veto_body(veto: Veto | None) -> dict[str, Any] | None:
    return {"source": veto[0], "reason": veto[1]} if veto else None


def _claimed_vetoes(rulings: list[Mapping[str, Any]], pairs: list[Pair]) -> dict[Pair, Veto]:
    """The must-not-link row each pair had before the split, as its undo body carries it."""
    out: dict[Pair, Veto] = {}
    for r, p in zip(rulings, pairs):
        veto = r.get("must_not_link")
        if veto is None:
            continue
        if not isinstance(veto, Mapping) or veto.get("source") not in VETO_SOURCES:
            raise _invalid("not a must-not-link row", list(p))
        reason = veto.get("reason")
        out[p] = (str(veto["source"]), None if reason is None else str(reason))
    return out


def undo_split(
    conn: psycopg.Connection,
    property_id: int,
    *,
    call_id: str,
    placements: Mapping[int, int],
    rulings: list[Mapping[str, Any]],
    decided_by: str,
) -> dict[str, Any]:
    """Take back one split from the body its response issued: while nothing moved or was ruled
    again since, its adverts become one property again (the oldest record, decision 17) and
    each pair it ruled carries the word it had before (`unsure` where there was none) and the
    must-not-link row it had before, a machine's included. Exact for ONE decider: the restored
    `unsure` is the newest word on its pair, so it masks another decider's older `same` (E919)."""
    try:
        call = str(uuid.UUID(str(call_id)))
    except ValueError as exc:
        raise _invalid("call_id is not a split's id") from exc
    pairs: list[Pair] = []
    seen: set[Pair] = set()
    for r in rulings:
        lo, hi = int(r["listing_lo"]), int(r["listing_hi"])
        if lo >= hi or (lo, hi) in seen:
            raise _invalid("a ruling names a pair twice or out of order", [lo, hi])
        if r.get("verdict") is not None and r["verdict"] not in usql.VERDICT_VALUES:
            raise _invalid(f"not a pair verdict: {r['verdict']}")
        pairs.append((lo, hi))
        seen.add((lo, hi))
    claimed = _claimed_vetoes(rulings, pairs)
    ruled = {lid for p in pairs for lid in p}
    place = {int(lid): int(pid) for lid, pid in placements.items()}
    if not pairs or (stray := sorted(set(place) - ruled)):
        raise _invalid("an undo restores the pairs its split ruled", stray if pairs else [])
    prefix = f"operator split {call}"
    props: list[int] = []
    try:
        with conn.transaction():
            _bound(conn)
            record = resolve_active_property_id(conn, int(property_id))
            if record is None:
                raise SplitRefused(404, "not_found", f"property {property_id} not found")
            now = listing_places(conn, sorted(place))
            want = resolve_active_property_ids(conn, sorted(set(place.values())))
            if moved := sorted(lid for lid, pid in place.items()
                               if now.get(lid) is None or now.get(lid) != want.get(pid)):
                raise SplitRefused(409, "stale", "moved again since the split", moved)
            stored = operator_pair_verdicts(conn, ruled, decided_by)
            if again := [list(p) for p in pairs
                         if not str((stored.get(p) or {}).get("note") or "").startswith(prefix)]:
                raise SplitRefused(409, "stale", "ruled again since the split", again)
            # A machine row goes back only over the split's own operator row (or onto itself).
            vetoes = must_not_link_rows(conn, ruled)
            if forged := [list(p) for p, v in claimed.items() if v[0] != "operator"
                          and vetoes.get(p) != v and not _split_row(vetoes.get(p), prefix)]:
                raise SplitRefused(409, "stale", "a must-not-link changed since the split", forged)
            props = sorted({int(now[lid]) for lid in place if now.get(lid) is not None})
            joined = None
            if len(props) > 1:
                lock_properties(conn, props)
                held = _adverts_on(conn, props)
                if drag := sorted(lid for p in props for lid in held.get(p, []) if lid not in place):
                    raise SplitRefused(409, "join_would_drag", "the adverts now share a property "
                                       "with adverts the split did not move", drag)
                try:
                    joined = merge_property_set(conn, props, source="operator",
                                                reason="operator_split_undo",
                                                decided_by=decided_by)["data"]
                except MergeError as exc:
                    raise SplitRefused(409, "refused", str(exc), props) from exc
            words: dict[tuple[str, str | None, tuple[str, ...]], set[Pair]] = {}
            for r, p in zip(rulings, pairs):
                key = ((r["verdict"], r.get("note"), tuple(r.get("reasons") or []))
                       if r.get("verdict") else ("unsure", f"operator split-undo {call}", ()))
                words.setdefault(key, set()).add(p)
            for (verdict, note, reasons), ps in sorted(words.items(), key=lambda kv: min(kv[1])):
                record_rulings(conn, ps, verdict=verdict, decided_by=decided_by, note=note,
                               reasons=list(reasons))
            restore_must_not_link(conn, claimed)
    except _BUSY as exc:
        raise SplitRefused(409, "busy", "the property is being changed right now; try again",
                           props) from exc
    return {
        "call_id": call,
        "undone": True,
        "property_id": joined["survivor_id"] if joined else (props[0] if props else record),
        "merge_group_id": joined["merge_group_id"] if joined else None,
        "rulings": {"restored": len(pairs)},
    }


def _split_row(veto: Veto | None, prefix: str) -> bool:
    """The operator row this split's `different` wrote (its note is the row's reason)."""
    return veto is not None and veto[0] == "operator" and str(veto[1] or "").startswith(prefix)
