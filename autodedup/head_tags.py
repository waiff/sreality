"""The DINOv3 tag heads as the engine's room tags: one SQL, one label-keyed room map, one rule.

The active tag model (migration 490, `tag_head_models.status = 'active'`) scores every image with
every head and stores the winner. The engine reads the WINNER only, mapped to its room vocabulary
(`toolkit.room_taxonomy`), and routes a photo only when the winner clears the consumer floor
(0.5, the one floor the NEW DEDUP ledger measured: it keeps 96.8 % of true tags) by at least
`HEAD_MARGIN` over the runner-up. Below that the photo is untagged: it is still compared by
pHash and CLIP cosine, it is only kept out of same-room routing. A head label the map does not
know is refused, never guessed.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

HEAD_FLOOR: float = 0.5
HEAD_MARGIN: float = 0.0

# Normalised operator taxonomy label (prefix `interier - ` / `exterier - ` / `podklad - ` dropped,
# lower case) -> the engine's room. 3d plan is NOT floor_plan: a 3D view against a 2D drawing of
# one unit cannot match by dHash and would fire `floorplan_conflict` falsely.
HEAD_ROOMS: dict[str, str] = {
    "kuchyně": "kitchen",
    "koupelna": "bathroom",
    "obývací pokoj": "living_room",
    "fasáda": "exterior_facade",
    "půdorys": "floor_plan",
    "3d plán": "plan_3d",
    "katastrální mapa": "site_plan",
    "letecký snímek s ohraničením subjektu": "site_plan",
    "property list": "property_document",
    "garáž": "garage",
    "technické zařízení / místnost": "technical",
}

_PREFIXES = ("interier - ", "exterier - ", "podklad - ")

COHORT_HEAD_TAGS_SQL = """
SELECT
    s.image_id       AS image_id,
    s.winner_score   AS winner_score,
    s.scores         AS scores,
    t.label          AS label
FROM image_tag_scores s
JOIN tag_head_models m ON m.id = s.model_id AND m.status = 'active'
JOIN tag_taxonomy t ON t.id = s.winner_tag_id
WHERE s.image_id = any(%(ids)s::bigint[])
"""


class UnknownHeadLabel(ValueError):
    """The active model names a tag the engine has no room for."""


def head_room(label: str) -> str:
    key = label.strip().lower()
    for prefix in _PREFIXES:
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    room = HEAD_ROOMS.get(key)
    if room is None:
        raise UnknownHeadLabel(f"no engine room for tag head {label!r}")
    return room


def _runner_up(scores: Any) -> float:
    raw = json.loads(scores) if isinstance(scores, str) else (scores or {})
    ranked = sorted((float(v) for v in raw.values()), reverse=True)
    return ranked[1] if len(ranked) > 1 else 0.0


def head_tag_pairs(rows: Sequence[Mapping[str, Any]], *, floor: float = HEAD_FLOOR,
                   margin: float = HEAD_MARGIN) -> list[list[Any]]:
    """`image_tag_scores` rows of one image -> `[[room, winner_score]]`, or `[]` (untagged)."""
    for row in rows:
        score = float(row["winner_score"])
        room = head_room(str(row["label"]))
        if score < floor or score - _runner_up(row.get("scores")) < margin:
            return []
        return [[room, round(score, 6)]]
    return []


def head_record(row: Mapping[str, Any]) -> list[Any]:
    """One `COHORT_HEAD_TAGS_SQL` row -> the export's `head`: [room, winner, runner-up], raw, so a
    harness arm applies its own floor and margin (`head_tag_pairs` is the live rule)."""
    winner = float(row["winner_score"])
    return [head_room(str(row["label"])), round(winner, 6), round(_runner_up(row.get("scores")), 6)]


def apply_head_tags(images: Any, *, floor: float = HEAD_FLOOR, margin: float = HEAD_MARGIN) -> dict[str, int]:
    """Harness arm H: replace each image's CLIP tags by its exported head winner under the rule.
    An image exported without a head score becomes untagged; returns the counts."""
    counts = {"tagged": 0, "untagged": 0, "unscored": 0}
    for image in images:
        head = image.head
        if not head:
            image.tags = []
            counts["unscored"] += 1
        elif float(head[1]) >= floor and float(head[1]) - float(head[2]) >= margin:
            image.tags = [(str(head[0]), float(head[1]))]
            counts["tagged"] += 1
        else:
            image.tags = []
            counts["untagged"] += 1
    return counts


def load_head_dump(path: str) -> dict[int, list[Any]]:
    """`tag_model dump`'s JSONL (image_id, room, winner, runner_up) -> {image_id: head record}, so an
    offline arm reads the real winners on an export made before the export carried them."""
    import gzip

    out: dict[int, list[Any]] = {}
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("room") is None:
                raise UnknownHeadLabel(f"dump row {row.get('image_id')} names no engine room")
            out[int(row["image_id"])] = [str(row["room"]), float(row["winner"]),
                                         float(row["runner_up"])]
    return out


def attach_heads(images: Any, dump: Mapping[int, Sequence[Any]]) -> int:
    """Set `image.head` from a dump (None when the dump does not carry the image); returns how many
    images it carried."""
    found = 0
    for image in images:
        head = dump.get(int(image.image_id))
        image.head = None if head is None else list(head)
        found += head is not None
    return found
