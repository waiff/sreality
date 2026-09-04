"""Bulk machine labeling: the LLM builds the training sets for defined heads.

The programme's north star is a classifier that routes dedup image comparisons
by category. A classifier needs labeled images in quantity, and the operator
cannot hand-label thousands. So: the definitions the operator wrote and ratified
are read by the model, one call per image covering every requested head at once,
and the verdicts land in `image_tag_labels` as ordinary `source='machine'` cells.

WHY THE EXISTING TABLE, not a new one. A machine cell there is already safe by
construction: the upsert's human-wins rail refuses a machine write onto any
human-decided cell, and every write stamps the ACTIVE `definition_id` and the
model — so a label always says which wording produced it. The one place a
separate table WAS needed is the exam, where machine and human answer the same
images and the suppression would hide the disagreement worth reading; that is
`tag_exam_machine_reviews` (467) and it stays separate for exactly that reason.

TWO RAILS ON WHAT MAY BE LABELED:
1. No exam member, ever. Holdout images must stay unseen by training, and a
   curated member is the operator's to answer. Both are excluded by membership,
   not by luck.
2. Heads must be named explicitly by the caller. There is no "label everything"
   default, because bulk labeling is only justified for a head the agreement
   gate has shown the model can read — and that threshold is the operator's
   call, not a constant hidden in a script.

RESUME IS BY PROVENANCE, not by a cursor. An image counts as done for a head
when it carries a machine cell stamped with that head's currently-active
definition. Re-running after a definition edit therefore re-labels exactly the
heads whose wording moved, and re-running after an interruption costs nothing
for what already landed.
"""

from __future__ import annotations

from typing import Any

import psycopg

# Verdict -> the label vocabulary. "skip" is the ratified leave-out: the subject
# is present but the photo is of something else. It is stored, not dropped, so
# the training reader can exclude it explicitly rather than mistake it for an
# unlabeled cell — and it must never be written as a negative.
VERDICT_STATE = {
    "yes": ("positive", None),
    "no": ("negative", None),
    "skip": ("excluded", "pruned"),
}

CREATED_BY = "machine_labeling"


CALLED_FOR = "label_image_bulk"

# The eligibility rails, shared by every strategy: never an exam member (the
# holdout must stay unseen and a curated member is the operator's to answer),
# and missing a machine cell stamped with the head's currently-active
# definition (the resume rail — a wording change re-opens exactly the heads it
# moved, an interruption costs nothing for what already landed).
_ELIGIBLE = """
      i.storage_path IS NOT NULL
      AND NOT EXISTS (
            SELECT 1 FROM tag_exam_members m WHERE m.image_id = i.id
          )
      AND EXISTS (
            SELECT 1
            FROM unnest(%(tag_ids)s::bigint[]) AS t (tag_id)
            JOIN tag_definitions d
              ON d.tag_id = t.tag_id AND d.status = 'active'
            WHERE NOT EXISTS (
                  SELECT 1 FROM image_tag_labels l
                  WHERE l.image_id = i.id AND l.tag_id = t.tag_id
                    AND l.source = 'machine'
                    AND l.definition_id = d.id
                )
          )
"""

# HOW the images are chosen is a sampling decision with budget consequences,
# so it is never implicit. `sample` draws a genuinely random slice (a block
# sample of the table, then the rails, then a shuffle) — the honest default,
# whose class balance mirrors the population. `ids` takes an explicit list, and
# is the hook for a targeted draw once the operator settles the strategy.
# Ordering by id would have been neither: it silently labels the OLDEST images.
_SAMPLE_SQL = f"""
    SELECT i.id, i.storage_path
    FROM images i TABLESAMPLE SYSTEM (%(pct)s)
    WHERE {_ELIGIBLE}
    ORDER BY random()
    LIMIT %(limit)s
"""

_BY_IDS_SQL = f"""
    SELECT i.id, i.storage_path
    FROM images i
    WHERE i.id = ANY(%(image_ids)s::bigint[])
      AND {_ELIGIBLE}
    LIMIT %(limit)s
"""


def sample_candidates(
    conn: psycopg.Connection, *, tag_ids: list[int], limit: int, pct: float = 1.0,
) -> list[tuple[int, str]]:
    """A random slice of eligible images. `pct` is the block-sample percentage:
    raise it when the rails filter most of what it draws (a corpus already
    largely labeled), lower it on a fresh one — it trades scan cost for the
    chance of returning fewer rows than asked."""
    with conn.cursor() as cur:
        cur.execute(_SAMPLE_SQL, {
            "tag_ids": list(tag_ids), "limit": limit, "pct": float(pct)})
        return [(int(r[0]), r[1]) for r in cur.fetchall()]


def candidates_by_ids(
    conn: psycopg.Connection, *, tag_ids: list[int], image_ids: list[int],
    limit: int,
) -> list[tuple[int, str]]:
    """The named images that are still eligible. Silently dropping the rest is
    the point: an id that names an exam member must not be labeled, however it
    reached the list."""
    with conn.cursor() as cur:
        cur.execute(_BY_IDS_SQL, {
            "tag_ids": list(tag_ids), "image_ids": [int(i) for i in image_ids],
            "limit": limit})
        return [(int(r[0]), r[1]) for r in cur.fetchall()]


def record_labels(
    conn: psycopg.Connection, *, image_id: int, verdicts: dict[int, str], model: str,
) -> dict[str, int]:
    """Write one image's verdicts as machine labels, grouped by state so the
    existing chokepoint does the writing (and its human-wins rail, its
    definition stamping and its trigger all apply unchanged)."""
    from toolkit import tag_annotations as ta

    by_state: dict[tuple[str, str | None], list[int]] = {}
    for tag_id, verdict in verdicts.items():
        if verdict not in VERDICT_STATE:
            raise ValueError(f"tag {tag_id}: verdict {verdict!r} is not yes/no/skip")
        by_state.setdefault(VERDICT_STATE[verdict], []).append(int(tag_id))
    written = 0
    for (state, reason), tag_ids in by_state.items():
        ta.bulk_set_state_for_image(
            conn, image_id=image_id, tag_ids=sorted(tag_ids), state=state,
            created_by=CREATED_BY, source=ta.SOURCE_MACHINE, model=model,
            excluded_reason=reason,
        )
        written += len(tag_ids)
    return {"image_id": image_id, "cells": written}


_LABELLED_COUNTS_SQL = """
    SELECT l.tag_id, l.state, count(*)::bigint
    FROM image_tag_labels l
    JOIN tag_definitions d
      ON d.tag_id = l.tag_id AND d.status = 'active' AND d.id = l.definition_id
    WHERE l.source = 'machine' AND l.tag_id = ANY(%(tag_ids)s::bigint[])
    GROUP BY l.tag_id, l.state
"""


def labelled_counts(
    conn: psycopg.Connection, *, tag_ids: list[int],
) -> dict[int, dict[str, int]]:
    """Per-head machine label counts under the CURRENT definition — the training
    set as it stands. Cells stamped with a superseded definition are not counted:
    they describe wording that no longer applies."""
    out: dict[int, dict[str, int]] = {
        int(t): {"positive": 0, "negative": 0, "excluded": 0} for t in tag_ids}
    with conn.cursor() as cur:
        cur.execute(_LABELLED_COUNTS_SQL, {"tag_ids": list(tag_ids)})
        for tag_id, state, count in cur.fetchall():
            out.setdefault(int(tag_id), {"positive": 0, "negative": 0, "excluded": 0})
            out[int(tag_id)][str(state)] = int(count)
    return out


_MEASURED_COST_SQL = """
    SELECT count(*)::int, COALESCE(avg(cost_usd), 0)::double precision
    FROM llm_calls
    WHERE called_for = %(called_for)s AND model = %(model)s AND cost_usd IS NOT NULL
"""


def measured_cost(
    conn: psycopg.Connection, *, model: str, called_for: str,
) -> tuple[int, float]:
    with conn.cursor() as cur:
        cur.execute(_MEASURED_COST_SQL, {"called_for": called_for, "model": model})
        row = cur.fetchone()
    return (int(row[0]), float(row[1])) if row else (0, 0.0)
