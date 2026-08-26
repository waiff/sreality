"""Tag annotation matrix (migration 442) — the permanent, per-(image, tag)
ground truth every future per-tag classifier head trains from.

Two concerns:
* Taxonomy (`tag_taxonomy`) — the operator-curated vocabulary. Promoted from
  `dedup_sim.taxonomy_labels` so it survives that schema's planned Wave-8 drop
  (docs/design/new-dedup/PROGRAM.md). A real surrogate key replaces the old
  text-keyed join, so a rename is one UPDATE instead of a cascade rewrite.
* Annotations (`image_tag_labels`) — one row per (image, tag) decision:
  positive, negative, or excluded. No row means untouched — displays and
  trains as negative once the image is in `dedup_sim.labeling_sample`
  (docs/design/tag-annotation-matrix.md's decisions ledger).
"""

from __future__ import annotations

from typing import Any

import psycopg

LABEL_MAX_CHARS = 100  # mirrors tag_taxonomy's CHECK (migration 442)
STATES = ("positive", "negative", "excluded")
BULK_STATE_MAX = 200
IMAGE_LIST_MAX = 200


def clean_label(label: str) -> str:
    clean = " ".join((label or "").split())
    if not clean:
        raise ValueError("a tag label needs a non-empty name")
    if len(clean) > LABEL_MAX_CHARS:
        raise ValueError(f"a tag label is at most {LABEL_MAX_CHARS} characters")
    return clean


# --- taxonomy -------------------------------------------------------------

_TAG_COLUMNS = "id, label, family, active, priority, ready_for_training, created_at"


def _tag_dict(r: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": r[0], "label": r[1], "family": r[2], "active": r[3],
        "priority": r[4], "ready_for_training": r[5], "created_at": r[6],
    }


def add_tag(
    conn: psycopg.Connection, *, label: str, family: str | None = None,
    created_by: str = "operator",
) -> dict[str, Any]:
    clean = clean_label(label)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tag_taxonomy (label, family, created_by) "
                f"VALUES (%s,%s,%s) RETURNING {_TAG_COLUMNS}",
                (clean, (family or "").strip() or None, created_by),
            )
            r = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise ValueError(f"tag {clean!r} already exists") from exc
    return _tag_dict(r)


def rename_tag(conn: psycopg.Connection, *, tag_id: int, new_label: str) -> dict[str, Any]:
    """Rename a tag. Unlike the old text-keyed taxonomy, this touches only
    tag_taxonomy — image_tag_labels references tag_id, not label text, so
    no cascade rewrite of dependent rows is needed."""
    clean = clean_label(new_label)
    with conn.cursor() as cur:
        try:
            cur.execute(
                f"UPDATE tag_taxonomy SET label = %s WHERE id = %s RETURNING {_TAG_COLUMNS}",
                (clean, tag_id),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise ValueError(f"tag {clean!r} already exists") from exc
        row = cur.fetchone()
        if row is None:
            raise KeyError(tag_id)
    return _tag_dict(row)


def set_tag_flags(
    conn: psycopg.Connection, *, tag_id: int,
    priority: bool | None = None, ready_for_training: bool | None = None,
) -> dict[str, Any]:
    """Update one or both operator flags on a tag — only the fields actually
    passed, so toggling one from the Modify labels popup never clobbers the
    other. `priority` pins a tag to the top of that popup and marks it red;
    `ready_for_training` is the operator's own call that a tag's set is solid
    enough for the (not yet built) per-tag trainer to consume — independent
    of Gate 1, which only says a tag is LABELED enough, not reviewed."""
    if priority is None and ready_for_training is None:
        raise ValueError("nothing to update")
    sets = []
    params: list[Any] = []
    if priority is not None:
        sets.append("priority = %s")
        params.append(priority)
    if ready_for_training is not None:
        sets.append("ready_for_training = %s")
        params.append(ready_for_training)
    params.append(tag_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE tag_taxonomy SET {', '.join(sets)} WHERE id = %s "
            f"RETURNING {_TAG_COLUMNS}",
            params,
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(tag_id)
    return _tag_dict(row)


def remove_tag(conn: psycopg.Connection, *, tag_id: int) -> dict[str, Any]:
    """Remove a tag. Every image_tag_labels row under it goes too (ON DELETE
    CASCADE on tag_id) — the images themselves are untouched."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT label FROM tag_taxonomy WHERE id = %s", (tag_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(tag_id)
        label = row[0]
        cur.execute("DELETE FROM image_tag_labels WHERE tag_id = %s", (tag_id,))
        deleted = cur.rowcount
        cur.execute("DELETE FROM tag_taxonomy WHERE id = %s", (tag_id,))
    return {"label": label, "deleted_annotations": deleted}


def get_or_create_tag_id(
    conn: psycopg.Connection, *, label: str, created_by: str = "operator",
) -> int:
    """Resolve a (possibly freehand) label to its tag_id, self-registering an
    off-taxonomy correction — the same open-vocabulary behaviour the old
    confirm_proposal had. Without this, a label typed only at review time
    would be invisible to the coverage chart and the tag picker, which both
    read tag_taxonomy, not image_tag_labels."""
    clean = clean_label(label)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tag_taxonomy WHERE label = %s", (clean,))
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute(
            "INSERT INTO tag_taxonomy (label, created_by) VALUES (%s,%s) "
            "ON CONFLICT (label) DO NOTHING RETURNING id",
            (clean, created_by),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute("SELECT id FROM tag_taxonomy WHERE label = %s", (clean,))
        return cur.fetchone()[0]


# --- annotations ------------------------------------------------------------

def set_state(
    conn: psycopg.Connection, *, image_id: int, tag_id: int, state: str,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Set one (image, tag) cell to positive/negative/excluded. Idempotent —
    re-setting the same or a different state on an existing cell just
    updates it, matching "no confirmation dialogs on individual toggles"."""
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO image_tag_labels (image_id, tag_id, state, created_by) "
            "VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (image_id, tag_id) DO UPDATE SET "
            "  state = excluded.state, updated_at = now() "
            "RETURNING image_id, tag_id, state, updated_at",
            (image_id, tag_id, state, created_by),
        )
        r = cur.fetchone()
    return {"image_id": r[0], "tag_id": r[1], "state": r[2], "updated_at": r[3]}


def bulk_set_state(
    conn: psycopg.Connection, *, image_ids: list[int], tag_id: int, state: str,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Batch version of set_state for one tag across many images — the
    labeling UI's main throughput lever."""
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    ids = list(dict.fromkeys(int(i) for i in image_ids))
    if not ids:
        raise ValueError("no images selected")
    if len(ids) > BULK_STATE_MAX:
        raise ValueError(f"at most {BULK_STATE_MAX} images per batch")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO image_tag_labels (image_id, tag_id, state, created_by) "
            "VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (image_id, tag_id) DO UPDATE SET "
            "  state = excluded.state, updated_at = now()",
            [(image_id, tag_id, state, created_by) for image_id in ids],
        )
    return {"updated": len(ids), "tag_id": tag_id, "state": state, "image_ids": ids}


def bulk_set_state_for_image(
    conn: psycopg.Connection, *, image_id: int, tag_ids: list[int], state: str,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Batch version of set_state for one image across many tags — the
    mirror of bulk_set_state (which fixes the tag and varies the image).
    Backs the detail panel's "set selected" action: an image the operator
    has actually looked at (e.g. a fitness room, clearly none of the room
    tags) can be closed out in one click instead of leaving 49 tags
    implicitly-but-not-explicitly negative."""
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    ids = list(dict.fromkeys(int(i) for i in tag_ids))
    if not ids:
        raise ValueError("no tags selected")
    if len(ids) > BULK_STATE_MAX:
        raise ValueError(f"at most {BULK_STATE_MAX} tags per batch")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO image_tag_labels (image_id, tag_id, state, created_by) "
            "VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (image_id, tag_id) DO UPDATE SET "
            "  state = excluded.state, updated_at = now()",
            [(image_id, tag_id, state, created_by) for tag_id in ids],
        )
    return {"updated": len(ids), "image_id": image_id, "state": state, "tag_ids": ids}


def clear_state(conn: psycopg.Connection, *, image_id: int, tag_id: int) -> dict[str, Any]:
    """Revert a cell to untouched by deleting its explicit row."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM image_tag_labels WHERE image_id = %s AND tag_id = %s",
            (image_id, tag_id),
        )
        deleted = cur.rowcount > 0
    return {"image_id": image_id, "tag_id": tag_id, "deleted": deleted}


# `s.added_at DESC, s.image_id DESC` is a total order for the same reason
# dedup_sim_labeling's proposal list needs one: a stable tiebreaker so the
# grid doesn't reshuffle under the operator between refetches.
_LIST_IMAGES_FOR_TAG_SQL = """
    SELECT s.image_id, i.storage_path, itl.state, itl.updated_at, itl.created_by
    FROM dedup_sim.labeling_sample s
    JOIN images i ON i.id = s.image_id
    LEFT JOIN image_tag_labels itl ON itl.image_id = s.image_id AND itl.tag_id = %(tag_id)s
    WHERE (
      %(state)s::text IS NULL
      OR (%(state)s = 'untouched' AND itl.state IS NULL)
      OR itl.state = %(state)s
    )
    ORDER BY s.added_at DESC, s.image_id DESC
    LIMIT %(limit)s
"""


_LIST_TAGS_FOR_IMAGE_SQL = """
    SELECT t.id, t.label, t.family, itl.state, itl.updated_at
    FROM tag_taxonomy t
    LEFT JOIN image_tag_labels itl ON itl.tag_id = t.id AND itl.image_id = %(image_id)s
    WHERE t.active
    ORDER BY t.family NULLS LAST, t.label
"""


def list_tags_for_image(conn: psycopg.Connection, *, image_id: int) -> list[dict[str, Any]]:
    """Image-centric view: every active tag with this image's current state
    (None = untouched). Backs the detail panel for the "one photo, several
    tags at once" case (e.g. an open kitchen-living room: kitchen positive,
    living_room excluded, everything else negative) — the mirror image of
    list_images_for_tag, grouped by family for a scannable panel."""
    with conn.cursor() as cur:
        cur.execute(_LIST_TAGS_FOR_IMAGE_SQL, {"image_id": image_id})
        rows = cur.fetchall()
    return [
        {
            "id": r[0], "label": r[1], "family": r[2],
            "state": r[3] or "untouched", "updated_at": r[4],
        }
        for r in rows
    ]


BATCH_IMAGE_MAX = 200

_LIST_POSITIVE_TAGS_FOR_IMAGES_SQL = """
    SELECT itl.image_id, t.id, t.label
    FROM image_tag_labels itl
    JOIN tag_taxonomy t ON t.id = itl.tag_id
    WHERE itl.state = 'positive' AND itl.image_id = ANY(%(image_ids)s)
    ORDER BY itl.image_id, t.label
"""


def list_positive_tags_for_images(
    conn: psycopg.Connection, *, image_ids: list[int],
) -> list[dict[str, Any]]:
    """Every positive tag on each of several images, in one query — the
    labeling grid's "what's already assigned to this image" line under each
    tile. A tile only shows the one tag it's reviewing; with multi-label
    images now possible, that's not the same as everything the image is
    already positive on, so this batches the lookup instead of one query per
    visible tile."""
    ids = list(dict.fromkeys(int(i) for i in image_ids))
    if not ids:
        return []
    if len(ids) > BATCH_IMAGE_MAX:
        raise ValueError(f"at most {BATCH_IMAGE_MAX} images per batch")
    with conn.cursor() as cur:
        cur.execute(_LIST_POSITIVE_TAGS_FOR_IMAGES_SQL, {"image_ids": ids})
        rows = cur.fetchall()
    return [{"image_id": r[0], "tag_id": r[1], "label": r[2]} for r in rows]


def list_images_for_tag(
    conn: psycopg.Connection, *, tag_id: int, state: str | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    """Tag-centric browse: every image in the labeling sample, with its
    current state for this one tag (state=None in the response means
    untouched/defaulted-negative). Backs the "kitchen = excluded" filter —
    unlike dedup_sim_labeling.list_proposals (which lists machine
    SUGGESTIONS), this lists the SAMPLE itself, so an image the secondary
    CLIP never proposed this tag for is still reachable and reviewable."""
    valid_states = (*STATES, "untouched")
    if state is not None and state not in valid_states:
        raise ValueError(f"state must be one of {valid_states}")
    limit = min(max(1, limit), IMAGE_LIST_MAX)
    with conn.cursor() as cur:
        cur.execute(_LIST_IMAGES_FOR_TAG_SQL, {"tag_id": tag_id, "state": state, "limit": limit})
        rows = cur.fetchall()
    return [
        {
            "image_id": r[0], "storage_path": r[1], "state": r[2] or "untouched",
            "updated_at": r[3], "created_by": r[4],
        }
        for r in rows
    ]


# gate_count vs positive_count carries over the 2026-08-21 operator decision
# from the old taxonomy_overview (docs: dedup_sim_labeling.py's history) — a
# border case is not evidence a tag is learnable, so it doesn't count toward
# Gate 1, but it stays in the honest positive_count inventory. Computed here,
# not subtracted client-side, so the gate predicate has one definition.
_OVERVIEW_SQL = """
    SELECT
      t.id, t.label, t.family, t.active, t.priority, t.ready_for_training, t.created_at,
      COALESCE(c.positive_count, 0) AS positive_count,
      COALESCE(c.gate_count, 0) AS gate_count,
      COALESCE(c.border_case_count, 0) AS border_case_count,
      COALESCE(c.negative_count, 0) AS negative_count,
      COALESCE(c.excluded_count, 0) AS excluded_count,
      COALESCE(p.pending_count, 0) AS pending_count,
      COALESCE(p.dismissed_count, 0) AS dismissed_count
    FROM tag_taxonomy t
    LEFT JOIN (
      SELECT itl.tag_id,
        count(*) FILTER (WHERE itl.state = 'positive') AS positive_count,
        count(*) FILTER (WHERE itl.state = 'positive' AND bc.image_id IS NULL) AS gate_count,
        count(*) FILTER (WHERE itl.state = 'positive' AND bc.image_id IS NOT NULL) AS border_case_count,
        count(*) FILTER (WHERE itl.state = 'negative') AS negative_count,
        count(*) FILTER (WHERE itl.state = 'excluded') AS excluded_count
      FROM image_tag_labels itl
      LEFT JOIN image_border_cases bc ON bc.image_id = itl.image_id
      GROUP BY itl.tag_id
    ) c ON c.tag_id = t.id
    LEFT JOIN (
      SELECT lp.label,
        count(*) FILTER (WHERE lp.status = 'pending') AS pending_count,
        count(*) FILTER (WHERE lp.status = 'dismissed') AS dismissed_count
      FROM dedup_sim.label_proposals lp
      GROUP BY lp.label
    ) p ON p.label = t.label
    ORDER BY t.label
"""


def tag_overview(conn: psycopg.Connection) -> dict[str, Any]:
    """Every tag with its tri-state counts (positive/negative/excluded, plus
    the Gate-1 countable slice of positive and the border-cased remainder)
    and pending/dismissed proposal counts — the single GET the labeling
    page's coverage strip renders from."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dedup_sim.labeling_sample")
        sample_size = cur.fetchone()[0]
        cur.execute(_OVERVIEW_SQL)
        rows = cur.fetchall()
    tags = [
        {
            "id": r[0], "label": r[1], "family": r[2], "active": r[3],
            "priority": r[4], "ready_for_training": r[5], "created_at": r[6],
            "positive_count": r[7], "gate_count": r[8], "border_case_count": r[9],
            "negative_count": r[10], "excluded_count": r[11],
            "pending_count": r[12], "dismissed_count": r[13],
        }
        for r in rows
    ]
    return {"sample_size": sample_size, "tags": tags}
