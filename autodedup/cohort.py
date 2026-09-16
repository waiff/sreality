"""The trial cohort as data: four blocks (ruling D1), and the assembled negative control.

Three blocks are geographic and are one code each — a town is `listing_location.obec_kod`,
a quarter is `cast_obce_kod`. The fourth is *assembled*: it has no code of its own, only a
rule (`NEGATIVE_CONTROL`) evaluated over Praha, and its `code` field carries the Praha
obec_kod the rule runs inside so every block still prints one number in the artifact's meta.

Blocks are disjoint by construction — the two towns are not Praha, the quarter is the one
Praha quarter the negative control excludes — and `dedupe_ids` keeps that true even if a
future override list overlaps, first block in `BLOCKS` order winning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

GRAINS: tuple[str, ...] = ("town", "quarter", "assembled")


@dataclass(frozen=True)
class Block:
    key: str
    grain: str
    code: int
    label: str

    def as_meta(self) -> dict[str, Any]:
        return {"key": self.key, "grain": self.grain, "code": self.code, "label": self.label}


@dataclass(frozen=True)
class NegativeControlRule:
    """Praha, minus the dense quarter, restricted to address points that carry listings on
    more than one floor — i.e. the hard negatives (E11/§2): same building, different unit.

    Deliberately NOT verbatim ruling D1, which phrases the stratum as a label over blocks
    1-3 with a second dHash arm (a hash on >= 5 distinct listings). W1 builds this
    geographic draw on the wave's build instruction; the dHash arm is unsampled and the
    group floor is 3 rather than D1's 2. Open item: PROGRAM.md section 14 needs a ruling
    line for the divergence before W1 closes."""

    obec_kod: int = 554782
    exclude_cast_obce_kod: int | None = 490245
    min_group_size: int = 3
    min_distinct_floors: int = 2
    max_listings: int = 800


PRAHA_OBEC_KOD = 554782

BLOCKS: tuple[Block, ...] = (
    Block("jablonec", "town", 563510, "Jablonec nad Nisou"),
    Block("turnov", "town", 577626, "Turnov"),
    Block("vysocany", "quarter", 490245, "Praha-Vysočany"),
    Block("negctl", "assembled", PRAHA_OBEC_KOD, "Praha negative control"),
)

NEGATIVE_CONTROL = NegativeControlRule()

_BY_KEY: dict[str, Block] = {b.key: b for b in BLOCKS}


def block_by_key(key: str) -> Block:
    if key not in _BY_KEY:
        raise ValueError(f"unknown block {key!r}; known: {', '.join(sorted(_BY_KEY))}")
    return _BY_KEY[key]


def parse_blocks(raw: str | None) -> tuple[Block, ...]:
    """`"town:563510 quarter:490245"` -> two blocks; `""`/None -> the ruled cohort.

    Both a comma and whitespace separate entries, but only whitespace survives a trip
    through the lane: `autodedup.lane.parse_kv_args` splits its own `k=v` string on commas,
    so a dispatched override must be spelled `blocks=town:563510 quarter:490245`.

    A bare block key (`negctl`, `jablonec`) selects a ruled block; `grain:code` builds an ad
    hoc one whose key and label are derived, so an override never silently reuses a ruled
    block's label for a different code.
    """
    text = (raw or "").strip()
    if not text:
        return BLOCKS
    out: list[Block] = []
    seen: set[str] = set()
    for token in text.replace(",", " ").split():
        if ":" in token:
            grain, _, code_text = token.partition(":")
            grain = grain.strip()
            if grain not in GRAINS:
                raise ValueError(f"unknown grain {grain!r}; known: {', '.join(GRAINS)}")
            try:
                code = int(code_text)
            except (TypeError, ValueError):
                raise ValueError(f"block {token!r}: code must be an integer") from None
            block = Block(f"{grain}{code}", grain, code, f"{grain} {code}")
        else:
            block = block_by_key(token)
        if block.key in seen:
            raise ValueError(f"duplicate block {block.key!r}")
        seen.add(block.key)
        out.append(block)
    return tuple(out)


def select_negative_control(
    groups: Sequence[dict[str, Any]], *, max_listings: int = NEGATIVE_CONTROL.max_listings
) -> tuple[list[int], list[dict[str, Any]]]:
    """Take WHOLE address-point groups, largest first, up to `max_listings` listings.

    A group that does not fit is skipped rather than ending the walk: one 900-listing
    developer address at the head of the list would otherwise empty the whole stratum, and
    the order is fixed by the query (size desc, then code) so skipping stays deterministic.
    """
    ids: list[int] = []
    taken: list[dict[str, Any]] = []
    for group in groups:
        group_ids = [int(i) for i in (group.get("listing_ids") or [])]
        if not group_ids or len(ids) + len(group_ids) > max_listings:
            continue
        ids.extend(group_ids)
        taken.append(
            {"ruian_adm_kod": group.get("ruian_adm_kod"), "n_listings": len(group_ids)}
        )
        if len(ids) >= max_listings:
            break
    return ids, taken


def dedupe_ids(per_block: Iterable[tuple[Block, Sequence[int]]]) -> dict[int, str]:
    """listing_id -> block key, first block in the given order winning."""
    out: dict[int, str] = {}
    for block, ids in per_block:
        for listing_id in ids:
            out.setdefault(int(listing_id), block.key)
    return out
