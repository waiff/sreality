"""Decision 9: the splits a stored generation proposes. Read-only: auto-merge yes, auto-split never.

A LIVE property of two or more adverts that the generation touches is a proposal when a pair of
its adverts is STATED apart: the generation groups the two apart (a seen advert no group holds is
a group of its own) and scored that pair reject/veto/band or a conflict names it -- in a batch pass
a pair it never scored is not spoken for, like an advert it never saw, while in the live stream
(`rt`) two adverts it read and holds apart with no stored row between them are listed as `not
compared` (Decision 9: a grouping the stream does not support is a proposal, never a split): no
row cannot say whether the pair was scored below the store's floor or never retrieved at all, so
it is never read as a band decision, and the page never takes an advert away on it (the batch
split moves only adverts a STATED reason holds apart) -- or the pair carries a stored negative (a
must-not-link, or a negative operator ruling). A pair whose newest ruling is `same` is never
proposed: the engine obeys it (decision 8). Each split pair carries its reason -- the conflict that
refused the union, else the pair's own decision, else the must-not-link, else `no stated fact` (a
negative ruling alone) -- and the operator's newest ruling. The batch split is
`POST /properties/{id}/detach`, advert by advert, from the page; each advert carries what that
detach would answer now (`detach_outcome`) and whether it moves it (`splittable`: back to its
origin, or a native advert beside another to a new record).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from autodedup import apply_sql as A
from autodedup import ui_sql as U
from autodedup.incremental import GENERATION
from toolkit.property_identity import MOVED, detach_outcomes, listing_origins

NO_STATED_FACT = "no stated fact"
NOT_COMPARED = "not compared"
_SPLIT_ZONES = frozenset({"reject", "veto", "band"})  # a merge-zone pair speaks FOR one property


def _fetch(conn: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _groups(adverts: list[tuple], canonical: int) -> list[tuple[int | None, list[int]]]:
    """The seen adverts as the generation groups them, the canonical advert's group first."""
    by: dict[int, tuple[int | None, list[int]]] = {}
    for lid, _src, _active, key, seen in adverts:
        if seen:
            by.setdefault(key if key is not None else -lid, (key, []))[1].append(lid)
    return sorted(by.values(), key=lambda g: (canonical not in g[1], min(g[1])))


def proposed_splits(
    conn: Any, generation: str, *, property_id: int | None = None,
) -> list[dict[str, Any]]:
    """Every proposal of `generation` by property id; with `property_id`, that property's view
    whether or not it is one."""
    props: dict[int, tuple[int, list[tuple]]] = {}
    for pid, lid, src, active, canonical, key, seen in _fetch(conn, U.PROPOSED_SPLIT_ADVERTS_SQL, {
            "generation": generation, "property_id": property_id}):
        props.setdefault(pid, (canonical, []))[1].append((lid, src, active, key, seen))
    props = {pid: v for pid, v in props.items() if len(v[1]) >= 2}
    ids = sorted(a[0] for _c, adverts in props.values() for a in adverts)
    if not ids:
        return []
    negatives = {(lo, hi): f"must_not_link ({src})" for lo, hi, src in _fetch(
        conn, A.MUST_NOT_LINK_SQL, {"listing_ids": ids})}
    rulings: dict[tuple[int, int], dict[str, Any]] = {}
    for row in _fetch(conn, U.MEMBER_PAIR_VERDICTS_SQL, {"ids": ids}):  # newest first
        v = dict(zip(U.VERDICT_COLUMNS, row))
        at = v["decided_at"]
        rulings.setdefault((v["listing_lo"], v["listing_hi"]), {
            "verdict": v["verdict"], "decided_by": v["decided_by"], "note": v["note"],
            "decided_at": at.isoformat() if isinstance(at, datetime) else at,
            "reasons": list(v["reasons"] or [])})

    candidates: dict[int, list[tuple[int, int]]] = {}
    apart: set[tuple[int, int]] = set()
    for pid, (canonical, adverts) in props.items():
        group = {lid: i for i, (_k, lids) in enumerate(_groups(adverts, canonical)) for lid in lids}
        lids = sorted(a[0] for a in adverts)
        apart |= {(lo, hi) for i, lo in enumerate(lids) for hi in lids[i + 1:]
                  if lo in group and hi in group and group[lo] != group[hi]}
        pairs = [(lo, hi) for i, lo in enumerate(lids) for hi in lids[i + 1:]
                 if (lo, hi) in apart
                 or (lo, hi) in negatives
                 or (rulings.get((lo, hi)) or {}).get("verdict") in U.NEGATIVE_VERDICTS]
        if pairs or property_id is not None:
            candidates[pid] = pairs
    scope = sorted(a[0] for pid in candidates for a in props[pid][1])
    if not scope:
        return []

    reasons: dict[tuple[int, int], tuple[str, str]] = {
        pair: ("must_not_link", text) for pair, text in negatives.items()}
    for row in _fetch(conn, U.CLUSTER_PAIRS_SQL, {"generation": generation, "ids": scope}):
        p = dict(zip(U.PAIR_COLUMNS, row))
        if p["zone"] in _SPLIT_ZONES:
            reasons[(p["listing_lo"], p["listing_hi"])] = ("pair", f"guard: {p['guard_veto']}"
                if p["guard_veto"] else f"{p['zone']}: {p['decision']}")
    for row in _fetch(conn, U.CLUSTER_CONFLICTS_SQL, {"cluster_key": None, "ids": scope}):
        c = dict(zip(U.CONFLICT_COLUMNS, row))
        if c["listing_lo"] is not None and (c["detail"] or {}).get("generation") in (None, generation):
            reasons[(c["listing_lo"], c["listing_hi"])] = (
                "conflict", f"{c['kind']}: {c['invariant'] or 'no invariant named'}")
    if generation == GENERATION:
        for pair in apart:
            reasons.setdefault(pair, ("not_compared", NOT_COMPARED))

    def stated(pair: tuple[int, int]) -> bool:
        verdict = (rulings.get(pair) or {}).get("verdict")
        return verdict != "same" and (pair in reasons or verdict in U.NEGATIVE_VERDICTS)

    splits = {pid: [pair for pair in pairs if stated(pair)] for pid, pairs in candidates.items()}
    splits = {pid: pairs for pid, pairs in splits.items() if pairs or property_id is not None}
    shown = sorted(a[0] for pid in splits for a in props[pid][1])
    if not shown:
        return []
    origins, outcomes = listing_origins(conn, shown), detach_outcomes(conn, shown)

    def advert(a: tuple) -> dict[str, Any]:
        return {"listing_id": a[0], "source": a[1], "is_active": a[2],
                "origin_property_id": origins[a[0]][0] if a[0] in origins else None,
                "detach_outcome": outcomes.get(a[0]),
                "splittable": outcomes.get(a[0]) in MOVED}

    items = []
    for pid in sorted(splits):
        canonical, adverts = props[pid]
        by_id = {a[0]: a for a in adverts}
        split = [{"listing_lo": lo, "listing_hi": hi,
                  **dict(zip(("reason_source", "reason"),
                             reasons.get((lo, hi), ("none", NO_STATED_FACT)))),
                  "ruling": rulings.get((lo, hi))} for lo, hi in splits[pid]]
        items.append({
            "property_id": pid, "canonical_listing_id": canonical, "proposed": bool(split),
            "groups": [{"cluster_key": key, "adverts": [advert(by_id[i]) for i in lids]}
                       for key, lids in _groups(adverts, canonical)],
            "unseen": [advert(a) for a in adverts if not a[4]],
            "splits": split,
            "ruled": bool(split) and all(s["ruling"] is not None for s in split),
        })
    return items
