"""NEW DEDUP Wave 1 — the Labeling program's transient half (migration 373).

Two concerns, both scoped to the droppable `dedup_sim` schema (PROGRAM.md
plans to drop it wholesale at Wave 8 — nothing durable lives here; the
permanent taxonomy and ground truth moved to `toolkit/tag_annotations.py`
and its `tag_taxonomy` / `image_tag_labels` tables, migration 442):

* **Sample** (`dedup_sim.labeling_sample`) — which images are in scope for
  the secondary-CLIP relabel job. Grows on demand.
* **Proposals** (`dedup_sim.label_proposals`) — one row per (image, model):
  what the secondary CLIP encoder suggests. Setting a proposal's state
  resolves its label to a tag (self-registering a freehand correction) and
  writes the tri-state decision into `image_tag_labels` — the real ground
  truth — while the proposal row itself just gets marked confirmed/dismissed
  as a review-queue bookkeeping flag. Never writes `image_clip_tags` — that
  table feeds the live gallery badge, and a proposal isn't operator-approved
  yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg

from toolkit import tag_annotations

if TYPE_CHECKING:
    pass

GROW_SAMPLE_MAX = 2000
PROPOSAL_LIST_MAX = 200
BULK_PROPOSAL_MAX = tag_annotations.BULK_STATE_MAX


# --- sample ---------------------------------------------------------------

_GROW_SAMPLE_SQL = """
    INSERT INTO dedup_sim.labeling_sample (image_id, added_by)
    SELECT i.id, %(added_by)s
    FROM images i
    JOIN listings l ON l.id = i.listing_id
    LEFT JOIN properties p ON p.id = l.property_id
    WHERE i.storage_path IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM dedup_sim.labeling_sample s WHERE s.image_id = i.id
      )
      AND (%(category_main)s::text IS NULL OR p.category_main = %(category_main)s)
    ORDER BY i.id DESC
    LIMIT %(count)s
    ON CONFLICT (image_id) DO NOTHING
"""


def grow_sample(
    conn: psycopg.Connection, *, count: int, category_main: str | None = None,
    added_by: str = "operator",
) -> dict[str, Any]:
    """Add up to `count` newest not-yet-sampled images (optionally scoped to
    one property type) to the relabel sample. Purely a membership grow —
    the secondary-CLIP backfill script (scripts/label_proposal_backfill.py)
    is what actually proposes tags for them, run separately (GH Actions
    dispatch, per PROGRAM.md's compute-placement convention)."""
    if count < 1:
        raise ValueError("count must be at least 1")
    if count > GROW_SAMPLE_MAX:
        raise ValueError(f"at most {GROW_SAMPLE_MAX} images per grow call")
    with conn.cursor() as cur:
        cur.execute(
            _GROW_SAMPLE_SQL,
            {"added_by": added_by, "category_main": category_main, "count": count},
        )
        added = cur.rowcount
    return {"added": added}


# --- proposals --------------------------------------------------------------

# `proposed_at` is NOT a tiebreaker on its own: the backfill inserts a whole
# batch inside one transaction, so every row in it shares the same now(), and
# an unqualified ORDER BY proposed_at DESC leaves Postgres free to return ties
# in a different order on every call — the review grid reshuffling under the
# operator between refetches. image_id makes the sort total (it's in the PK).
#
# `current_state` is the image's tri-state decision for THIS proposal's own
# (possibly stale, if the operator hasn't reviewed it yet) label — resolved
# by joining tag_taxonomy on label text since a pending proposal's tag may
# not even exist there yet (get_or_create_tag_id only registers it at review
# time). NULL means untouched, same convention as list_images_for_tag.
_LIST_PROPOSALS_SQL = """
    SELECT lp.image_id, lp.model, lp.label, lp.confidence, lp.proposed_at, lp.status,
           lp.reviewed_at, lp.reviewed_by, itl.state AS current_state
    FROM dedup_sim.label_proposals lp
    LEFT JOIN tag_taxonomy tt ON tt.label = lp.label
    LEFT JOIN image_tag_labels itl ON itl.image_id = lp.image_id AND itl.tag_id = tt.id
    WHERE (%(status)s::text IS NULL OR %(status)s = 'all' OR lp.status = %(status)s)
      AND (%(label)s::text IS NULL OR lp.label = %(label)s)
    ORDER BY lp.proposed_at DESC, lp.image_id DESC
    LIMIT %(limit)s
"""

LIST_STATUSES = ("all", "pending", "confirmed", "dismissed")


def list_proposals(
    conn: psycopg.Connection, *, status: str | None = None, label: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List proposals for the review grid — the machine-suggestion queue.
    Every row carries `current_state`: the image's tri-state decision for
    the proposal's own label (None = untouched), so the caller can grey an
    already-decided tile without a second query."""
    limit = min(max(1, limit), PROPOSAL_LIST_MAX)
    with conn.cursor() as cur:
        cur.execute(
            _LIST_PROPOSALS_SQL,
            {"status": status, "label": label, "limit": limit},
        )
        rows = cur.fetchall()
    return [
        {
            "image_id": r[0], "model": r[1], "label": r[2], "confidence": r[3],
            "proposed_at": r[4], "status": r[5], "reviewed_at": r[6], "reviewed_by": r[7],
            "current_state": r[8],
        }
        for r in rows
    ]


def _proposal_status_for(state: str) -> str:
    return "confirmed" if state == "positive" else "dismissed"


def set_proposal_state(
    conn: psycopg.Connection, *, image_id: int, model: str, state: str,
    reviewed_by: str = "operator", label: str | None = None,
) -> dict[str, Any]:
    """Record the operator's tri-state verdict on one proposal: mark it
    confirmed (positive) or dismissed (negative/excluded) for bookkeeping,
    and write the real decision into image_tag_labels. Re-deciding an
    already-decided proposal is allowed and simply overwrites both rows
    together — unlike the old confirm/dismiss split, there is only ONE
    write path into image_tag_labels here, so nothing can diverge from a
    repeat call. Only a proposal that never existed for this
    (image_id, model) 404s.

    `label` overrides which tag the decision lands on when the operator
    corrects a wrong suggestion before deciding (the Labeling page's
    per-tile combobox). The proposal row keeps the model's own label
    untouched — that's the record of what the encoder actually predicted.
    A correction typed freehand self-registers in tag_taxonomy in the same
    transaction (tag_annotations.get_or_create_tag_id) — without that, an
    off-taxonomy label would be invisible to the coverage chart and the tag
    picker, both of which read tag_taxonomy, not image_tag_labels."""
    if state not in tag_annotations.STATES:
        raise ValueError(f"state must be one of {tag_annotations.STATES}")
    corrected = tag_annotations.clean_label(label) if label is not None and label.strip() else None
    proposal_status = _proposal_status_for(state)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE dedup_sim.label_proposals SET status = %s, "
            "  reviewed_at = now(), reviewed_by = %s "
            "WHERE image_id = %s AND model = %s "
            "RETURNING label",
            (proposal_status, reviewed_by, image_id, model),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError((image_id, model))
        proposed = row[0]
        final = corrected or proposed
        tag_id = tag_annotations.get_or_create_tag_id(conn, label=final, created_by=reviewed_by)
        tag_annotations.set_state(
            conn, image_id=image_id, tag_id=tag_id, state=state, created_by=reviewed_by,
        )
    return {
        "image_id": image_id, "model": model, "label": final, "state": state,
        "status": proposal_status, "proposed_label": proposed, "corrected": final != proposed,
    }


def bulk_set_proposal_state(
    conn: psycopg.Connection, *, model: str, image_ids: list[int], state: str,
    reviewed_by: str = "operator",
) -> dict[str, Any]:
    """Batch version of set_proposal_state for one model at once — the
    review queue's "looks right, take the whole batch" action. Each row
    keeps its own proposed label (the batch can span more than one tag).
    Re-deciding an already-decided row is allowed, same as the single-row
    version."""
    if state not in tag_annotations.STATES:
        raise ValueError(f"state must be one of {tag_annotations.STATES}")
    ids = list(dict.fromkeys(int(i) for i in image_ids))
    if not ids:
        raise ValueError("no proposals selected")
    if len(ids) > BULK_PROPOSAL_MAX:
        raise ValueError(f"at most {BULK_PROPOSAL_MAX} proposals per batch")
    proposal_status = _proposal_status_for(state)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE dedup_sim.label_proposals SET status = %s, "
            "  reviewed_at = now(), reviewed_by = %s "
            "WHERE model = %s AND image_id = ANY(%s) "
            "RETURNING image_id, label",
            (proposal_status, reviewed_by, model, ids),
        )
        rows = cur.fetchall()
        for image_id, label in rows:
            tag_id = tag_annotations.get_or_create_tag_id(conn, label=label, created_by=reviewed_by)
            tag_annotations.set_state(
                conn, image_id=image_id, tag_id=tag_id, state=state, created_by=reviewed_by,
            )
    return {
        "updated": len(rows), "model": model, "state": state,
        "image_ids": [r[0] for r in rows],
    }
