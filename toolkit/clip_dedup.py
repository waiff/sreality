"""CLIP-backed dedup helpers — the free room source + the cosine recall tier.

Read-only DB reads over image_clip_tags (the free zero-shot room tag per image)
and image_clip_embeddings (the 512-d vector). The engine prefers these over the
paid LLM classify on the hot path; the LLM forensic compare still gates merges.
The cosine is computed server-side via pgvector `<=>` so the engine job stays
torch-free.
"""

from __future__ import annotations

from typing import Any


def clip_room_grouping(
    conn: Any, *, sreality_id: int, model: str,
) -> dict[str, list[int]] | None:
    """{logical_tag: [image_id, ...]} for one listing from image_clip_tags, or
    None if it has no CLIP-tagged image yet (caller falls back to LLM classify)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.logical_tag, t.image_id "
            "FROM image_clip_tags t JOIN images i ON i.id = t.image_id "
            "WHERE i.sreality_id = %s AND t.model = %s",
            (sreality_id, model),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    out: dict[str, list[int]] = {}
    for logical_tag, image_id in rows:
        out.setdefault(logical_tag, []).append(image_id)
    return out


def room_pair_cosine(
    conn: Any, *, image_ids_a: list[int], image_ids_b: list[int], model: str,
) -> float | None:
    """Best (max) cosine similarity between any A-image and any B-image of one
    room, from stored CLIP embeddings. None if either side has no stored vector
    (e.g. an inactive listing whose embeddings were never persisted). pgvector
    `<=>` is cosine DISTANCE, so similarity = 1 - distance."""
    if not image_ids_a or not image_ids_b:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(1 - (ea.embedding <=> eb.embedding)) "
            "FROM image_clip_embeddings ea, image_clip_embeddings eb "
            "WHERE ea.model = %s AND eb.model = %s "
            "  AND ea.image_id = ANY(%s) AND eb.image_id = ANY(%s)",
            (model, model, list(image_ids_a), list(image_ids_b)),
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None
