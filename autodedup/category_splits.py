"""The category review (E937): the named live properties whose ads carry categories rule 15 never
joins, as sides for the operator to split or keep. Read-only.

Every two ads of one side pass `toolkit.property_identity.category_clash` -- the one definition,
so the sides move with the rule. The rule is a set of pairs, not an equivalence (a komerční may
meet a byt and a dům, the byt and the dům never), so the sides are not its connected components:
the ads whose deal type and category are both known are walked in one fixed order (deal type, then
byt, dům, komerční, pozemek, ostatní, then the ad's id) and each joins the first side it clashes
with no member of, else opens one. An ad with either unknown, or a contentless record
(no price, area, disposition or description: an index sighting whose page was never read, stored
under the default `byt` / `prodej`), never makes a property mixed: it rides with the kept side,
flagged `unknown` / `empty`. The kept side holds most of the property's own ads (no standing merge
brought them), the earliest on a tie, else the canonical ad's: after a split it is the survivor, with
the property's number and its curation (E919). Each item is `proposed_splits`' card plus `mixed` and
`confirmed`; the decision is the operator's `POST /properties/{id}/split`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from itertools import product
from typing import Any, NamedTuple

from autodedup import ui_sql as U
from autodedup.export import scrubbed_text
from autodedup.proposed_splits import pair_rulings, split_facts
from toolkit.property_identity import category_clash, detach_outcomes, listing_origins

REASON_SOURCE = "category"


class Ad(NamedTuple):
    """One ad as the sides read it; `origin` is where a detach returns it, None for an own ad."""

    listing_id: int
    category_type: str | None
    category_main: str | None
    contentless: bool
    origin: int | None


class Side(NamedTuple):
    """Ads one property may hold together, ascending, and the riders that go with the kept side."""

    ads: tuple[int, ...]
    riders: tuple[int, ...]
    kept: bool


def contentless(
    price_czk: Any, area_m2: Any, disposition: str | None, description: str | None,
) -> bool:
    """An index sighting whose page was never read: no price, area, disposition or description."""
    return (price_czk is None and area_m2 is None and disposition is None
            and not (description or "").strip())


def rides(ad: Ad) -> bool:
    """Never a side of its own: a contentless record, or a deal type or category unknown."""
    return ad.contentless or ad.category_type is None or ad.category_main is None


_MAIN_ORDER: tuple[str, ...] = ("byt", "dum", "komercni", "pozemek", "ostatni")


def _clash(a: Ad, b: Ad) -> tuple[str, str | None, str | None] | None:
    return category_clash((a.category_type, a.category_main), (b.category_type, b.category_main))


def _walk(ad: Ad) -> tuple[str, int, str, int]:
    main = str(ad.category_main)
    rank = _MAIN_ORDER.index(main) if main in _MAIN_ORDER else len(_MAIN_ORDER)
    return (str(ad.category_type), rank, main, ad.listing_id)


def sides(ads: Sequence[Ad], canonical: int | None) -> list[Side]:
    """The property's sides, every two ads of a side compatible: the canonical ad's first, then
    by lowest ad."""
    walked: list[list[Ad]] = []
    for ad in sorted((a for a in ads if not rides(a)), key=_walk):
        home = next((side for side in walked if all(_clash(ad, b) is None for b in side)), None)
        if home is None:
            walked.append([ad])
        else:
            home.append(ad)
    order = sorted((tuple(sorted(a.listing_id for a in side)) for side in walked),
                   key=lambda ids: (canonical not in ids, ids[0]))
    riders = tuple(sorted(a.listing_id for a in ads if rides(a)))
    if not order:
        return [Side((), riders, True)] if riders else []
    own = {a.listing_id for a in ads if a.origin is None}
    kept = max(range(len(order)), key=lambda i: (len(own.intersection(order[i])), -i))
    found = [Side(ids, riders if i == kept else (), i == kept) for i, ids in enumerate(order)]
    return sorted(found, key=lambda s: canonical not in s.ads + s.riders)


def _fetch(conn: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def category_splits(conn: Any, property_ids: Sequence[int]) -> list[dict[str, Any]]:
    """One item per live property of two or more ads among `property_ids`, in the order asked."""
    asked = list(dict.fromkeys(int(p) for p in property_ids))
    held: dict[int, list[dict[str, Any]]] = {}
    for row in _fetch(conn, U.CATEGORY_SPLIT_ADVERTS_SQL, {"ids": sorted(asked)}):
        r = dict(zip(U.CATEGORY_SPLIT_ADVERT_COLUMNS, row))
        held.setdefault(int(r["property_id"]), []).append(r)
    held = {pid: rows for pid, rows in held.items() if len(rows) >= 2}
    row = {int(r["listing_id"]): r for rows in held.values() for r in rows}
    if not row:
        return []
    ids = sorted(row)
    texts = {int(lid): (title, description)
             for lid, title, description in _fetch(conn, U.MEMBER_TEXT_SQL, {"ids": ids})}
    rulings = pair_rulings(conn, ids)
    origins, outcomes = listing_origins(conn, ids), detach_outcomes(conn, ids)
    ads = {lid: Ad(lid, r["category_type"], r["category_main"],
                   contentless(r["price_czk"], r["area_m2"], r["disposition"],
                               texts.get(lid, (None, None))[1]),
                   split_facts(lid, origins, outcomes)["origin_property_id"])
           for lid, r in row.items()}

    def advert(ad: Ad) -> dict[str, Any]:
        title, description = texts.get(ad.listing_id, (None, None))
        return {"listing_id": ad.listing_id, "source": row[ad.listing_id]["source"],
                "is_active": row[ad.listing_id]["is_active"],
                **split_facts(ad.listing_id, origins, outcomes),
                "empty": ad.contentless,
                "unknown": ad.category_type is None or ad.category_main is None,
                "text": {"title": scrubbed_text(title), "description": scrubbed_text(description)}}

    return [_item(pid, held[pid][0]["canonical_listing_id"],
                  {int(r["listing_id"]): ads[int(r["listing_id"])] for r in held[pid]},
                  rulings, advert)
            for pid in asked if pid in held]


def _item(
    pid: int, canonical: int | None, ads: Mapping[int, Ad],
    rulings: Mapping[tuple[int, int], dict[str, Any]], advert: Callable[[Ad], dict[str, Any]],
) -> dict[str, Any]:
    """One property as a card: its sides, a clash per pair of sides, and whether it is decided.
    A pair of sides is named by its first clashing pair, each side read from its canonical (else
    lowest) ad: two sides always hold one, as the later side's first ad clashed with the earlier."""
    found = sides(list(ads.values()), canonical)
    members = [s.ads for s in found if s.ads]
    across = [(min(p), max(p)) for i, one in enumerate(members) for other in members[i + 1:]
              for p in product(one, other)]
    words = [(rulings.get(p) or {}).get("verdict") for p in across]
    reads = [[ads[i] for i in sorted(ids, key=lambda i: (i != canonical, i))] for ids in members]
    splits = []
    for i, one in enumerate(reads):
        for other in reads[i + 1:]:
            pairs = (sorted(p, key=lambda ad: ad.listing_id) for p in product(one, other))
            lo, hi = next(p for p in pairs if _clash(*p) is not None)
            field, a, b = _clash(lo, hi)
            splits.append({"listing_lo": lo.listing_id, "listing_hi": hi.listing_id,
                           "reason_source": REASON_SOURCE, "reason": f"{field}: {a} vs {b}",
                           "ruling": rulings.get((lo.listing_id, hi.listing_id))})
    return {
        "property_id": pid,
        "canonical_listing_id": canonical,
        "groups": [_group(s, ads, advert) for s in found],
        "unseen": [],
        "splits": splits,
        "ruled": bool(across) and all(w not in (None, "unsure") for w in words),
        "mixed": len(members) >= 2,
        "confirmed": bool(across) and all(w == "same" for w in words),
    }


def _group(
    side: Side, ads: Mapping[int, Ad], advert: Callable[[Ad], dict[str, Any]],
) -> dict[str, Any]:
    """One side as a card group: its stored deal type and categories, its ads, then its riders."""
    deal = ads[side.ads[0]].category_type if side.ads else None
    mains = sorted({str(ads[i].category_main) for i in side.ads})
    return {"cluster_key": None,
            "label": f"{deal} · {' + '.join(mains)}" if side.ads else "unknown",
            "kept": side.kept, "category_type": deal, "category_main": mains,
            "adverts": [advert(ads[i]) for i in side.ads + side.riders]}
