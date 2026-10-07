"""The operator's split by letters (MS18): every ad of a property gets a letter, and ads with the
same letter are one property afterwards. One letter keeps the property, its number and page, by
the rule (`property_identity.letter_landings`); every other letter leaves, back to the property it
came from while that is still merged into this one, else to a new property, and a letter landing
on two or more properties is joined into the oldest.

`split_preview` reads, changing nothing, where each letter and each of the acting account's items
would land (with the user's picks, what the click would then skip), the rulings it would write and
take back, and `plan`, a digest of all of it but labels, picks and other accounts. `split_property`
makes it true in ONE transaction only while that digest still holds, moving ads ONLY through rule
15's chokepoint (ONE `detach_listings` call with its births and its curation routes, then
`merge_property_set` per letter to join) and ruling ONLY through `record_rulings`: "different"
between ads with different letters; inside a letter only what its join rules, as the merge it is
(MS12: "same" over the landings' canonical ads and every "different" between them). A refusal is a
`SplitRefused` (`{code, message, ids}`, in Czech) and nothing is written. No undo: one merge takes
a split back (MS12).
"""

from __future__ import annotations

import hashlib
import json
import string
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, NamedTuple, Sequence
from uuid import UUID

import psycopg
from psycopg import errors as pg_errors

from autodedup import ui_sql as usql
from toolkit import property_carriers as carriers
from toolkit.property_carriers import Route
from toolkit.property_identity import (
    CategoryClash,
    Landings,
    MergeError,
    ad_categories,
    adverts_on,
    detach_listings,
    first_clash,
    letter_landings,
    listing_places,
    lock_properties,
    merge_property_set,
    negative_sets,
    record_rulings,
    resolve_active_property_id,
)

Pair = tuple[int, int]
Slot = tuple[str, int]  # where an ad lands before the joins: ('kept' | 'origin' | 'new', id)

UNIT_LETTERS = string.ascii_uppercase
MAX_ADVERTS = 100
REASON_MAX = 500
# The lane's per-group bounds (E911): a split never waits long behind the reconcile.
_TIMEOUTS = ("SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '25s'")
_READ_ONLY = "SET TRANSACTION READ ONLY"
_BUSY = (pg_errors.LockNotAvailable, pg_errors.DeadlockDetected, pg_errors.QueryCanceled)
STALE = "Nemovitost se mezitím změnila; nic se nezapsalo."
_KINDS = {"property_notes": "note", "property_pipeline": "pipeline",
          "collection_properties": "collection", "property_tags": "tag",
          "property_dismissals": "dismissal"}
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


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


def newest_pair_rulings(
    conn: psycopg.Connection, listing_ids: Iterable[int],
) -> dict[Pair, dict[str, Any]]:
    """The ruling on every pair drawn from `listing_ids`: its NEWEST row, whoever took it -- the
    one every reader obeys (migration 574, E920), so the one a new word would replace."""
    ids = sorted({int(i) for i in listing_ids})
    out: dict[Pair, dict[str, Any]] = {}
    if len(ids) < 2:
        return out
    with conn.cursor() as cur:
        cur.execute(usql.MEMBER_PAIR_VERDICTS_SQL, {"ids": ids})
        rows = cur.fetchall()
    for row in rows:  # newest first
        v = dict(zip(usql.VERDICT_COLUMNS, row))
        out.setdefault((int(v["listing_lo"]), int(v["listing_hi"])), v)
    return out


def reversed_pairs(stored: Mapping[Pair, str | None], same_pairs: Iterable[Pair]) -> list[Pair]:
    """E52: the pairs a `same` would take back from a standing negative ruling."""
    return sorted(p for p in same_pairs if stored.get(p) in usql.NEGATIVE_VERDICTS)


def reversal_message(pairs: list[Pair]) -> str:
    listed = ", ".join(f"{lo}-{hi}" for lo, hi in pairs[:8])
    return (f"this split takes back your earlier ruling on {len(pairs)} pair(s) ({listed}) and "
            "drops their permanent must-not-link — re-send with confirm_retract to go ahead")


def _bound(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for sql in _TIMEOUTS:
            cur.execute(sql)


def _statement_letters(letters: Mapping[Any, Any]) -> dict[int, str]:
    """Every ad of the property with its letter, or a 400."""
    if not 1 <= len(letters) <= MAX_ADVERTS:
        raise _invalid(f"Rozdělení jmenuje 1 až {MAX_ADVERTS} inzerátů.")
    named: dict[int, str] = {}
    for ad, letter in letters.items():
        lid = int(ad)
        if lid in named:
            raise _invalid("Inzerát je uveden dvakrát.", [lid])
        if not isinstance(letter, str) or len(letter) != 1 or letter not in UNIT_LETTERS:
            raise _invalid("Písmeno musí být A–Z.", [lid])
        named[lid] = letter
    if len(set(named.values())) < 2:
        raise _invalid("Všechny inzeráty mají stejné písmeno, není co rozdělit.")
    return named


def parse_choices(raw: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    """`{"pipeline": {"to": "B", "copies": ["C"]}}` (the preview's query, the click's own shape) as
    each item's letter and copies."""
    try:
        return {str(item): (str(c["to"]), tuple(str(x) for x in c.get("copies") or ()))
                for item, c in json.loads(raw).items()}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise _invalid("Volby se nepodařilo přečíst.") from exc


def parse_letters(raw: str) -> dict[int, str]:
    """`94020:A,140903:A,94492:B` (the preview's query and the pages' links) as letters."""
    out: dict[int, str] = {}
    for token in (t.strip() for t in raw.split(",")):
        ad, _, letter = token.partition(":")
        if not (ad.isascii() and ad.isdigit()):
            raise _invalid("Písmena se zadávají jako inzerát:písmeno, například 94020:A.")
        if int(ad) in out:
            raise _invalid("Inzerát je uveden dvakrát.", [int(ad)])
        out[int(ad)] = letter
    return _statement_letters(out)


def _statement_reason(reason: str | None) -> str | None:
    reason = (reason or "").strip() or None
    if reason is not None and len(reason) > REASON_MAX:
        raise _invalid(f"Důvod má nejvýš {REASON_MAX} znaků.")
    return reason


def _statement_choices(
    choices: Mapping[str, tuple[str, Iterable[str]]], letters: Iterable[str],
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Each item's letter and its copies' letters, among the letters in use, or a 400."""
    in_use = set(letters)
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for item, (to, copies) in choices.items():
        copies = tuple(dict.fromkeys(copies))
        if to not in in_use or not set(copies) <= in_use:
            raise _invalid("Volba míří na písmeno, které žádný inzerát nemá.", [item])
        if to in copies:
            raise _invalid("Kopie nemůže jít do písmene, které položku dostane.", [item])
        out[str(item)] = (to, copies)
    return out


def _record(conn: psycopg.Connection, property_id: int) -> int:
    record = resolve_active_property_id(conn, int(property_id))
    if record is None:
        raise SplitRefused(404, "not_found", f"Nemovitost #{property_id} neexistuje.",
                           [property_id])
    return record


def _check_ads(conn: psycopg.Connection, record: int, letters: Mapping[int, str]) -> None:
    """The letters name every ad on the property and no other: an unknown ad is a 404, an ad
    that arrived or left since the dialog read them a 409 `stale`."""
    places = listing_places(conn, sorted(letters))
    if missing := sorted(lid for lid in letters if lid not in places):
        raise SplitRefused(404, "not_found", f"Inzerát #{missing[0]} neexistuje.", missing)
    if moved := sorted(set(adverts_on(conn, [record])[record]) ^ set(letters)):
        raise SplitRefused(409, "stale", STALE, moved)


class _Plan(NamedTuple):
    """Everything the preview shows and the write makes true, read under one snapshot."""

    record: int
    letters: dict[int, str]
    account: UUID | None
    landings: Landings
    slots: dict[int, Slot]  # each ad, where it lands before the joins
    kept: dict[str, Slot]  # each letter, the landing its join keeps (its only one if no join)
    joins: dict[str, int]  # each joined letter, how many properties it lands on
    refused: dict[str, dict[str, Any]]  # a letter whose join rule 15 refuses
    inside: list[dict[str, Any]]  # standing "different" rulings inside a letter
    different: int  # pairs across letters, each ruled "different"
    taken_back: int  # what the joins take back (MS12)
    routes: list[Route]  # every account's curation, preselected

    @property
    def movers(self) -> dict[int, int | None]:
        return {ad: self.landings.origin.get(ad) for ad, letter in self.letters.items()
                if letter != self.landings.staying}

    def anchor(self, letter: str) -> int | None:
        """The ad whose landing the letter's join keeps; None for the letter that stays."""
        if letter == self.landings.staying:
            return None
        return min(ad for ad, s in self.slots.items()
                   if self.letters[ad] == letter and s == self.kept[letter])

    def letter_of(self, anchor: int | None) -> str:
        return self.landings.staying if anchor is None else self.letters[anchor]

    def lands(self, letter: str) -> tuple[str, int | None]:
        kind, at = self.kept[letter]
        return kind, (None if kind == "new" else at)

    def mine(self, routes: Iterable[Route] | None = None) -> list[Route]:
        """The acting account's routes (the preselection's unless given); none without one."""
        return [r for r in (self.routes if routes is None else routes)
                if self.account is not None and r.account_id == self.account]


def _slot_order(slot: Slot) -> tuple[bool, int]:
    """A landing's place among a letter's: existing properties by id, then the newborns in birth
    order (`detach_listings` births them in ascending ad order)."""
    return slot[0] == "new", slot[1]


def _keeps(slots: Mapping[Slot, list[int]], first_seen: Mapping[int, datetime | None]) -> Slot:
    """The landing a letter's join keeps: decision 17 over each landing's first seen date as the
    recompute will date it (its ads' earliest), unknown last, then `_slot_order`."""
    def dated(slot: Slot) -> datetime | None:
        dates = [d for d in (first_seen.get(ad) for ad in slots[slot]) if d is not None]
        return min(dates) if dates else None
    return min(slots, key=lambda s: (dated(s) is None, dated(s) or _EARLIEST, *_slot_order(s)))


def _plan(conn: psycopg.Connection, record: int, letters: Mapping[int, str],
          account: UUID | None) -> _Plan:
    """The split as it stands now, read only (`split_preview` and, under the lock,
    `split_property`)."""
    land = letter_landings(conn, record, letters)
    slots: dict[int, Slot] = {}
    for ad, letter in letters.items():
        if letter == land.staying:
            slots[ad] = ("kept", record)
        else:
            slots[ad] = ("origin", land.origin[ad]) if ad in land.origin else ("new", ad)
    facts = ad_categories(conn, letters)
    by_letter: dict[str, dict[Slot, list[int]]] = {}
    for ad in sorted(letters):
        by_letter.setdefault(letters[ad], {}).setdefault(slots[ad], []).append(ad)
    first_seen = {ad: f[2] for ad, f in facts.items()}
    kept = {letter: _keeps(at, first_seen) for letter, at in by_letter.items()}
    joins = {letter: len(at) for letter, at in by_letter.items() if len(at) > 1}
    refused: dict[str, dict[str, Any]] = {}
    for letter in joins:
        rows, seen = [], set()
        for slot in sorted(by_letter[letter], key=_slot_order):
            for ad in by_letter[letter][slot]:
                kind, main, _first, empty = facts.get(ad, (None, None, None, True))
                if not empty and (slot, kind, main) not in seen:
                    seen.add((slot, kind, main))
                    rows.append((slot, (kind, main), ad))
        if found := first_clash(rows):
            (field, a, b), _members, ads = found
            refused[letter] = {"code": "refused", "field": field, "a": a, "b": b,
                               "ids": list(ads)}
    stored = newest_pair_rulings(conn, letters)
    sets = negative_sets(conn, letters)
    inside: list[dict[str, Any]] = []
    taken_back = 0
    for letter in sorted(by_letter):
        pairs = sorted(p for p, v in stored.items() if v["verdict"] in usql.NEGATIVE_VERDICTS
                       and letters[p[0]] == letters[p[1]] == letter)
        own_sets = [ids for _key, _gen, ids in sets if all(letters[i] == letter for i in ids)]
        for taken in (True, False):
            p = [x for x in pairs if (slots[x[0]] != slots[x[1]]) == taken]
            s = [x for x in own_sets if (len({slots[i] for i in x}) >= 2) == taken]
            if p or s:
                inside.append({"letter": letter, "pairs": [list(x) for x in p], "sets": len(s),
                               "taken_back": taken})
                taken_back += (len(p) + len(s)) if taken else 0
    sizes = [sum(len(ads) for ads in at.values()) for at in by_letter.values()]
    different = (len(letters) * (len(letters) - 1) - sum(n * (n - 1) for n in sizes)) // 2
    movers = {ad: land.origin.get(ad) for ad, letter in letters.items() if letter != land.staying}
    with conn.cursor() as cur:
        routes = carriers.curation_plan(cur, left=record, movers=movers, account=account)
    return _Plan(record, dict(letters), account, land, slots, kept, joins, refused, inside,
                 different, taken_back, list(routes))


def _items(plan: _Plan, chosen: Sequence[Route] | None = None) -> list[dict[str, Any]]:
    """The acting account's items as the preselection routes them: where each goes and why. With
    `chosen` (the plan the user's picks make), what the click would then skip: a fold not
    re-created and each copy, with why (`skipped`)."""
    picked = plan.mine(chosen) if chosen is not None else []
    folds = {r.item: r.skipped for r in picked if r.action == "recreate"}
    out = []
    for r in plan.mine():
        why = ("fold" if r.action == "recreate" else "came_from" if r.origin is not None
               else "ad" if r.table == "property_notes" and r.anchor is not None else "stays")
        entry: dict[str, Any] = {"item": r.item, "kind": _KINDS[r.table], "label": r.label,
                                 "letter": plan.letter_of(r.anchor), "why": why}
        if r.origin is not None:
            entry["from_property_id"] = r.origin
        if r.action == "recreate":
            entry["skipped"] = folds.get(r.item, r.skipped)
        if copies := [{"letter": plan.letter_of(c.anchor), "skipped": c.skipped}
                      for c in picked if c.action == "copy" and c.item == r.item]:
            entry["copies"] = copies
        out.append(entry)
    return out


def _digest(plan: _Plan, items: list[dict[str, Any]]) -> str:
    """The plan the user saw, but labels, picks and other accounts (so another account's change
    neither refuses the click nor leaks, and a pick never makes it stale)."""
    canon = {
        "letters": sorted([ad, letter] for ad, letter in plan.letters.items()),
        "staying": plan.landings.staying,
        "landings": sorted([letter, *plan.lands(letter), plan.joins.get(letter, 0)]
                           for letter in plan.kept),
        "items": sorted([i["item"], i["letter"], bool(i.get("skipped"))] for i in items),
        "rulings": [plan.different, plan.taken_back],
    }
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _letters_out(plan: _Plan) -> list[dict[str, Any]]:
    out = []
    for letter in sorted(plan.kept):
        lands, at = plan.lands(letter)
        out.append({"letter": letter,
                    "listing_ids": sorted(ad for ad, x in plan.letters.items() if x == letter),
                    "lands": lands, "property_id": at, "joins": plan.joins.get(letter, 0),
                    "refused": plan.refused.get(letter)})
    return out


def split_preview(
    conn: psycopg.Connection, property_id: int, *, letters: Mapping[int, str],
    account: UUID | None, choices: Mapping[str, tuple[str, Iterable[str]]] | None = None,
) -> dict[str, Any]:
    """Where these letters would take each ad and each of `account`'s items (with `choices`, what
    the click would then skip), what would be ruled and taken back, and `plan` (the digest the
    write checks, over the preselection); in a read-only transaction that takes no lock and is
    rolled back."""
    named = _statement_letters(letters)
    picks = _statement_choices(choices or {}, named.values())
    try:
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cur:
                cur.execute(_READ_ONLY)
            _bound(conn)
            record = _record(conn, property_id)
            _check_ads(conn, record, named)
            plan = _plan(conn, record, named, account)
            routes = _chosen(conn, plan, picks)
    except _BUSY as exc:
        raise SplitRefused(409, "busy", "Nemovitost se právě mění, zkuste to za chvíli.",
                           [property_id]) from exc
    return {
        "property_id": plan.record,
        "letters": _letters_out(plan),
        "curation": _items(plan, routes if picks else None),
        "rulings": {"different": plan.different, "taken_back": plan.taken_back,
                    "inside": plan.inside},
        "plan": _digest(plan, _items(plan)),
    }


def _anchors(
    plan: _Plan, choices: Mapping[str, tuple[str, tuple[str, ...]]],
) -> dict[str, tuple[int | None, tuple[int | None, ...]]]:
    """Each chosen letter as the ad whose landing that letter's join keeps (an unchanged letter
    keeps its preselected anchor), for the acting account's own items only."""
    mine = {r.item: r for r in plan.mine() if r.action == "move"}
    out: dict[str, tuple[int | None, tuple[int | None, ...]]] = {}
    for item, (to, copies) in choices.items():
        if (r := mine.get(item)) is None:
            raise _invalid("Volba patří položce, kterou náhled neukázal.", [item])
        anchor = r.anchor if to == plan.letter_of(r.anchor) else plan.anchor(to)
        out[item] = (anchor, tuple(plan.anchor(c) for c in copies))
    return out


def _chosen(conn: psycopg.Connection, plan: _Plan,
            picks: Mapping[str, tuple[str, tuple[str, ...]]]) -> list[Route]:
    """The routes the acting account's picks make (the preselection when none)."""
    if not picks:
        return plan.routes
    with conn.cursor() as cur:
        return carriers.curation_plan(cur, left=plan.record, movers=plan.movers,
                                      choices=_anchors(plan, picks), account=plan.account)


def _join(conn: psycopg.Connection, plan: _Plan, decided_by: str) -> dict[str, dict[str, Any]]:
    """Each letter that landed on two or more properties becomes one, by the one merge (MS12
    takes back the "different" rulings between them); rule 15's refusal propagates."""
    places = listing_places(conn, sorted(plan.movers))
    joined: dict[str, dict[str, Any]] = {}
    for letter in sorted(plan.joins):
        props = sorted({int(places[ad]) for ad, x in plan.letters.items()
                        if x == letter and places.get(ad) is not None})
        if len(props) < 2:
            continue
        try:
            joined[letter] = merge_property_set(
                conn, props, source="operator", reason="operator_split", decided_by=decided_by,
            )["data"]
        except CategoryClash:
            raise
        except MergeError as exc:
            raise SplitRefused(409, "stale", STALE, props) from exc
    return joined


def split_property(
    conn: psycopg.Connection,
    property_id: int,
    *,
    letters: Mapping[int, str],
    decided_by: str,
    account: UUID | None,
    choices: Mapping[str, tuple[str, Iterable[str]]] | None = None,
    reason: str | None = None,
    expect: str = "",
) -> dict[str, Any]:
    """The split, all or nothing, while its plan is still `expect` (the preview's digest):
    `choices` move the acting account's own items to a letter and copy them to others (the rest
    follow the preselection), one `detach_listings`, the joins, then "different" across letters.
    Answers one receipt: where each letter and each of `account`'s items landed."""
    named = _statement_letters(letters)
    why = _statement_reason(reason)
    picks = _statement_choices(choices or {}, named.values())
    if not expect:
        raise _invalid("Chybí náhled rozdělení.")
    call_id = str(uuid.uuid4())
    note = f"operator split {call_id} · {split_summary(named)}" + (f" · {why}" if why else "")
    try:
        with conn.transaction():
            _bound(conn)
            record = _record(conn, property_id)
            _check_ads(conn, record, named)
            lock_properties(conn, [record, *letter_landings(conn, record, named).origin.values()])
            _check_ads(conn, record, named)
            plan = _plan(conn, record, named, account)
            if _digest(plan, _items(plan)) != expect:
                raise SplitRefused(409, "stale", STALE)
            movers = plan.movers
            routes = _chosen(conn, plan, picks)
            out = detach_listings(conn, sorted(movers), decided_by=decided_by, source="operator",
                                  new=plan.landings.new, curation=routes)["data"]["adverts"]
            if stuck := [a["listing_id"] for a in out if not a["detached"] or (
                    a["listing_id"] in plan.landings.origin
                    and a["restored_property_id"] != plan.landings.origin[a["listing_id"]])]:
                raise SplitRefused(409, "stale", STALE, stuck)
            joined = _join(conn, plan, decided_by)
            cross = {(lo, hi) for lo in named for hi in named if lo < hi and named[lo] != named[hi]}
            record_rulings(conn, cross, verdict="different", decided_by=decided_by, note=note)
            final = listing_places(conn, sorted(named))
    except _BUSY as exc:
        raise SplitRefused(409, "busy", "Nemovitost se právě mění, zkuste to za chvíli.",
                           [property_id]) from exc
    # after the joins every letter's ads sit on one property
    at = {letter: int(final[min(ad for ad, x in named.items() if x == letter)])
          for letter in plan.kept}
    return {
        "property_id": record,
        "call_id": call_id,
        "letters": [{**{k: v for k, v in x.items() if k in ("letter", "listing_ids", "lands")},
                     "property_id": at[x["letter"]],
                     "joined": joined[x["letter"]]["merge_group_id"] if x["letter"] in joined
                     else None} for x in _letters_out(plan)],
        "curation": _receipt_items(plan, routes, at),
        "rulings": {"different": len(cross),
                    "same": sum(j["pairs_ruled_same"] for j in joined.values()),
                    "taken_back": sum(j["rulings_taken_back"] for j in joined.values())},
    }


def _receipt_items(plan: _Plan, routes: list[Route],
                   at: Mapping[str, int]) -> list[dict[str, Any]]:
    """What became of each of the acting account's items: its letter and property, and where
    its copies went (a fold re-created is one of them); a fold or a copy not made says why
    (`skipped`)."""
    def skip(r: Route) -> dict[str, str]:
        return {"skipped": r.skipped} if r.skipped else {}

    mine = plan.mine(routes)
    out = []
    for r in mine:
        if r.action == "copy":
            continue
        letter = plan.letter_of(r.anchor)
        copies = [{"letter": plan.letter_of(c.anchor), "property_id": at[plan.letter_of(c.anchor)],
                   **skip(c)} for c in mine if c.action == "copy" and c.item == r.item]
        out.append({"item": r.item, "kind": _KINDS[r.table], "label": r.label, "letter": letter,
                    "property_id": at[letter], "copies": copies,
                    **({"why": "fold"} if r.action == "recreate" else {}), **skip(r)})
    return out
