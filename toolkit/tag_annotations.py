"""Tag annotation matrix (migration 442) — the permanent, per-(image, tag)
ground truth every future per-tag classifier head trains from.

Two concerns:
* Taxonomy (`tag_taxonomy`) — the operator-curated vocabulary. Promoted from
  `dedup_sim.taxonomy_labels` so it survives that schema's planned Wave-8 drop
  (docs/design/new-dedup/PROGRAM.md). A real surrogate key replaces the old
  text-keyed join, so a rename is one UPDATE instead of a cascade rewrite.
* Annotations (`image_tag_labels`) — one row per (image, tag) decision:
  positive, negative, or excluded. No row means UNTOUCHED, and untouched never
  trains as negative (operator ruling 2026-08-27, superseding migration 442's
  pool-scoped default-negative): an image never reviewed for a tag must stay
  distinguishable from one reviewed and judged not-that-tag. Membership of the
  `tag_candidates` review queue (migration 450) confers no label of any kind.
* Provenance (migration 446) — every decision also records WHO decided it
  (`source`), under WHICH written definition (`definition_id`, migration 445),
  when a human last checked it (`verified_at`) and, on an excluded cell, WHY
  (`excluded_reason`: ambiguous vs pruned). The append-only history behind it
  (`image_tag_label_events`) is written by a database TRIGGER, never from here
  — a log every future writer must remember to append to is a log with holes.
"""

from __future__ import annotations

from typing import Any

import psycopg

from toolkit.tag_holdout import exclusion_for

LABEL_MAX_CHARS = 100  # mirrors tag_taxonomy's CHECK (migration 442)
STATES = ("positive", "negative", "excluded")
BULK_STATE_MAX = 200
IMAGE_LIST_MAX = 200

SOURCE_HUMAN = "human"
SOURCE_HUMAN_CONFIRMED = "human_confirmed"
SOURCE_MACHINE = "machine"
SOURCE_BACKFILL_442 = "backfill_442"
SOURCES = (SOURCE_HUMAN, SOURCE_HUMAN_CONFIRMED, SOURCE_MACHINE, SOURCE_BACKFILL_442)
# backfill_442 is historical fact, never something a caller may claim.
WRITABLE_SOURCES = (SOURCE_HUMAN, SOURCE_HUMAN_CONFIRMED, SOURCE_MACHINE)
HUMAN_SOURCES = (SOURCE_HUMAN, SOURCE_HUMAN_CONFIRMED)
MACHINE_SOURCES = (SOURCE_MACHINE, SOURCE_HUMAN_CONFIRMED)

EXCLUDED_AMBIGUOUS = "ambiguous"
EXCLUDED_PRUNED = "pruned"
EXCLUDED_REASONS = (EXCLUDED_AMBIGUOUS, EXCLUDED_PRUNED)

# Above this share of a tag's decisions, the tag's DEFINITION is the problem, not
# the labeling. Named once, computed once (in _OVERVIEW_SQL), echoed to the SPA in
# tag_overview's payload — never a literal sprinkled through Python or TS.
AMBIGUITY_RATE_THRESHOLD = 0.15
# Below this many decisions the rate is reported but the alert does not fire:
# 3-of-5 is 60 percent and means nothing.
AMBIGUITY_MIN_DECISIONS = 20


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


def _clean_provenance(
    state: str, source: str, model: str | None, excluded_reason: str | None,
) -> tuple[str, str | None, str | None]:
    """Validate the vocabulary and normalise the two conditional fields so a
    caller can never trip the CHECKs from Python — the fakes cannot catch a
    CHECK violation, so the toolkit must not be able to produce one."""
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    if source not in WRITABLE_SOURCES:
        raise ValueError(f"source must be one of {WRITABLE_SOURCES}")
    if excluded_reason is not None and excluded_reason not in EXCLUDED_REASONS:
        raise ValueError(f"excluded_reason must be one of {EXCLUDED_REASONS}")
    reason = excluded_reason if state == "excluded" else None
    named_model = model if source in MACHINE_SOURCES else None
    return state, named_model, reason


# definition_id is resolved HERE, from the annotation's own tag, at write time —
# never a parameter. That is what makes "requeue exactly the annotations decided
# under the old definition" sound, and what makes citing another tag's definition
# structurally impossible. Served by tag_definitions_one_active_idx.
#
# verified_at is derived from `verified`, not supplied: no caller knows better
# than now(), and coalesce on conflict means a machine can never erase a human's
# verification.
#
# The DO UPDATE's WHERE is the human-wins rail: a machine write lands only on a
# cell that is untouched, machine-written or backfill. A human write always lands.
# When it is suppressed, RETURNING emits nothing — set_state reports applied=false.
_UPSERT_STATE_RETURNING_SQL = """
    INSERT INTO image_tag_labels (
      image_id, tag_id, state, created_by, source, definition_id, model,
      excluded_reason, verified_at
    )
    VALUES (
      %(image_id)s, %(tag_id)s, %(state)s, %(created_by)s, %(source)s,
      (SELECT id FROM tag_definitions
        WHERE tag_id = %(tag_id)s AND status = 'active'),
      %(model)s, %(excluded_reason)s,
      CASE WHEN %(verified)s THEN now() END
    )
    ON CONFLICT (image_id, tag_id) DO UPDATE SET
      state = excluded.state,
      source = excluded.source,
      definition_id = excluded.definition_id,
      model = excluded.model,
      excluded_reason = excluded.excluded_reason,
      verified_at = coalesce(excluded.verified_at, image_tag_labels.verified_at),
      updated_at = now()
    WHERE excluded.source <> 'machine'
       OR image_tag_labels.source IN ('machine', 'backfill_442')
    RETURNING image_id, tag_id, state, source, excluded_reason, definition_id,
              verified_at, updated_at
"""

_UPSERT_STATE_SQL = """
    INSERT INTO image_tag_labels (
      image_id, tag_id, state, created_by, source, definition_id, model,
      excluded_reason, verified_at
    )
    VALUES (
      %(image_id)s, %(tag_id)s, %(state)s, %(created_by)s, %(source)s,
      (SELECT id FROM tag_definitions
        WHERE tag_id = %(tag_id)s AND status = 'active'),
      %(model)s, %(excluded_reason)s,
      CASE WHEN %(verified)s THEN now() END
    )
    ON CONFLICT (image_id, tag_id) DO UPDATE SET
      state = excluded.state,
      source = excluded.source,
      definition_id = excluded.definition_id,
      model = excluded.model,
      excluded_reason = excluded.excluded_reason,
      verified_at = coalesce(excluded.verified_at, image_tag_labels.verified_at),
      updated_at = now()
    WHERE excluded.source <> 'machine'
       OR image_tag_labels.source IN ('machine', 'backfill_442')
"""

_READ_STATE_SQL = """
    SELECT image_id, tag_id, state, source, excluded_reason, definition_id,
           verified_at, updated_at
    FROM image_tag_labels
    WHERE image_id = %(image_id)s AND tag_id = %(tag_id)s
"""


def _cell_dict(r: tuple[Any, ...], *, applied: bool) -> dict[str, Any]:
    return {
        "image_id": r[0], "tag_id": r[1], "state": r[2], "source": r[3],
        "excluded_reason": r[4], "definition_id": r[5], "verified_at": r[6],
        "updated_at": r[7], "applied": applied,
    }


def _upsert_params(
    *, image_id: int, tag_id: int, state: str, created_by: str, source: str,
    model: str | None, excluded_reason: str | None,
) -> dict[str, Any]:
    return {
        "image_id": image_id, "tag_id": tag_id, "state": state,
        "created_by": created_by, "source": source, "model": model,
        "excluded_reason": excluded_reason, "verified": source in HUMAN_SOURCES,
    }


def set_state(
    conn: psycopg.Connection, *, image_id: int, tag_id: int, state: str,
    created_by: str = "operator", source: str = SOURCE_HUMAN,
    model: str | None = None, excluded_reason: str | None = None,
) -> dict[str, Any]:
    """Set one (image, tag) cell to positive/negative/excluded. Idempotent —
    re-setting the same or a different state on an existing cell just
    updates it, matching "no confirmation dialogs on individual toggles".
    `applied` is False when the human-wins rail refused a machine write onto
    a cell a person had already decided."""
    state, model, excluded_reason = _clean_provenance(state, source, model, excluded_reason)
    with conn.cursor() as cur:
        cur.execute(
            _UPSERT_STATE_RETURNING_SQL,
            _upsert_params(
                image_id=image_id, tag_id=tag_id, state=state, created_by=created_by,
                source=source, model=model, excluded_reason=excluded_reason,
            ),
        )
        r = cur.fetchone()
        if r is not None:
            return _cell_dict(r, applied=True)
        cur.execute(_READ_STATE_SQL, {"image_id": image_id, "tag_id": tag_id})
        standing = cur.fetchone()
    return _cell_dict(standing, applied=False)


def bulk_set_state(
    conn: psycopg.Connection, *, image_ids: list[int], tag_id: int, state: str,
    created_by: str = "operator", source: str = SOURCE_HUMAN,
    model: str | None = None, excluded_reason: str | None = None,
) -> dict[str, Any]:
    """Batch version of set_state for one tag across many images — the
    labeling UI's main throughput lever. `updated` is cells SUBMITTED, not
    cells changed: executemany has no RETURNING, so the human-wins rail's
    suppressions are invisible here (pre-existing semantics, unchanged)."""
    state, model, excluded_reason = _clean_provenance(state, source, model, excluded_reason)
    ids = list(dict.fromkeys(int(i) for i in image_ids))
    if not ids:
        raise ValueError("no images selected")
    if len(ids) > BULK_STATE_MAX:
        raise ValueError(f"at most {BULK_STATE_MAX} images per batch")
    with conn.cursor() as cur:
        cur.executemany(
            _UPSERT_STATE_SQL,
            [
                _upsert_params(
                    image_id=image_id, tag_id=tag_id, state=state, created_by=created_by,
                    source=source, model=model, excluded_reason=excluded_reason,
                )
                for image_id in ids
            ],
        )
    return {
        "updated": len(ids), "tag_id": tag_id, "state": state, "source": source,
        "excluded_reason": excluded_reason, "image_ids": ids,
    }


def bulk_set_state_for_image(
    conn: psycopg.Connection, *, image_id: int, tag_ids: list[int], state: str,
    created_by: str = "operator", source: str = SOURCE_HUMAN,
    model: str | None = None, excluded_reason: str | None = None,
) -> dict[str, Any]:
    """Batch version of set_state for one image across many tags — the
    mirror of bulk_set_state (which fixes the tag and varies the image).
    Backs the detail panel's "set selected" action: an image the operator
    has actually looked at (e.g. a fitness room, clearly none of the room
    tags) can be closed out in one click instead of leaving 49 tags
    implicitly-but-not-explicitly negative."""
    state, model, excluded_reason = _clean_provenance(state, source, model, excluded_reason)
    ids = list(dict.fromkeys(int(i) for i in tag_ids))
    if not ids:
        raise ValueError("no tags selected")
    if len(ids) > BULK_STATE_MAX:
        raise ValueError(f"at most {BULK_STATE_MAX} tags per batch")
    with conn.cursor() as cur:
        cur.executemany(
            _UPSERT_STATE_SQL,
            [
                _upsert_params(
                    image_id=image_id, tag_id=tag_id, state=state, created_by=created_by,
                    source=source, model=model, excluded_reason=excluded_reason,
                )
                for tag_id in ids
            ],
        )
    return {
        "updated": len(ids), "image_id": image_id, "state": state, "source": source,
        "excluded_reason": excluded_reason, "tag_ids": ids,
    }


def clear_state(conn: psycopg.Connection, *, image_id: int, tag_id: int) -> dict[str, Any]:
    """Revert a cell to untouched by deleting its explicit row. The deletion is
    itself a recorded decision — the image_tag_labels trigger (migration 446)
    appends a cleared event with state NULL."""
    # KNOWN LIMIT: that event's `actor` is the row's created_by (who CREATED the
    # cell), not who cleared it — the trigger only ever sees OLD. One operator,
    # one shared token today, so not currently a distinction.
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM image_tag_labels WHERE image_id = %s AND tag_id = %s",
            (image_id, tag_id),
        )
        deleted = cur.rowcount > 0
    return {"image_id": image_id, "tag_id": tag_id, "deleted": deleted}


# The browse is TAG-SCOPED now: a tag's own candidate queue (migration 450),
# not one global pool every tag shared. The second UNION arm is not optional —
# without it, "show me every image where kitchen = positive" would return almost
# nothing, because the 1,440 legacy positives were never drawn as candidates and
# the operator would reasonably conclude their labels had vanished. Candidates
# first (a draw always has a drawn_at), decided-but-never-drawn images after.
# (drawn_at DESC, pool_rank ASC, image_id DESC) is a TOTAL order — image_id is
# unique across the union because arm two excludes candidates — so the grid does
# not reshuffle under the operator between refetches.
_LIST_IMAGES_FOR_TAG_SQL = f"""
    SELECT q.image_id, i.storage_path, itl.state, itl.updated_at, itl.created_by,
           itl.source, itl.excluded_reason, q.draw, q.category_main, q.pool_rank
    FROM (
      SELECT c.image_id, c.drawn_at, c.pool_rank, c.draw, c.category_main
      FROM tag_candidates c
      WHERE c.tag_id = %(tag_id)s
      UNION ALL
      SELECT d.image_id, NULL::timestamptz, NULL::int, NULL::text, NULL::text
      FROM image_tag_labels d
      WHERE d.tag_id = %(tag_id)s
        AND NOT EXISTS (
          SELECT 1 FROM tag_candidates c2
          WHERE c2.tag_id = %(tag_id)s AND c2.image_id = d.image_id
        )
        {exclusion_for("d")}
    ) q
    JOIN images i ON i.id = q.image_id
    LEFT JOIN image_tag_labels itl
      ON itl.image_id = q.image_id AND itl.tag_id = %(tag_id)s
    WHERE (
      %(state)s::text IS NULL
      OR (%(state)s = 'untouched' AND itl.state IS NULL)
      OR itl.state = %(state)s
    )
    ORDER BY q.drawn_at DESC NULLS LAST, q.pool_rank ASC NULLS LAST, q.image_id DESC
    LIMIT %(limit)s
"""


_LIST_TAGS_FOR_IMAGE_SQL = """
    SELECT t.id, t.label, t.family, itl.state, itl.updated_at,
           itl.source, itl.excluded_reason
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
            "source": r[5], "excluded_reason": r[6],
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
    """Tag-centric browse: this tag's candidate queue (migration 450) plus
    everything already decided for it, each row carrying its state for this one
    tag (state=None in the response means untouched).

    Candidate membership means "LOOK at this image for this tag" — it is not a
    label, and an untouched candidate is not a negative. `draw` / `category_main`
    / `pool_rank` say how a row was drawn, and are None for an image decided but
    never drawn as a candidate. Unlike dedup_sim_labeling.list_proposals (machine
    SUGGESTIONS), this reaches images no model ever proposed this tag for."""
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
            "source": r[5], "excluded_reason": r[6],
            "draw": r[7], "category_main": r[8], "pool_rank": r[9],
        }
        for r in rows
    ]


# gate_count vs positive_count carries over the 2026-08-21 operator decision
# from the old taxonomy_overview (docs: dedup_sim_labeling.py's history) — a
# border case is not evidence a tag is learnable, so it doesn't count toward
# Gate 1, but it stays in the honest positive_count inventory. Computed here,
# not subtracted client-side, so the gate predicate has one definition.
#
# The ambiguity rate is the operator's "go fix the DEFINITION" signal, and it
# measures HUMAN indecision — so its numerator and denominator are both scoped to
# HUMAN_SOURCES. Two populations are deliberately outside it. PRUNED rows: leaving
# them in would let pruning dilute the rate — prune a hundred images and a broken
# tag reads healthy. And every row nobody has verified: backfill_442 (72,000
# manufactured negatives would drive every tag to ~0 and the signal would never
# fire) and, by exactly the same argument, `machine` (10,000 unreviewed machine
# negatives would bury 10 ambiguous human calls out of 20 just as thoroughly —
# the machine loop this PR builds the substrate for is what would do it).
#
# An excluded cell whose reason is NULL counts as AMBIGUOUS, not as a third silent
# bucket: pruning is always a deliberate act and a deliberate act names itself, so
# an unexplained exclusion is "nobody could decide". That is also what the grid
# renders for a legacy pre-445 row, and the two must not disagree.
#
# Threshold and floor are bound parameters so AMBIGUITY_RATE_THRESHOLD /
# AMBIGUITY_MIN_DECISIONS have exactly one definition, like gate_count above.
_OVERVIEW_SQL = f"""
    SELECT
      t.id, t.label, t.family, t.active, t.priority, t.ready_for_training, t.created_at,
      COALESCE(c.positive_count, 0) AS positive_count,
      COALESCE(c.gate_count, 0) AS gate_count,
      COALESCE(c.border_case_count, 0) AS border_case_count,
      COALESCE(c.negative_count, 0) AS negative_count,
      COALESCE(c.excluded_count, 0) AS excluded_count,
      COALESCE(c.human_count, 0) AS human_count,
      COALESCE(c.machine_count, 0) AS machine_count,
      COALESCE(c.backfill_count, 0) AS backfill_count,
      COALESCE(c.ambiguous_count, 0) AS ambiguous_count,
      COALESCE(c.ambiguous_decided_count, 0) AS ambiguous_decided_count,
      COALESCE(c.pruned_count, 0) AS pruned_count,
      COALESCE(c.decided_count, 0) AS decided_count,
      -- NULL, never 0, when nothing has been decided: a tag with no decisions is
      -- unknown, not healthy.
      (c.ambiguous_decided_count::numeric / NULLIF(c.decided_count, 0)) AS ambiguity_rate,
      COALESCE(
        COALESCE(c.decided_count, 0) >= %(min_decisions)s
        AND (c.ambiguous_decided_count::numeric / NULLIF(c.decided_count, 0))
            > %(threshold)s,
        false
      ) AS ambiguity_alert,
      COALESCE(p.pending_count, 0) AS pending_count,
      COALESCE(p.dismissed_count, 0) AS dismissed_count,
      COALESCE(cand.candidate_count, 0) AS candidate_count,
      COALESCE(cand.candidate_open_count, 0) AS candidate_open_count,
      cand.last_drawn_at
    FROM tag_taxonomy t
    LEFT JOIN (
      SELECT itl.tag_id,
        count(*) FILTER (WHERE itl.state = 'positive') AS positive_count,
        count(*) FILTER (WHERE itl.state = 'positive' AND bc.image_id IS NULL) AS gate_count,
        count(*) FILTER (WHERE itl.state = 'positive' AND bc.image_id IS NOT NULL) AS border_case_count,
        count(*) FILTER (WHERE itl.state = 'negative') AS negative_count,
        count(*) FILTER (WHERE itl.state = 'excluded') AS excluded_count,
        count(*) FILTER (WHERE itl.source IN ('human', 'human_confirmed')) AS human_count,
        count(*) FILTER (WHERE itl.source = 'machine') AS machine_count,
        count(*) FILTER (WHERE itl.source = 'backfill_442') AS backfill_count,
        count(*) FILTER (
          WHERE itl.state = 'excluded'
            AND itl.excluded_reason IS DISTINCT FROM 'pruned'
        ) AS ambiguous_count,
        count(*) FILTER (
          WHERE itl.state = 'excluded' AND itl.excluded_reason = 'pruned'
        ) AS pruned_count,
        count(*) FILTER (
          WHERE itl.source IN ('human', 'human_confirmed')
            AND itl.state = 'excluded'
            AND itl.excluded_reason IS DISTINCT FROM 'pruned'
        ) AS ambiguous_decided_count,
        count(*) FILTER (
          WHERE itl.source IN ('human', 'human_confirmed')
            AND (
              itl.state IN ('positive', 'negative')
              OR (
                itl.state = 'excluded'
                AND itl.excluded_reason IS DISTINCT FROM 'pruned'
              )
            )
        ) AS decided_count
      FROM image_tag_labels itl
      LEFT JOIN image_border_cases bc ON bc.image_id = itl.image_id
      WHERE true
        {exclusion_for("itl")}
      GROUP BY itl.tag_id
    ) c ON c.tag_id = t.id
    LEFT JOIN (
      SELECT lp.label,
        count(*) FILTER (WHERE lp.status = 'pending') AS pending_count,
        count(*) FILTER (WHERE lp.status = 'dismissed') AS dismissed_count
      FROM dedup_sim.label_proposals lp
      GROUP BY lp.label
    ) p ON p.label = t.label
    LEFT JOIN (
      SELECT tc.tag_id,
        count(*) AS candidate_count,
        count(*) FILTER (WHERE lab.image_id IS NULL) AS candidate_open_count,
        max(tc.drawn_at) AS last_drawn_at
      FROM tag_candidates tc
      LEFT JOIN image_tag_labels lab
        ON lab.image_id = tc.image_id AND lab.tag_id = tc.tag_id
      GROUP BY tc.tag_id
    ) cand ON cand.tag_id = t.id
    ORDER BY t.label
"""

# Distinct IMAGES queued for at least one tag — deliberately NOT the old
# `sample_size` under a new name. That number meant "images in the one pool every
# tag shared"; candidates are per tag, so nothing in the new world means it, and a
# reused name with a changed denominator is exactly the drift this codebase refuses.
_CANDIDATE_IMAGE_COUNT_SQL = "SELECT count(DISTINCT image_id)::int FROM tag_candidates"


def tag_overview(conn: psycopg.Connection) -> dict[str, Any]:
    """Every tag with its tri-state counts (positive/negative/excluded, plus
    the Gate-1 countable slice of positive and the border-cased remainder),
    its provenance inventory (human / machine / still-backfill_442), its
    ambiguity rate and pending/dismissed proposal counts — the single GET the
    labeling page's coverage strip renders from.

    `ambiguous_count` is the whole inventory of ambiguous exclusions;
    `ambiguous_decided_count` is the rate's actual numerator (human decisions
    only), so a surface can render the fraction the rate was computed from
    rather than a near-miss of it.

    positive/negative/excluded_count still INCLUDE the migration-442 backfill
    rows; `backfill_count` is what makes that inventory legible. Narrowing them
    belongs to the separate, gated deletion PR.

    `sample_size` is GONE, not repurposed (migration 450): it meant "images in the
    one pool every tag shared", and candidates are per tag. `candidate_image_count`
    is distinct images queued for at least one tag — a different quantity with a
    different denominator — and the per-tag `candidate_count` /
    `candidate_open_count` are the numbers an operator actually works against.
    Queue membership is not a label: an untouched candidate is untouched."""
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_IMAGE_COUNT_SQL)
        candidate_image_count = cur.fetchone()[0]
        cur.execute(_OVERVIEW_SQL, {
            "threshold": AMBIGUITY_RATE_THRESHOLD,
            "min_decisions": AMBIGUITY_MIN_DECISIONS,
        })
        rows = cur.fetchall()
    tags = [
        {
            "id": r[0], "label": r[1], "family": r[2], "active": r[3],
            "priority": r[4], "ready_for_training": r[5], "created_at": r[6],
            "positive_count": r[7], "gate_count": r[8], "border_case_count": r[9],
            "negative_count": r[10], "excluded_count": r[11],
            "human_count": r[12], "machine_count": r[13], "backfill_count": r[14],
            "ambiguous_count": r[15], "ambiguous_decided_count": r[16],
            "pruned_count": r[17], "decided_count": r[18],
            # Decimal out of Postgres — this goes through FastAPI's JSON encoder,
            # not into a jsonb parameter, so it is converted explicitly here.
            "ambiguity_rate": None if r[19] is None else float(r[19]),
            "ambiguity_alert": bool(r[20]),
            "pending_count": r[21], "dismissed_count": r[22],
            "candidate_count": r[23], "candidate_open_count": r[24],
            "last_drawn_at": r[25],
        }
        for r in rows
    ]
    return {
        "candidate_image_count": candidate_image_count,
        "ambiguity_threshold": AMBIGUITY_RATE_THRESHOLD,
        "ambiguity_min_decisions": AMBIGUITY_MIN_DECISIONS,
        "tags": tags,
    }
