"""The operator's reason for changing a training-set mark (migration 473).

A flip on the training-set page says WHAT was wrong; a note says WHY, and the
why is what improves the definition the next machine pass reads. Notes are the
raw material for definition revisions — never labels, never training data.

THE ABSORPTION RULE, stated once where the code lives (the migration carries
the same words): a note is NOT copied into the definition one sentence per
note. The definitions are read by a model and by a person, and either absorbs
a short general rule and drowns in a list of specifics. The reviser reads every
open note for a head together, finds the rule they point at, states it ONCE at
the level of the existing lines, and marks the batch absorbed by the version
that carries it. `absorb` exists so a note is never read into two revisions.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

LOG = logging.getLogger(__name__)

NOTE_MAX_CHARS = 600
STATES = ("positive", "negative", "excluded")

_INSERT_SQL = """
    INSERT INTO tag_label_notes (image_id, tag_id, from_state, to_state, note, created_by)
    VALUES (%(image_id)s, %(tag_id)s, %(from_state)s, %(to_state)s, %(note)s, %(created_by)s)
    RETURNING id, created_at
"""

# Open notes for one head, newest first, with the photo so the reviser can look
# at the case the note describes. The holdout is irrelevant here: a note is not
# a label and selects nothing for training.
_OPEN_BY_TAG_SQL = """
    SELECT n.id, n.image_id, i.storage_path, n.from_state, n.to_state, n.note,
           n.created_at
    FROM tag_label_notes n
    JOIN images i ON i.id = n.image_id
    WHERE n.tag_id = %(tag_id)s::bigint
      AND (%(include_absorbed)s::boolean OR n.absorbed_definition_id IS NULL)
    ORDER BY n.created_at DESC, n.id DESC
    LIMIT %(limit)s
"""

_OPEN_COUNTS_SQL = """
    SELECT tag_id, count(*)::int
    FROM tag_label_notes
    WHERE absorbed_definition_id IS NULL
    GROUP BY tag_id
"""

# Marks a batch absorbed by ONE definition version of the SAME tag. The tag
# check is in the WHERE, not left to the caller: absorbing garáž's notes into a
# kuchyně revision would be a silent audit corruption.
_ABSORB_SQL = """
    UPDATE tag_label_notes n
       SET absorbed_definition_id = d.id, absorbed_at = now()
      FROM tag_definitions d
     WHERE d.id = %(definition_id)s::bigint
       AND n.tag_id = d.tag_id
       AND n.id = ANY(%(note_ids)s::bigint[])
       AND n.absorbed_definition_id IS NULL
    RETURNING n.id
"""


def record_note(
    conn: psycopg.Connection, *, image_id: int, tag_id: int, to_state: str,
    note: str, from_state: str | None = None, created_by: str = "operator",
) -> dict[str, Any]:
    text = " ".join((note or "").split())
    if not text:
        raise ValueError("a note needs words")
    if len(text) > NOTE_MAX_CHARS:
        raise ValueError(f"a note is at most {NOTE_MAX_CHARS} characters")
    if to_state not in STATES:
        raise ValueError(f"to_state must be one of {STATES}")
    if from_state is not None and from_state not in STATES:
        raise ValueError(f"from_state must be one of {STATES} or null")
    with conn.cursor() as cur:
        cur.execute(_INSERT_SQL, {
            "image_id": int(image_id), "tag_id": int(tag_id),
            "from_state": from_state, "to_state": to_state,
            "note": text, "created_by": created_by,
        })
        row = cur.fetchone()
    return {"id": int(row[0]), "image_id": int(image_id), "tag_id": int(tag_id),
            "from_state": from_state, "to_state": to_state, "note": text,
            "created_at": row[1].isoformat() if row[1] else None}


def list_notes(
    conn: psycopg.Connection, *, tag_id: int, include_absorbed: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    try:
        with conn.cursor() as cur:
            cur.execute(_OPEN_BY_TAG_SQL, {
                "tag_id": int(tag_id), "include_absorbed": bool(include_absorbed),
                "limit": max(1, min(int(limit), 500)),
            })
            rows = cur.fetchall()
    except psycopg.errors.UndefinedTable:
        # Merge is not apply: this read ships in the same PR as migration 473
        # and must not take the taxonomy page down in the window between.
        LOG.warning("tag_label_notes is absent — migration 473 not applied yet")
        return []
    return [
            {"id": int(r[0]), "image_id": int(r[1]), "storage_path": r[2],
             "from_state": r[3], "to_state": r[4], "note": r[5],
             "created_at": r[6].isoformat() if r[6] else None}
            for r in rows
        ]


def open_counts(conn: psycopg.Connection) -> dict[int, int]:
    """{tag_id: open notes} — what the taxonomy page shows beside each head so
    a reviser sees where there is material waiting."""
    try:
        with conn.cursor() as cur:
            cur.execute(_OPEN_COUNTS_SQL)
            return {int(r[0]): int(r[1]) for r in cur.fetchall()}
    except psycopg.errors.UndefinedTable:
        LOG.warning("tag_label_notes is absent — migration 473 not applied yet")
        return {}


def absorb(
    conn: psycopg.Connection, *, definition_id: int, note_ids: list[int],
) -> list[int]:
    """Mark notes absorbed by one definition version. Returns the ids actually
    marked — a note of another tag, or one already absorbed, is silently left
    alone, and the caller compares lengths to notice."""
    if not note_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(_ABSORB_SQL, {
            "definition_id": int(definition_id),
            "note_ids": [int(i) for i in note_ids],
        })
        return [int(r[0]) for r in cur.fetchall()]
