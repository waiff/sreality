"""Tag definitions (migration 445) — the versioned written meaning of every
`tag_taxonomy` row, and the CLIP-space overlap evidence the operator writes them
against.

Supersede, never overwrite: there are no drafts, and `save_definition` inserts
version = max(version) + 1 as 'active' while flipping the previous active row to
'superseded' inside one transaction. Every save states the version it was written
against (`base_version`) and is rejected when that is no longer the active one, so
a second tab left open for ten minutes cannot silently revert the definition.
Other tags are referenced by tag_id inside the versioned JSONB document (a rename
can't rot a definition) and resolved to labels on read, skipping ids that no
longer exist.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from scraper.clip_tagger import load_taxonomy
from toolkit.tag_holdout import exclusion_for

MEANS_MAX_CHARS = 500
LINE_MAX_CHARS = 300
COUNTS_MAX = 30
DOES_NOT_COUNT_MAX = 30
CONFUSABLE_MAX = 30
EXAMPLE_IMAGES_MAX = 24
VERSION_LIST_MAX = 100
POSITIVE_IMAGE_LIST_MAX = 300
NEIGHBOUR_LIMIT_MAX = 25

# A centroid built from fewer positives than this is one image's idiosyncrasies,
# not a tag's visual identity — the tag is simply reported as having no
# neighbours rather than producing a confident-looking wrong answer. 51 tags over
# ~1,440 positives averages ~28 each, so this excludes only the near-empty ones.
MIN_POSITIVES_FOR_CENTROID = 5

STATUSES = ("active", "superseded")

_DEFINITION_COLUMNS = (
    "id, tag_id, version, means, counts, does_not_count, confusable_with, "
    "leave_out_when, example_image_ids, status, created_at, created_by"
)


def embedding_model() -> str:
    """The checkpoint image_clip_embeddings is written with — read from
    data/clip_taxonomy.json, never a second hardcoded copy of the name."""
    return load_taxonomy()["model"]


def embedding_revision() -> str | None:
    """The pinned HF commit behind embedding_model(); None only for a taxonomy
    file predating the pin. Same single-source rule as the name above."""
    return load_taxonomy().get("revision")


# --- row mappers ------------------------------------------------------------

def _definition_dict(r: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": r[0], "tag_id": r[1], "version": r[2], "means": r[3],
        "counts": r[4], "does_not_count": r[5], "confusable_with": r[6],
        "leave_out_when": r[7], "example_image_ids": list(r[8] or []),
        "status": r[9], "created_at": r[10], "created_by": r[11],
    }


# --- input normalisation ----------------------------------------------------

def _clean_line(value: Any, *, field: str, max_chars: int = LINE_MAX_CHARS) -> str:
    clean = " ".join(str(value or "").split())
    if not clean:
        raise ValueError(f"{field} must not be empty")
    if len(clean) > max_chars:
        raise ValueError(f"{field} is at most {max_chars} characters")
    return clean


def _clean_counts(values: list[Any] | None) -> list[str]:
    lines = [_clean_line(v, field="counts entry") for v in (values or [])]
    lines = list(dict.fromkeys(lines))
    if len(lines) > COUNTS_MAX:
        raise ValueError(f"at most {COUNTS_MAX} 'counts' entries")
    return lines


def _clean_goes_to_tag_id(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("goes_to_tag_id must be an integer or null") from exc


def _clean_does_not_count(values: list[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in values or []:
        if not isinstance(item, dict):
            raise ValueError("each does_not_count entry must be an object")
        for key in item:
            if key not in ("case", "goes_to_tag_id"):
                raise ValueError(f"unknown does_not_count field {key!r}")
        out.append({
            "case": _clean_line(item.get("case"), field="does_not_count case"),
            "goes_to_tag_id": _clean_goes_to_tag_id(item.get("goes_to_tag_id")),
        })
    if len(out) > DOES_NOT_COUNT_MAX:
        raise ValueError(f"at most {DOES_NOT_COUNT_MAX} 'does_not_count' entries")
    return out


def _clean_confusable_with(
    values: list[Any] | None, *, tag_id: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in values or []:
        if not isinstance(item, dict):
            raise ValueError("each confusable_with entry must be an object")
        for key in item:
            if key not in ("tag_id", "tell"):
                raise ValueError(f"unknown confusable_with field {key!r}")
        if "tag_id" not in item or item["tag_id"] is None:
            raise ValueError("confusable_with tag_id is required")
        try:
            other = int(item["tag_id"])
        except (TypeError, ValueError) as exc:
            raise ValueError("confusable_with tag_id must be an integer") from exc
        if other == tag_id:
            raise ValueError("a tag cannot be confusable with itself")
        if other in seen:  # first occurrence wins
            continue
        seen.add(other)
        out.append({
            "tag_id": other,
            "tell": _clean_line(item.get("tell"), field="confusable_with tell"),
        })
    if len(out) > CONFUSABLE_MAX:
        raise ValueError(f"at most {CONFUSABLE_MAX} 'confusable_with' entries")
    return out


def _clean_example_image_ids(values: list[Any] | None) -> list[int]:
    """No existence check: images can be deleted, no FK is possible on an array,
    and a missing id is skipped at render time."""
    try:
        ids = list(dict.fromkeys(int(v) for v in (values or [])))
    except (TypeError, ValueError) as exc:
        raise ValueError("example_image_ids must be integers") from exc
    if len(ids) > EXAMPLE_IMAGES_MAX:
        raise ValueError(f"at most {EXAMPLE_IMAGES_MAX} example images")
    return ids


# --- reference resolution ---------------------------------------------------

_REFERENCED_TAGS_SQL = """
    SELECT id, label FROM tag_taxonomy WHERE id = ANY(%(ids)s) ORDER BY label
"""


def _referenced_ids(
    does_not_count: list[dict[str, Any]], confusable_with: list[dict[str, Any]],
) -> list[int]:
    ids = {int(c["tag_id"]) for c in confusable_with}
    ids |= {
        int(d["goes_to_tag_id"]) for d in does_not_count
        if d.get("goes_to_tag_id") is not None
    }
    return sorted(ids)


def _referenced_tags(
    conn: psycopg.Connection, *, does_not_count: list[dict[str, Any]],
    confusable_with: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Every OTHER tag this definition points at, resolved to a label — ids that
    no longer exist are simply absent, which is what "resolve at render time"
    means for a denormalized snapshot."""
    ids = _referenced_ids(does_not_count, confusable_with)
    if not ids:
        return []
    with conn.cursor() as cur:
        cur.execute(_REFERENCED_TAGS_SQL, {"ids": ids})
        rows = cur.fetchall()
    return [{"tag_id": r[0], "label": r[1]} for r in rows]


def referenced_tags_for(
    conn: psycopg.Connection, *, does_not_count: list[dict[str, Any]],
    confusable_with: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Public face of `_referenced_tags`, for callers holding a definition that is
    not (yet) a stored row — the editor's live draft. Same resolution, so a draft
    preview and a saved card name their tags identically."""
    return _referenced_tags(
        conn, does_not_count=does_not_count, confusable_with=confusable_with,
    )


def _with_references(conn: psycopg.Connection, doc: dict[str, Any]) -> dict[str, Any]:
    return doc | {
        "referenced_tags": _referenced_tags(
            conn, does_not_count=doc["does_not_count"] or [],
            confusable_with=doc["confusable_with"] or [],
        )
    }


# --- reads ------------------------------------------------------------------

_ACTIVE_DEFINITION_SQL = """
    SELECT d.id, d.tag_id, d.version, d.means, d.counts, d.does_not_count,
           d.confusable_with, d.leave_out_when, d.example_image_ids,
           d.status, d.created_at, d.created_by
    FROM tag_taxonomy t
    LEFT JOIN tag_definitions d ON d.tag_id = t.id AND d.status = 'active'
    WHERE t.id = %(tag_id)s
"""


def get_active_definition(
    conn: psycopg.Connection, *, tag_id: int,
) -> dict[str, Any] | None:
    """The tag's current definition, or None when it has none yet. LEFT JOINed
    off tag_taxonomy so an unknown TAG (KeyError, a 404) is distinguishable from
    a known tag with no definition yet (None, a 200 with a null body)."""
    with conn.cursor() as cur:
        cur.execute(_ACTIVE_DEFINITION_SQL, {"tag_id": tag_id})
        row = cur.fetchone()
    if row is None:
        raise KeyError(tag_id)
    if row[0] is None:
        return None
    return _with_references(conn, _definition_dict(row))


_VERSION_LIST_SQL = """
    SELECT d.id, d.version, d.status, d.means, d.created_at, d.created_by
    FROM tag_taxonomy t
    LEFT JOIN tag_definitions d ON d.tag_id = t.id
    WHERE t.id = %(tag_id)s
    ORDER BY d.version DESC NULLS LAST
    LIMIT %(limit)s
"""


def list_definition_versions(
    conn: psycopg.Connection, *, tag_id: int, limit: int = VERSION_LIST_MAX,
) -> list[dict[str, Any]]:
    """Newest-first version metadata for one tag (no document bodies) — the
    history dropdown. Same LEFT JOIN trick as get_active_definition: an unknown
    tag raises, a tag with no versions returns []."""
    limit = min(max(1, limit), VERSION_LIST_MAX)
    with conn.cursor() as cur:
        cur.execute(_VERSION_LIST_SQL, {"tag_id": tag_id, "limit": limit})
        rows = cur.fetchall()
    if not rows:
        raise KeyError(tag_id)
    if rows[0][0] is None:
        return []
    return [
        {
            "id": r[0], "version": r[1], "status": r[2], "means": r[3],
            "created_at": r[4], "created_by": r[5],
        }
        for r in rows
    ]


_VERSION_SQL = f"""
    SELECT {_DEFINITION_COLUMNS}
    FROM tag_definitions
    WHERE tag_id = %(tag_id)s AND version = %(version)s
"""


def get_definition_version(
    conn: psycopg.Connection, *, tag_id: int, version: int,
) -> dict[str, Any]:
    """One historical version, read-only, with its tag references resolved —
    what the history view renders."""
    with conn.cursor() as cur:
        cur.execute(_VERSION_SQL, {"tag_id": tag_id, "version": version})
        row = cur.fetchone()
    if row is None:
        raise KeyError((tag_id, version))
    return _with_references(conn, _definition_dict(row))


_STATUS_SQL = """
    SELECT tag_id, id, version, means, created_at
    FROM tag_definitions
    WHERE status = 'active'
    ORDER BY tag_id
"""


def list_definition_status(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """One row per tag that HAS an active definition — the tag list's "— / v3"
    column, and the read the future "no definition = cannot enter the pipeline"
    gate keys on (a tag absent from this list has no definition).

    Deliberately carries no label/family/priority: those have exactly one
    source, tag_annotations.tag_overview."""
    with conn.cursor() as cur:
        cur.execute(_STATUS_SQL)
        rows = cur.fetchall()
    return [
        {
            "tag_id": r[0], "definition_id": r[1], "version": r[2],
            "means": r[3], "created_at": r[4],
        }
        for r in rows
    ]


# --- write ------------------------------------------------------------------

STALE_SAVE_MESSAGE = (
    "this tag's definition changed in another tab — reload and save again"
)

# Names the version it expects to retire. Without that predicate a save written
# against v2 while v3 is already active would supersede v3 and land its own stale
# text as v4 — no error, no unique violation, the active definition silently
# reverted. The partial unique index only catches OVERLAPPING transactions; two
# browser tabs minutes apart are not overlapping.
_SUPERSEDE_SQL = """
    UPDATE tag_definitions SET status = 'superseded'
    WHERE tag_id = %(tag_id)s AND status = 'active'
      AND version = %(base_version)s
"""

_ACTIVE_VERSION_SQL = """
    SELECT version FROM tag_definitions
    WHERE tag_id = %(tag_id)s AND status = 'active'
"""

_INSERT_DEFINITION_SQL = f"""
    INSERT INTO tag_definitions (
      tag_id, version, means, counts, does_not_count, confusable_with,
      leave_out_when, example_image_ids, status, created_by
    )
    SELECT %(tag_id)s, coalesce(max(version), 0) + 1,
           %(means)s, %(counts)s, %(does_not_count)s, %(confusable_with)s,
           %(leave_out_when)s, %(example_image_ids)s, 'active', %(created_by)s
    FROM tag_definitions WHERE tag_id = %(tag_id)s
    RETURNING {_DEFINITION_COLUMNS}
"""

_TAG_EXISTS_SQL = "SELECT 1 FROM tag_taxonomy WHERE id = %(tag_id)s"

_TAG_LABEL_SQL = "SELECT label FROM tag_taxonomy WHERE id = %(tag_id)s"


def tag_label(conn: psycopg.Connection, *, tag_id: int) -> str:
    """This tag's label. KeyError for an unknown tag, so callers 404 rather than
    render a card headed by a blank."""
    with conn.cursor() as cur:
        cur.execute(_TAG_LABEL_SQL, {"tag_id": tag_id})
        row = cur.fetchone()
    if row is None:
        raise KeyError(tag_id)
    return str(row[0])
_TAGS_EXIST_SQL = "SELECT id FROM tag_taxonomy WHERE id = ANY(%(ids)s)"


def save_definition(
    conn: psycopg.Connection, *, tag_id: int, means: str,
    counts: list[Any] | None = None,
    does_not_count: list[Any] | None = None,
    confusable_with: list[Any] | None = None,
    leave_out_when: str | None = None,
    example_image_ids: list[Any] | None = None,
    base_version: int | None = None,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Save a new version. There are no drafts: this supersedes the version the
    caller wrote against and inserts version = max(version)+1 as the new active
    row, both in one transaction, so history stays complete and exactly one
    version is active (the partial unique index backstops that, not this code).

    `base_version` is the version the caller loaded — None meaning "I loaded a tag
    with no definition". Either way it is an assertion, and a save whose
    assertion no longer holds raises rather than overwriting someone else's
    newer wording."""
    clean_means = _clean_line(means, field="means", max_chars=MEANS_MAX_CHARS)
    clean_counts = _clean_counts(counts)
    clean_dnc = _clean_does_not_count(does_not_count)
    clean_conf = _clean_confusable_with(confusable_with, tag_id=tag_id)
    clean_examples = _clean_example_image_ids(example_image_ids)
    clean_leave_out = (
        _clean_line(leave_out_when, field="leave_out_when")
        if (leave_out_when or "").strip() else None
    )

    with conn.cursor() as cur:
        cur.execute(_TAG_EXISTS_SQL, {"tag_id": tag_id})
        if cur.fetchone() is None:
            raise KeyError(tag_id)
        # Strict on WRITE, lenient on read: the picker only ever offers real
        # tags, so an unknown id at save time is a bug — while a LATER deletion
        # is normal and is handled by skipping at render.
        wanted = _referenced_ids(clean_dnc, clean_conf)
        if wanted:
            cur.execute(_TAGS_EXIST_SQL, {"ids": wanted})
            found = {r[0] for r in cur.fetchall()}
            missing = sorted(set(wanted) - found)
            if missing:
                raise ValueError(
                    f"unknown tag_id {missing} referenced by this definition"
                )

    params = {
        "tag_id": tag_id, "means": clean_means, "counts": Jsonb(clean_counts),
        "does_not_count": Jsonb(clean_dnc), "confusable_with": Jsonb(clean_conf),
        "leave_out_when": clean_leave_out, "example_image_ids": clean_examples,
        "created_by": created_by,
    }
    try:
        with conn.transaction(), conn.cursor() as cur:
            if base_version is None:
                cur.execute(_ACTIVE_VERSION_SQL, {"tag_id": tag_id})
                if cur.fetchone() is not None:
                    raise ValueError(STALE_SAVE_MESSAGE)
            else:
                cur.execute(
                    _SUPERSEDE_SQL,
                    {"tag_id": tag_id, "base_version": int(base_version)},
                )
                if cur.rowcount != 1:
                    raise ValueError(STALE_SAVE_MESSAGE)
            cur.execute(_INSERT_DEFINITION_SQL, params)
            row = cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        # The other half of the race, the one base_version can't see: two saves
        # in OVERLAPPING transactions, where the loser's read predates the
        # winner's insert. Its own insert then trips
        # tag_definitions_one_active_idx (or the (tag_id, version) unique).
        raise ValueError(STALE_SAVE_MESSAGE) from exc
    return _with_references(conn, _definition_dict(row))


# --- what the tag actually contains -----------------------------------------

# (updated_at DESC, image_id DESC) is a total order — a bare timestamp sort
# reshuffles under the operator between refetches.
_POSITIVE_IMAGES_SQL = f"""
    SELECT itl.image_id, i.storage_path, i.sreality_url, itl.updated_at
    FROM image_tag_labels itl
    JOIN images i ON i.id = itl.image_id
    WHERE itl.tag_id = %(tag_id)s AND itl.state = 'positive'
      {exclusion_for("itl")}
    ORDER BY itl.updated_at DESC, itl.image_id DESC
    LIMIT %(limit)s
"""


def list_positive_images(
    conn: psycopg.Connection, *, tag_id: int, limit: int = 200,
) -> list[dict[str, Any]]:
    """Every image currently positive on this tag — what the tag ACTUALLY
    contains, which is how the operator sees the drift between a label's name
    and its contents while writing the definition.

    Deliberately NOT tag_annotations.list_images_for_tag: that one is driven by
    the tag's REVIEW QUEUE (tag_candidates, migration 450) — a work list, not
    "every positive" — and its rows carry no label semantics at all. This page is
    about what the tag contains, so it reads image_tag_labels directly."""
    limit = min(max(1, limit), POSITIVE_IMAGE_LIST_MAX)
    with conn.cursor() as cur:
        cur.execute(_POSITIVE_IMAGES_SQL, {"tag_id": tag_id, "limit": limit})
        rows = cur.fetchall()
    return [
        {
            "image_id": r[0], "storage_path": r[1], "sreality_url": r[2],
            "updated_at": r[3],
        }
        for r in rows
    ]


POSITIVE_IMAGE_ORDERS = ("recent", "outlier_first")

# Outlier-first: this tag's own positives ordered by cosine distance from this
# tag's own centroid, farthest first, so a mis-filed image is on screen instead
# of somewhere in a wall of 300 photos.
#
# The centroid is built from HUMAN-VERIFIED positives only — the same predicate
# tag_candidates._DRAW_POOL_SQL uses. Migration 446 (:84) stamped backfill_442
# on NEGATIVES only, so `state = 'positive'` already excludes every manufactured
# row; the source clause is the rail that keeps an unreviewed MACHINE positive
# from defining the centre it would then be measured against.
#
# The floor is applied in the CASE, not in a HAVING, so `positives` is readable
# even when the tag is UNDER it — a page that has to say "3 of the 5 needed"
# cannot get that from an empty CTE. An aggregate with no GROUP BY always
# returns exactly one row, so the CROSS JOIN is safe and total.
#
# Below the floor every distance is NULL and the ORDER BY degrades to
# `updated_at DESC, image_id DESC` — byte-identical to _POSITIVE_IMAGES_SQL's
# order. Same degrade-by-construction trick as nearest_tags' empty `subject`.
#
# `<=>` is cosine DISTANCE (0 = identical), never similarity, and there is NO
# threshold on it anywhere: measured inter-tag centroid distances span ~0.01 to
# ~0.42, so only RANK within one tag transfers. The LEFT JOIN cannot fan out —
# (image_id, model) is image_clip_embeddings' primary key (migration 226).
# Every placeholder carries an explicit cast so tests/test_sql_schema_prepare.py
# can PREPARE it without binding values.
_POSITIVE_IMAGES_OUTLIER_SQL = f"""
    WITH centroid AS (
      SELECT avg(e.embedding) AS vec, count(*)::int AS positives
      FROM image_tag_labels itl
      JOIN image_clip_embeddings e
        ON e.image_id = itl.image_id AND e.model = %(model)s::text
      WHERE itl.tag_id = %(tag_id)s::bigint
        AND itl.state = 'positive'
        AND itl.source IN ('human', 'human_confirmed')
        {exclusion_for("itl")}
    ),
    scored AS (
      SELECT itl.image_id, i.storage_path, i.sreality_url, itl.updated_at,
             c.positives,
             CASE WHEN c.positives >= %(min_positives)s::int
                  THEN (e.embedding <=> c.vec) END AS centroid_distance
      FROM image_tag_labels itl
      JOIN images i ON i.id = itl.image_id
      CROSS JOIN centroid c
      LEFT JOIN image_clip_embeddings e
        ON e.image_id = itl.image_id AND e.model = %(model)s::text
      WHERE itl.tag_id = %(tag_id)s::bigint AND itl.state = 'positive'
        {exclusion_for("itl")}
    )
    SELECT image_id, storage_path, sreality_url, updated_at, centroid_distance,
           CASE WHEN centroid_distance IS NULL THEN NULL ELSE
             row_number() OVER (
               ORDER BY centroid_distance DESC NULLS LAST,
                        updated_at DESC, image_id DESC
             )::int END AS distance_rank,
           positives
    FROM scored
    ORDER BY centroid_distance DESC NULLS LAST, updated_at DESC, image_id DESC
    LIMIT %(limit)s::int
"""


def list_positive_images_outlier_first(
    conn: psycopg.Connection, *, tag_id: int, limit: int = 200,
    min_positives: int = MIN_POSITIVES_FOR_CENTROID, model: str | None = None,
) -> dict[str, Any]:
    """This tag's positives ordered farthest-first from its own centroid — the
    mis-filed images, on screen instead of somewhere in a wall of 300 photos.

    Reports the order it ACTUALLY applied: a tag under the positives floor has
    no meaningful centroid, so it falls back to list_positive_images' order and
    says so, rather than sorting on nothing."""
    limit = min(max(1, limit), POSITIVE_IMAGE_LIST_MAX)
    min_positives = max(1, int(min_positives))
    with conn.cursor() as cur:
        cur.execute(_POSITIVE_IMAGES_OUTLIER_SQL, {
            "tag_id": tag_id, "limit": limit, "min_positives": min_positives,
            "model": model or embedding_model(),
        })
        rows = cur.fetchall()
    positives = int(rows[0][6]) if rows else 0
    return {
        "order": "outlier_first" if positives >= min_positives else "recent",
        "centroid_positives": positives,
        "min_positives": min_positives,
        "images": [
            {
                "image_id": r[0], "storage_path": r[1], "sreality_url": r[2],
                "updated_at": r[3],
                "centroid_distance": None if r[4] is None else float(r[4]),
                "distance_rank": r[5],
            }
            for r in rows
        ],
    }


# --- overlap evidence -------------------------------------------------------

# `<=>` is cosine DISTANCE (0 = identical), never similarity. The c.tag_id
# tiebreaker is mandatory, not decorative: equal distances would otherwise
# reshuffle between refetches.
_NEAREST_TAGS_SQL = f"""
    WITH centroids AS (
      SELECT itl.tag_id,
             avg(e.embedding) AS centroid,
             count(*) AS embedded_positive_count
      FROM image_tag_labels itl
      JOIN image_clip_embeddings e
        ON e.image_id = itl.image_id AND e.model = %(model)s
      WHERE itl.state = 'positive'
        {exclusion_for("itl")}
      GROUP BY itl.tag_id
      HAVING count(*) >= %(min_positives)s
    ),
    subject AS (
      SELECT centroid FROM centroids WHERE tag_id = %(tag_id)s
    )
    SELECT c.tag_id, t.label, t.family, c.embedded_positive_count,
           (c.centroid <=> s.centroid) AS cosine_distance
    FROM centroids c
    CROSS JOIN subject s
    JOIN tag_taxonomy t ON t.id = c.tag_id
    WHERE c.tag_id <> %(tag_id)s
    ORDER BY cosine_distance, c.tag_id
    LIMIT %(limit)s
"""


def nearest_tags(
    conn: psycopg.Connection, *, tag_id: int, limit: int = 8,
    min_positives: int = MIN_POSITIVES_FOR_CENTROID, model: str | None = None,
) -> list[dict[str, Any]]:
    """The tags whose labeled positives sit closest to this tag's in CLIP
    embedding space — how the operator discovers that two tags are the same tag.

    One per-tag centroid over ~1,440 vectors, so it is cheap and needs no ANN
    index; it is nonetheless bounded by LIMIT and by the min_positives floor
    below. Degrades to [] rather than raising when the subject tag has too few
    embedded positives to have a meaningful centroid: the CROSS JOIN over an
    empty `subject` yields no rows, so there is no special case to write."""
    limit = min(max(1, limit), NEIGHBOUR_LIMIT_MAX)
    min_positives = max(1, int(min_positives))
    with conn.cursor() as cur:
        cur.execute(_NEAREST_TAGS_SQL, {
            "tag_id": tag_id, "limit": limit, "min_positives": min_positives,
            "model": model or embedding_model(),
        })
        rows = cur.fetchall()
    return [
        {
            "tag_id": r[0], "label": r[1], "family": r[2],
            "embedded_positive_count": r[3], "cosine_distance": float(r[4]),
        }
        for r in rows
    ]
