"""NEW DEDUP Wave 1 — the Labeling program's data layer (migration 373).

Three concerns, one module (they're small and load-bearing on each other —
confirming a proposal writes `image_training_examples`, removing a taxonomy
label purges both `image_training_examples` and `label_proposals`):

* **Taxonomy** (`dedup_sim.taxonomy_labels`) — the operator-curated Taxonomy
  v1 vocabulary. Free text, add/rename/remove; never a hardcoded list here
  (PROGRAM.md: "tag-family defaults reconfirmed at training-set
  finalization" — the exact label set is explicitly not decided yet).
* **Sample** (`dedup_sim.labeling_sample`) — which images are in scope for
  the secondary-CLIP relabel job. Grows on demand.
* **Proposals** (`dedup_sim.label_proposals`) — one row per (image, model):
  what the secondary CLIP encoder suggests. Confirming one upserts into
  `image_training_examples` (the real, confirmed training set); dismissing
  one just marks it reviewed. Never writes `image_clip_tags` — that table
  feeds the live gallery badge, and a proposal isn't operator-approved yet.
  Listing the 'confirmed' status pulls in the WHOLE training set, not just
  proposals reviewed on this page — see `list_proposals`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg

if TYPE_CHECKING:
    pass

LABEL_MAX_CHARS = 100  # mirrors dedup_sim.taxonomy_labels' CHECK (migration 373)
GROW_SAMPLE_MAX = 2000
PROPOSAL_LIST_MAX = 200


def _clean_label(label: str) -> str:
    clean = " ".join((label or "").split())
    if not clean:
        raise ValueError("a taxonomy label needs a non-empty name")
    if len(clean) > LABEL_MAX_CHARS:
        raise ValueError(f"a taxonomy label is at most {LABEL_MAX_CHARS} characters")
    return clean


# --- taxonomy -----------------------------------------------------------

_OVERVIEW_SQL = """
    SELECT
      t.id, t.label, t.family, t.active, t.created_at,
      COALESCE(te.confirmed_count, 0) AS confirmed_count,
      COALESCE(p.pending_count, 0) AS pending_count,
      COALESCE(p.dismissed_count, 0) AS dismissed_count
    FROM dedup_sim.taxonomy_labels t
    LEFT JOIN (
      SELECT label, count(*) AS confirmed_count
      FROM image_training_examples
      GROUP BY label
    ) te ON te.label = t.label
    LEFT JOIN (
      SELECT label,
        count(*) FILTER (WHERE status = 'pending') AS pending_count,
        count(*) FILTER (WHERE status = 'dismissed') AS dismissed_count
      FROM dedup_sim.label_proposals
      GROUP BY label
    ) p ON p.label = t.label
    ORDER BY t.label
"""


def taxonomy_overview(conn: psycopg.Connection) -> dict[str, Any]:
    """Every taxonomy label with its confirmed/pending/dismissed counts, plus
    the current sample size — the single GET the Labeling page's coverage
    strip renders from (mirrors ClipAudit's TrainingSetSummary)."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dedup_sim.labeling_sample")
        sample_size = cur.fetchone()[0]
        cur.execute(_OVERVIEW_SQL)
        rows = cur.fetchall()
    labels = [
        {
            "id": r[0], "label": r[1], "family": r[2], "active": r[3],
            "created_at": r[4], "confirmed_count": r[5], "pending_count": r[6],
            "dismissed_count": r[7],
        }
        for r in rows
    ]
    return {"sample_size": sample_size, "labels": labels}


def add_taxonomy_label(
    conn: psycopg.Connection, *, label: str, family: str | None = None,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Add one label to the Taxonomy v1 vocabulary. Empty sample/coverage
    until the relabel job proposes it or the operator trains an image by
    hand — this only registers the name."""
    clean = _clean_label(label)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dedup_sim.taxonomy_labels (label, family, created_by) "
                "VALUES (%s,%s,%s) "
                "RETURNING id, label, family, active, created_at",
                (clean, (family or "").strip() or None, created_by),
            )
            r = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise ValueError(f"taxonomy label {clean!r} already exists") from exc
    return {"id": r[0], "label": r[1], "family": r[2], "active": r[3], "created_at": r[4]}


def rename_taxonomy_label(
    conn: psycopg.Connection, *, label_id: int, new_label: str,
    updated_by: str = "operator",
) -> dict[str, Any]:
    """Rename a taxonomy label. Cascades: every image_training_examples row
    and every label_proposals row carrying the OLD text moves to the new
    one in the same transaction, so a rename never silently orphans
    existing confirmed/proposed data under the stale spelling."""
    clean = _clean_label(new_label)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT label FROM dedup_sim.taxonomy_labels WHERE id = %s", (label_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(label_id)
        old_label = row[0]
        if old_label != clean:
            try:
                cur.execute(
                    "UPDATE dedup_sim.taxonomy_labels SET label = %s WHERE id = %s",
                    (clean, label_id),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise ValueError(f"taxonomy label {clean!r} already exists") from exc
            cur.execute(
                "UPDATE image_training_examples SET label = %s, updated_at = now() "
                "WHERE label = %s",
                (clean, old_label),
            )
            cur.execute(
                "UPDATE dedup_sim.label_proposals SET label = %s WHERE label = %s",
                (clean, old_label),
            )
        cur.execute(
            "SELECT id, label, family, active, created_at "
            "FROM dedup_sim.taxonomy_labels WHERE id = %s",
            (label_id,),
        )
        r = cur.fetchone()
    return {"id": r[0], "label": r[1], "family": r[2], "active": r[3], "created_at": r[4]}


def remove_taxonomy_label(conn: psycopg.Connection, *, label_id: int) -> dict[str, Any]:
    """Remove a taxonomy label. Every image_training_examples row and every
    label_proposals row under it goes too (images themselves are
    untouched) — same "images stay, only the assignment rows go" semantics
    as the ClipAudit label-chip trash (api/labeling.py's
    delete_training_label), extended to also clear pending proposals so
    nothing points at a retired label."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT label FROM dedup_sim.taxonomy_labels WHERE id = %s", (label_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(label_id)
        label = row[0]
        cur.execute(
            "DELETE FROM image_training_examples WHERE label = %s", (label,),
        )
        deleted_training = cur.rowcount
        cur.execute(
            "DELETE FROM dedup_sim.label_proposals WHERE label = %s", (label,),
        )
        deleted_proposals = cur.rowcount
        cur.execute("DELETE FROM dedup_sim.taxonomy_labels WHERE id = %s", (label_id,))
    return {
        "label": label,
        "deleted_training_examples": deleted_training,
        "deleted_proposals": deleted_proposals,
    }


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
_LIST_PROPOSALS_SQL = """
    SELECT lp.image_id, lp.model, lp.label, lp.confidence, lp.proposed_at, lp.status,
           lp.reviewed_at, lp.reviewed_by, te.label AS trained_label
    FROM dedup_sim.label_proposals lp
    LEFT JOIN image_training_examples te ON te.image_id = lp.image_id
    WHERE (%(status)s::text IS NULL OR lp.status = %(status)s)
      AND (%(label)s::text IS NULL OR lp.label = %(label)s)
    ORDER BY lp.proposed_at DESC, lp.image_id DESC
    LIMIT %(limit)s
"""

# 'confirmed' means something wider than "a label_proposals row we flipped": most of the
# real training set (image_training_examples) predates this page and was written straight
# from /phash-audit's Train CTA, never through a proposal, and that older page's Train CTA
# (api/labeling.py's set_training_example/bulk_set_training_examples) stays live and can
# still relabel an image AFTER it was confirmed here — it only ever touches
# image_training_examples, never label_proposals. So this drives FROM
# image_training_examples (one row per image_id, always the CURRENT label) and only
# LEFT JOINs label_proposals for display provenance (model/confidence/who-reviewed-it);
# a naive UNION keyed off label_proposals.label would silently show a stale label once the
# two tables diverge. DISTINCT ON picks the most-recently-confirmed proposal per image (an
# image can accumulate more than one confirmed proposal across models over time) or, if
# none exists, synthesizes model='manual' from the training example itself.
_LIST_CONFIRMED_SQL = """
    WITH confirmed AS (
      SELECT DISTINCT ON (te.image_id)
        te.image_id,
        COALESCE(lp.model, 'manual') AS model,
        te.label,
        lp.confidence,
        COALESCE(lp.proposed_at, te.created_at) AS proposed_at,
        'confirmed'::text AS status,
        COALESCE(lp.reviewed_at, te.updated_at) AS reviewed_at,
        COALESCE(lp.reviewed_by, te.created_by) AS reviewed_by
      FROM image_training_examples te
      LEFT JOIN dedup_sim.label_proposals lp
        ON lp.image_id = te.image_id AND lp.status = 'confirmed'
      WHERE (%(label)s::text IS NULL OR te.label = %(label)s)
      ORDER BY te.image_id, lp.proposed_at DESC NULLS LAST
    )
    SELECT image_id, model, label, confidence, proposed_at, status,
           reviewed_at, reviewed_by, label AS trained_label
    FROM confirmed
    ORDER BY proposed_at DESC, image_id DESC
    LIMIT %(limit)s
"""

# The 'all' tab: the union of the other three, so a tile keeps its position
# when its status changes under it (the page greys reviewed rows rather than
# moving them). Same two sources as above — label_proposals for what a model
# suggested, image_training_examples for what the operator actually confirmed
# — with `trained_label` carried on every row so the page can grey the
# already-tagged ones without a second query. A confirmed row shows the
# CURRENT training label for the same reason _LIST_CONFIRMED_SQL does: the
# proposal keeps the model's own prediction, which goes stale the moment the
# operator corrects it.
_LIST_ALL_SQL = """
    WITH all_rows AS (
      SELECT
        lp.image_id,
        lp.model,
        CASE WHEN lp.status = 'confirmed' THEN COALESCE(te.label, lp.label)
             ELSE lp.label END AS label,
        lp.confidence,
        lp.proposed_at,
        lp.status,
        lp.reviewed_at,
        lp.reviewed_by,
        te.label AS trained_label
      FROM dedup_sim.label_proposals lp
      LEFT JOIN image_training_examples te ON te.image_id = lp.image_id
      UNION ALL
      SELECT
        te.image_id, 'manual'::text, te.label, NULL::real, te.created_at,
        'confirmed'::text, te.updated_at, te.created_by, te.label
      FROM image_training_examples te
      WHERE NOT EXISTS (
        SELECT 1 FROM dedup_sim.label_proposals lp WHERE lp.image_id = te.image_id
      )
    )
    SELECT image_id, model, label, confidence, proposed_at, status,
           reviewed_at, reviewed_by, trained_label
    FROM all_rows
    WHERE (%(label)s::text IS NULL OR label = %(label)s)
    ORDER BY proposed_at DESC, image_id DESC
    LIMIT %(limit)s
"""

LIST_STATUSES = ("all", "pending", "confirmed", "dismissed")


def list_proposals(
    conn: psycopg.Connection, *, status: str | None = None, label: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List proposals for the review grid. status='confirmed' is special-cased to union
    in image_training_examples rows with no matching confirmed proposal (see
    _LIST_CONFIRMED_SQL); status='all' is the union of all three tabs (_LIST_ALL_SQL);
    every other status is a plain label_proposals filter. Every row carries
    `trained_label` — the image's current image_training_examples label, or None —
    so the caller can tell an already-tagged image from an untouched one."""
    limit = min(max(1, limit), PROPOSAL_LIST_MAX)
    if status == "confirmed":
        sql, params = _LIST_CONFIRMED_SQL, {"label": label, "limit": limit}
    elif status == "all":
        sql, params = _LIST_ALL_SQL, {"label": label, "limit": limit}
    else:
        sql, params = _LIST_PROPOSALS_SQL, {"status": status, "label": label, "limit": limit}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {
            "image_id": r[0], "model": r[1], "label": r[2], "confidence": r[3],
            "proposed_at": r[4], "status": r[5], "reviewed_at": r[6], "reviewed_by": r[7],
            "trained_label": r[8],
        }
        for r in rows
    ]


def confirm_proposal(
    conn: psycopg.Connection, *, image_id: int, model: str, reviewed_by: str = "operator",
    label: str | None = None,
) -> dict[str, Any]:
    """Accept a proposal: mark it confirmed AND upsert a label into
    image_training_examples — the one write path that ever promotes a
    sim-side proposal into the real, confirmed training set. Only a
    'pending' proposal can be confirmed (mirrors bulk_confirm_proposals'
    guard) — a stale/repeated call against an already-reviewed proposal
    404s instead of silently re-flipping it or re-writing a training
    example a dismiss may have since superseded.

    `label` overrides what lands in the training set when the operator
    corrects a wrong suggestion before accepting it (the Labeling page's
    per-tile combobox). The proposal row KEEPS the model's own label
    untouched — that's the record of what the encoder actually predicted,
    and the correction stays derivable by comparing it against
    image_training_examples, so no extra column is needed to capture
    "model said X, operator said Y".

    A correction the operator typed freehand is REGISTERED in
    dedup_sim.taxonomy_labels as part of the same transaction. Without
    that, an off-taxonomy label would be a dead end: the coverage chart
    and the tag picker both read the taxonomy table (not the training
    set), and the secondary-CLIP backfill only ever scores against
    `taxonomy_labels WHERE active` — so the class would be invisible,
    un-reofferable, and impossible for the model to ever propose. This is
    the same open-vocabulary behaviour /clip-audit has, where the picker
    reads image_training_examples directly and free text self-registers;
    here the vocabulary lives in its own table, so it takes an explicit
    write (migration 379 backfilled exactly this gap once already)."""
    corrected = _clean_label(label) if label is not None and label.strip() else None
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE dedup_sim.label_proposals SET status = 'confirmed', "
            "  reviewed_at = now(), reviewed_by = %s "
            "WHERE image_id = %s AND model = %s AND status = 'pending' "
            "RETURNING label",
            (reviewed_by, image_id, model),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError((image_id, model))
        proposed = row[0]
        final = corrected or proposed
        if final != proposed:
            cur.execute(
                "INSERT INTO dedup_sim.taxonomy_labels (label, created_by) "
                "VALUES (%s,%s) ON CONFLICT (label) DO NOTHING",
                (final, reviewed_by),
            )
        cur.execute(
            "INSERT INTO image_training_examples (image_id, label, created_by) "
            "VALUES (%s,%s,%s) "
            "ON CONFLICT (image_id) DO UPDATE SET "
            "  label = excluded.label, updated_at = now()",
            (image_id, final, reviewed_by),
        )
    return {
        "image_id": image_id, "model": model, "label": final, "status": "confirmed",
        "proposed_label": proposed, "corrected": final != proposed,
    }


def dismiss_proposal(
    conn: psycopg.Connection, *, image_id: int, model: str, reviewed_by: str = "operator",
) -> dict[str, Any]:
    """Reject a proposal. Stays in the table (status='dismissed') as a
    record, so a future relabel run for the same model can be told not to
    re-surface it. Only a 'pending' proposal can be dismissed (mirrors
    bulk_dismiss_proposals' guard) — dismissing an already-confirmed
    proposal would otherwise flip label_proposals.status without ever
    retracting the image_training_examples row the confirm already wrote,
    silently diverging the two stores."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dedup_sim.label_proposals SET status = 'dismissed', "
            "  reviewed_at = now(), reviewed_by = %s "
            "WHERE image_id = %s AND model = %s AND status = 'pending' "
            "RETURNING label",
            (reviewed_by, image_id, model),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError((image_id, model))
    return {"image_id": image_id, "model": model, "label": row[0], "status": "dismissed"}


BULK_PROPOSAL_MAX = 200


def bulk_confirm_proposals(
    conn: psycopg.Connection, *, model: str, image_ids: list[int],
    reviewed_by: str = "operator",
) -> dict[str, Any]:
    """Accept many pending proposals for one model at once — the review
    queue's "looks right, take the whole batch" action. Each accepted
    proposal upserts its own label into image_training_examples (the
    labels can differ across the batch; this isn't a relabel-to-one-value
    bulk write like bulk_set_training_examples in api/labeling.py)."""
    ids = list(dict.fromkeys(int(i) for i in image_ids))
    if not ids:
        raise ValueError("no proposals selected")
    if len(ids) > BULK_PROPOSAL_MAX:
        raise ValueError(f"at most {BULK_PROPOSAL_MAX} proposals per batch")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE dedup_sim.label_proposals SET status = 'confirmed', "
            "  reviewed_at = now(), reviewed_by = %s "
            "WHERE model = %s AND image_id = ANY(%s) AND status = 'pending' "
            "RETURNING image_id, label",
            (reviewed_by, model, ids),
        )
        rows = cur.fetchall()
        if rows:
            cur.executemany(
                "INSERT INTO image_training_examples (image_id, label, created_by) "
                "VALUES (%s,%s,%s) "
                "ON CONFLICT (image_id) DO UPDATE SET "
                "  label = excluded.label, updated_at = now()",
                [(image_id, label, reviewed_by) for image_id, label in rows],
            )
    return {"confirmed": len(rows), "model": model, "image_ids": [r[0] for r in rows]}


def bulk_dismiss_proposals(
    conn: psycopg.Connection, *, model: str, image_ids: list[int],
    reviewed_by: str = "operator",
) -> dict[str, Any]:
    ids = list(dict.fromkeys(int(i) for i in image_ids))
    if not ids:
        raise ValueError("no proposals selected")
    if len(ids) > BULK_PROPOSAL_MAX:
        raise ValueError(f"at most {BULK_PROPOSAL_MAX} proposals per batch")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dedup_sim.label_proposals SET status = 'dismissed', "
            "  reviewed_at = now(), reviewed_by = %s "
            "WHERE model = %s AND image_id = ANY(%s) AND status = 'pending'",
            (reviewed_by, model, ids),
        )
        dismissed = cur.rowcount
    return {"dismissed": dismissed, "model": model, "image_ids": ids}
