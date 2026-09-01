"""Machine pre-answers for exam sittings (migration 461).

The operator's ruling, reversing 458/459's no-suggestion posture: pre-run each
exam image through the model and mark the suggested buttons, subtly, on the exam
screen. The honest cost is anchoring — a machine-assisted sitting measures
agreement with a machine-anchored human, not blind agreement — and the
mitigation is provenance: every suggestion is stored with the exact question
list it answered, so suggested-vs-final disagreement stays computable per image
and per tag. Suggestions are never labels and never training data; they live in
their own table and the holdout census does not apply to them.

TUNED FOR PRECISION, NOT RECALL — the exact opposite of the screener. The
screener errs toward including (a miss costs coverage); a suggestion errs toward
omitting (a wrong mark anchors the human toward a wrong answer, an omission
merely leaves a button unmarked). Same JSON contract, so the screener's parser
is reused unchanged.

ASKED LIST FROZEN AT CALL TIME. Sets grow by design ("the exam grows by
columns"), and a suggestion computed for a 3-tag set says nothing about the
8-tag version of the same set. `suggestion_for` serves a row only when its
asked list equals the sitting's current list; after a set edit the lane simply
runs again.
"""

from __future__ import annotations

from typing import Any

import psycopg


def build_prompt(tags: list[dict[str, Any]]) -> str:
    """The suggester's instruction — the human's question, machine-worded, with
    the brief's positive rule (the photo is OF the thing; a clear co-subject
    counts, an incidental background hint does not)."""
    listing = "\n".join(f"  {t['id']}: {t['label']}" for t in tags)
    return (
        "You are pre-answering a real-estate photo exam so a human can confirm "
        "faster.\n\n"
        "Which of these categories is this photo genuinely OF? A category counts "
        "when the photo is of that thing — alone or as a clear co-subject — not "
        "when it merely appears incidentally in the background.\n"
        f"{listing}\n\n"
        "Answer with a JSON object: {\"ids\": [<category ids>]}. Use an empty "
        "list if none apply.\n"
        "Suggest ONLY what you would defend: a wrong suggestion misleads the "
        "human, an omission merely leaves a button unmarked."
    )


_GET_SET_SQL = """
    SELECT id, name, tag_ids FROM tag_exam_sets WHERE name = %(name)s
"""

# Order-preserving label fetch: the array order IS the on-screen key order.
_SET_TAGS_SQL = """
    SELECT o.tag_id, t.label
    FROM unnest(%(tag_ids)s::bigint[]) WITH ORDINALITY AS o (tag_id, pos)
    JOIN tag_taxonomy t ON t.id = o.tag_id AND t.active
    ORDER BY o.pos
"""

# Members still needing a suggestion for this set. Two rows do NOT count as
# suggestions and are re-offered here: an ERRORED one (an absence of evidence,
# exactly as a failed screen is re-offered) and a STALE one — its frozen
# asked_tag_ids no longer equal the set's current list. Staleness alone is
# only half a mechanism: the API refusing to SERVE a stale row (by design)
# means nothing if the lane never re-FILLS it — measured live when set_2 grew
# 8 -> 10 and both "successful" re-runs found zero members to suggest. The
# mutual containment pair is set equality (neither array carries duplicates).
_UNSUGGESTED_MEMBERS_SQL = """
    SELECT m.image_id, i.storage_path
    FROM tag_exam_members m
    JOIN images i ON i.id = m.image_id AND i.storage_path IS NOT NULL
    WHERE m.cohort_id = %(cohort_id)s
      AND NOT EXISTS (
            SELECT 1 FROM tag_exam_suggestions s
            WHERE s.cohort_id = m.cohort_id AND s.image_id = m.image_id
              AND s.set_id = %(set_id)s AND s.error IS NULL
              AND s.asked_tag_ids <@ %(tag_ids)s::bigint[]
              AND s.asked_tag_ids @> %(tag_ids)s::bigint[]
          )
    ORDER BY m.position
    LIMIT %(limit)s
"""

_UPSERT_SUGGESTION_SQL = """
    INSERT INTO tag_exam_suggestions (
      cohort_id, image_id, set_id, asked_tag_ids, suggested_tag_ids, model, error
    )
    VALUES (%(cohort_id)s, %(image_id)s, %(set_id)s, %(asked_tag_ids)s,
            %(suggested_tag_ids)s, %(model)s, %(error)s)
    ON CONFLICT (cohort_id, image_id, set_id) DO UPDATE
      SET asked_tag_ids = EXCLUDED.asked_tag_ids,
          suggested_tag_ids = EXCLUDED.suggested_tag_ids,
          model = EXCLUDED.model,
          error = EXCLUDED.error,
          suggested_at = now()
"""

_SUGGESTION_FOR_SQL = """
    SELECT asked_tag_ids, suggested_tag_ids
    FROM tag_exam_suggestions
    WHERE cohort_id = %(cohort_id)s AND image_id = %(image_id)s
      AND set_id = %(set_id)s AND error IS NULL
"""

_MEASURED_COST_SQL = """
    SELECT count(*)::int, COALESCE(avg(cost_usd), 0)::double precision
    FROM llm_calls
    WHERE called_for = ANY(%(called_fors)s) AND model = %(model)s
      AND cost_usd IS NOT NULL
"""


def get_set(conn: psycopg.Connection, *, name: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_GET_SET_SQL, {"name": name})
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": int(row[0]), "name": row[1], "tag_ids": [int(x) for x in row[2]]}


def set_tags(conn: psycopg.Connection, *, tag_ids: list[int]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_SET_TAGS_SQL, {"tag_ids": tag_ids})
        return [{"id": int(r[0]), "label": r[1]} for r in cur.fetchall()]


def unsuggested_members(
    conn: psycopg.Connection, *, cohort_id: int, set_id: int,
    tag_ids: list[int], limit: int,
) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(_UNSUGGESTED_MEMBERS_SQL, {
            "cohort_id": cohort_id, "set_id": set_id,
            "tag_ids": list(tag_ids), "limit": limit,
        })
        return [(int(r[0]), r[1]) for r in cur.fetchall()]


def record_suggestion(
    conn: psycopg.Connection, *, cohort_id: int, image_id: int, set_id: int,
    asked_tag_ids: list[int], suggested_tag_ids: list[int] | None, model: str,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(_UPSERT_SUGGESTION_SQL, {
            "cohort_id": cohort_id, "image_id": image_id, "set_id": set_id,
            "asked_tag_ids": list(asked_tag_ids),
            "suggested_tag_ids": list(suggested_tag_ids or []),
            "model": model, "error": error,
        })


def suggestion_for(
    conn: psycopg.Connection, *, cohort_id: int, image_id: int, set_id: int,
    current_tag_ids: list[int],
) -> list[int] | None:
    """The stored suggestion for one question, or None when there is none worth
    showing — not computed, errored, or computed against a different question
    list than the sitting is asking (a stale answer marking a subset of the
    buttons would look complete while being wrong)."""
    with conn.cursor() as cur:
        cur.execute(_SUGGESTION_FOR_SQL, {
            "cohort_id": cohort_id, "image_id": image_id, "set_id": set_id,
        })
        row = cur.fetchone()
    if row is None:
        return None
    asked = sorted(int(x) for x in (row[0] or []))
    if asked != sorted(current_tag_ids):
        return None
    current = set(current_tag_ids)
    return [int(x) for x in (row[1] or []) if int(x) in current]


def measured_cost(
    conn: psycopg.Connection, *, model: str, called_fors: list[str],
) -> tuple[int, float]:
    """Measured per-image cost across the given call types. The suggest call is
    the screener's shape — one image, a short tag list, a ~30-token JSON reply —
    so the screen lane's measured cost is a valid prior for the pre-flight
    estimate until suggest has ten calls of its own."""
    with conn.cursor() as cur:
        cur.execute(_MEASURED_COST_SQL, {"called_fors": called_fors, "model": model})
        row = cur.fetchone()
    return (int(row[0]), float(row[1])) if row else (0, 0.0)
