"""Decision 9: the splits a stored generation proposes. Read-only: auto-merge yes, auto-split never.

A LIVE property of two or more adverts that the generation touches is a proposal when the
generation puts the adverts it saw into different groups (a seen advert no group holds is a group
of its own; one it never saw is not spoken for), or when two of its adverts carry a stored
negative (a must-not-link, or a negative operator ruling). Each split pair carries the engine's
stated reason -- the conflict that refused the union, else the pair's own reject/veto/band
decision, else the must-not-link, else `no stated fact` -- and the operator's newest ruling.
The batch split is `POST /properties/{id}/detach`, advert by advert, from the page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from autodedup import apply_sql as A
from autodedup import ui_sql as U
from toolkit.property_identity import listing_origins

NO_STATED_FACT = "no stated fact"
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

    splits: dict[int, list[tuple[int, int]]] = {}
    for pid, (canonical, adverts) in props.items():
        group = {lid: i for i, (_k, lids) in enumerate(_groups(adverts, canonical)) for lid in lids}
        lids = sorted(a[0] for a in adverts)
        pairs = [(lo, hi) for i, lo in enumerate(lids) for hi in lids[i + 1:]
                 if (lo in group and hi in group and group[lo] != group[hi])
                 or (lo, hi) in negatives
                 or (rulings.get((lo, hi)) or {}).get("verdict") in U.NEGATIVE_VERDICTS]
        if pairs or property_id is not None:
            splits[pid] = pairs
    shown = sorted(a[0] for pid in splits for a in props[pid][1])
    if not shown:
        return []

    reasons: dict[tuple[int, int], tuple[str, str]] = {
        pair: ("must_not_link", text) for pair, text in negatives.items()}
    for row in _fetch(conn, U.CLUSTER_PAIRS_SQL, {"generation": generation, "ids": shown}):
        p = dict(zip(U.PAIR_COLUMNS, row))
        if p["zone"] in _SPLIT_ZONES:
            reasons[(p["listing_lo"], p["listing_hi"])] = ("pair", f"guard: {p['guard_veto']}"
                if p["guard_veto"] else f"{p['zone']}: {p['decision']}")
    for row in _fetch(conn, U.CLUSTER_CONFLICTS_SQL, {"cluster_key": None, "ids": shown}):
        c = dict(zip(U.CONFLICT_COLUMNS, row))
        if c["listing_lo"] is not None and (c["detail"] or {}).get("generation") in (None, generation):
            reasons[(c["listing_lo"], c["listing_hi"])] = (
                "conflict", f"{c['kind']}: {c['invariant'] or 'no invariant named'}")
    origins = listing_origins(conn, shown)

    def advert(a: tuple) -> dict[str, Any]:
        return {"listing_id": a[0], "source": a[1], "is_active": a[2],
                "origin_property_id": origins[a[0]][0] if a[0] in origins else None}

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
