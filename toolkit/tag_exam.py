"""Drawing and sealing the exam cohort (migration 458).

THE EXAM IS TWO FRAMES, and they answer different questions.

  * `pure_random` — a uniform sample of the whole embedded corpus. It is the
    incorruptible core: no machine chose it, so it measures how common each tag
    actually IS and gives an honest, if noisy, read on what the probes miss.
  * `stratified` — drawn with the vision screener's guesses in hand, deliberately
    over-sampling rare tags. A random 250 holds three or four garages; no exam of
    buildable size fixes that by growing. Enrichment does, at the price of needing
    to be weighted.

Every member records the odds it was drawn under, so statistics over the exam are
inverse-probability weighted rather than counted raw — the arithmetic a pollster
uses to over-interview a small group and still report a national figure. STRATIFY,
NEVER FILTER: every stratum keeps a non-zero probability, including "the screener
saw nothing here", or recall is measured only over what the screener already found
and the probe is graded on the easy half.

WHY PROBE-BY-RANDOM-ID AND NOT TABLESAMPLE. `toolkit/tag_candidates.py` samples
listing blocks with TABLESAMPLE, which is right there: it needs a big diverse pool
fast, and mild block clustering is attenuated by later per-listing and per-property
caps. Here the requirement is the opposite. `pure_random` exists precisely to be
unbiased, and TABLESAMPLE SYSTEM returns whole 8KB pages — for `images`, a page is
mostly ONE listing's photos, so a 100-image block sample would be perhaps 25
listings seen four times each. Probing uniformly-random ids and keeping the hits
gives every existing row the same selection probability, at the cost of a few
hundred primary-key lookups. Do not "optimise" this into TABLESAMPLE.
"""

from __future__ import annotations

from typing import Any

import psycopg

from toolkit.tag_holdout import exclusion_for

FRAMES = ("pure_random", "stratified")

# Probes per wanted image. Every probe is a PK lookup, so overshooting is cheap;
# undershooting means a short draw. The eligible fraction is high (an image only
# has an embedding if its bytes were fetched) but id gaps, cleared storage_paths
# and already-drawn images all cost hits.
PROBE_FACTOR = 60
PROBE_MAX = 40_000

_CREATE_COHORT_SQL = """
    INSERT INTO tag_exam_cohorts (name, frame_size, model, revision, note)
    VALUES (%(name)s, %(frame_size)s, %(model)s, %(revision)s, %(note)s)
    RETURNING id, name, frame_size, model, revision, drawn_at, sealed_at, sealed_by, note
"""

# The eligible frame: an image carrying a vector from the pinned encoder. An image
# only has one if its bytes were fetched, so this is also "an image we can show".
_FRAME_SIZE_SQL = """
    SELECT count(*)::bigint FROM image_clip_embeddings WHERE model = %(model)s::text
"""

_PURE_RANDOM_PROBE_SQL = """
    WITH bounds AS (
      SELECT min(id) AS lo, max(id) AS hi FROM images
    ),
    probes AS (
      SELECT DISTINCT (b.lo + floor(random() * (b.hi - b.lo + 1)))::bigint AS id
      FROM bounds b, generate_series(1, %(probes)s)
    )
    SELECT i.id
    FROM probes p
    JOIN images i ON i.id = p.id
    WHERE i.storage_path IS NOT NULL
      AND EXISTS (
            SELECT 1 FROM image_clip_embeddings e
            WHERE e.image_id = i.id AND e.model = %(model)s::text
          )
      AND NOT EXISTS (
            SELECT 1 FROM tag_exam_members m WHERE m.image_id = i.id
          )
    LIMIT %(count)s
"""

# Frozen at draw so a later overwrite is auditable. A pure-random draw over 10.4M
# images lands on one of the ~1,440 already labelled about 0.014% of the time, so
# this is almost always NULL — but "almost always" is not "never", and discovering
# a silently rewritten training label during analysis is much worse than carrying
# the column.
_PREEXISTING_LABELS_SQL = """
    SELECT itl.image_id,
           jsonb_agg(jsonb_build_object(
             'tag_id', itl.tag_id, 'state', itl.state, 'source', itl.source
           ) ORDER BY itl.tag_id) AS labels
    FROM image_tag_labels itl
    WHERE itl.image_id = ANY(%(image_ids)s::bigint[])
    GROUP BY itl.image_id
"""

_INSERT_MEMBER_SQL = """
    INSERT INTO tag_exam_members (
      cohort_id, image_id, frame, stratum, inclusion_probability,
      screen_guess_tag_ids, preexisting_labels, position
    )
    VALUES (
      %(cohort_id)s, %(image_id)s, %(frame)s, %(stratum)s, %(inclusion_probability)s,
      %(screen_guess_tag_ids)s, %(preexisting_labels)s, %(position)s
    )
    ON CONFLICT (cohort_id, image_id) DO NOTHING
"""

_MAX_POSITION_SQL = """
    SELECT COALESCE(max(position), 0)::int FROM tag_exam_members WHERE cohort_id = %(cohort_id)s
"""

_SEAL_SQL = """
    UPDATE tag_exam_cohorts
       SET sealed_at = now(), sealed_by = %(sealed_by)s
     WHERE id = %(cohort_id)s AND sealed_at IS NULL
    RETURNING id, sealed_at
"""

_COMPOSITION_SQL = """
    SELECT m.frame, m.stratum, count(*)::int AS n,
           min(m.inclusion_probability) AS p_min,
           max(m.inclusion_probability) AS p_max
    FROM tag_exam_members m
    WHERE m.cohort_id = %(cohort_id)s
    GROUP BY m.frame, m.stratum
    ORDER BY m.frame, m.stratum
"""

_COHORT_KEYS = ("id", "name", "frame_size", "model", "revision", "drawn_at",
                "sealed_at", "sealed_by", "note")


def create_cohort(
    conn: psycopg.Connection, *, name: str, model: str, revision: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Open a cohort. It accepts members until `seal_cohort`; the frame size is
    measured NOW and stored, so a corpus that grows later cannot silently rewrite
    what the recorded probabilities meant."""
    with conn.cursor() as cur:
        cur.execute(_FRAME_SIZE_SQL, {"model": model})
        row = cur.fetchone()
        frame_size = int(row[0]) if row else 0
        if frame_size <= 0:
            raise ValueError(f"no embeddings for model {model!r}; nothing to draw from")
        cur.execute(_CREATE_COHORT_SQL, {
            "name": name, "frame_size": frame_size, "model": model,
            "revision": revision, "note": note,
        })
        created = cur.fetchone()
    return dict(zip(_COHORT_KEYS, created))


def _preexisting_labels(
    conn: psycopg.Connection, *, image_ids: list[int],
) -> dict[int, Any]:
    if not image_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(_PREEXISTING_LABELS_SQL, {"image_ids": image_ids})
        return {int(r[0]): r[1] for r in cur.fetchall()}


def _next_position(conn: psycopg.Connection, *, cohort_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(_MAX_POSITION_SQL, {"cohort_id": cohort_id})
        row = cur.fetchone()
    return (int(row[0]) if row else 0) + 1


def add_members(
    conn: psycopg.Connection, *, cohort_id: int, rows: list[dict[str, Any]],
) -> int:
    """Insert members. `rows` carry image_id, frame, stratum,
    inclusion_probability and optionally screen_guess_tag_ids.

    Refuses to touch a sealed cohort: sealing is the one-way door, and a sealed
    exam that grew afterwards would invalidate every grade already taken on it."""
    cohort = _get_cohort_by_id(conn, cohort_id=cohort_id)
    if cohort is None:
        raise KeyError(cohort_id)
    if cohort["sealed_at"] is not None:
        raise ValueError(f"cohort {cohort['name']!r} is sealed; it cannot take members")
    if not rows:
        return 0
    for r in rows:
        if r["frame"] not in FRAMES:
            raise ValueError(f"unknown frame {r['frame']!r}")
        p = float(r["inclusion_probability"])
        # Zero probability is not a small number, it is a filtered stratum wearing
        # a sample's clothes: 1/p is undefined and the row can never be weighted.
        if not 0 < p <= 1:
            raise ValueError(f"inclusion_probability must be in (0, 1], got {p}")

    labels = _preexisting_labels(conn, image_ids=[int(r["image_id"]) for r in rows])
    position = _next_position(conn, cohort_id=cohort_id)
    written = 0
    with conn.transaction(), conn.cursor() as cur:
        for i, r in enumerate(rows):
            image_id = int(r["image_id"])
            cur.execute(_INSERT_MEMBER_SQL, {
                "cohort_id": cohort_id,
                "image_id": image_id,
                "frame": r["frame"],
                "stratum": r["stratum"],
                "inclusion_probability": float(r["inclusion_probability"]),
                "screen_guess_tag_ids": r.get("screen_guess_tag_ids"),
                "preexisting_labels": labels.get(image_id),
                "position": position + i,
            })
            written += cur.rowcount
    return written


def draw_pure_random(
    conn: psycopg.Connection, *, cohort_id: int, count: int,
) -> dict[str, Any]:
    """The incorruptible core: a uniform sample of the embedded corpus.

    Uniform because every existing id is equally likely to be probed — see this
    module's docstring for why this is NOT TABLESAMPLE."""
    cohort = _get_cohort_by_id(conn, cohort_id=cohort_id)
    if cohort is None:
        raise KeyError(cohort_id)
    probes = min(PROBE_MAX, max(count * PROBE_FACTOR, count))
    with conn.cursor() as cur:
        cur.execute(_PURE_RANDOM_PROBE_SQL, {
            "probes": probes, "count": count, "model": cohort["model"],
        })
        image_ids = [int(r[0]) for r in cur.fetchall()]

    # p = n/N for a uniform draw. Recorded per row rather than derived later so a
    # cohort stays interpretable even if the corpus moves under it.
    p = count / float(cohort["frame_size"])
    written = add_members(conn, cohort_id=cohort_id, rows=[
        {"image_id": i, "frame": "pure_random", "stratum": "pure_random",
         "inclusion_probability": p}
        for i in image_ids
    ])
    return {
        "requested": count, "probed": probes, "found": len(image_ids),
        "inserted": written, "inclusion_probability": p,
        "frame_size": cohort["frame_size"],
        # A short draw is reported, never quietly padded from somewhere else: it
        # means the probe factor is wrong for this corpus, which is worth knowing.
        "short_by": max(0, count - written),
    }


_GET_COHORT_BY_ID_SQL = """
    SELECT id, name, frame_size, model, revision, drawn_at, sealed_at, sealed_by, note
    FROM tag_exam_cohorts WHERE id = %(cohort_id)s
"""


def _get_cohort_by_id(
    conn: psycopg.Connection, *, cohort_id: int,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_GET_COHORT_BY_ID_SQL, {"cohort_id": cohort_id})
        row = cur.fetchone()
    return dict(zip(_COHORT_KEYS, row)) if row else None


def seal_cohort(
    conn: psycopg.Connection, *, cohort_id: int, sealed_by: str = "operator",
) -> dict[str, Any]:
    """Close the exam. Idempotent in effect but honest about it: a second seal
    reports `already_sealed` rather than restamping, because the seal time is what
    later says which grades were taken against a finished exam."""
    with conn.cursor() as cur:
        cur.execute(_SEAL_SQL, {"cohort_id": cohort_id, "sealed_by": sealed_by})
        row = cur.fetchone()
    if row is None:
        cohort = _get_cohort_by_id(conn, cohort_id=cohort_id)
        if cohort is None:
            raise KeyError(cohort_id)
        return {"cohort_id": cohort_id, "status": "already_sealed",
                "sealed_at": cohort["sealed_at"]}
    return {"cohort_id": int(row[0]), "status": "sealed", "sealed_at": row[1]}


def composition(conn: psycopg.Connection, *, cohort_id: int) -> list[dict[str, Any]]:
    """Members by frame and stratum, with the probability range each carries — the
    readout that makes a weighted statistic auditable rather than asserted."""
    with conn.cursor() as cur:
        cur.execute(_COMPOSITION_SQL, {"cohort_id": cohort_id})
        return [
            {"frame": r[0], "stratum": r[1], "n": int(r[2]),
             "p_min": float(r[3]), "p_max": float(r[4])}
            for r in cur.fetchall()
        ]


# --- sitting the exam -------------------------------------------------------

_NEXT_MEMBER_SQL = """
    SELECT m.image_id, m.position, i.storage_path
    FROM tag_exam_members m
    JOIN images i ON i.id = m.image_id
    WHERE m.cohort_id = %(cohort_id)s
      AND NOT EXISTS (
            SELECT 1 FROM image_tag_labels l
            WHERE l.image_id = m.image_id
              AND l.tag_id = ANY(%(tag_ids)s::bigint[])
              AND l.source IN ('human', 'human_confirmed')
          )
    ORDER BY m.position
    LIMIT 1
"""

# Progress is "images with a verdict on EVERY routing tag", not "images with any
# label". A half-answered image is not answered: the exam grades all eight heads,
# so a partial row would silently shrink one tag's test set.
_PROGRESS_SQL = """
    SELECT count(*)::int AS total,
           count(*) FILTER (WHERE d.decided = %(tag_count)s)::int AS answered
    FROM tag_exam_members m
    CROSS JOIN LATERAL (
      SELECT count(*)::int AS decided
      FROM image_tag_labels l
      WHERE l.image_id = m.image_id
        AND l.tag_id = ANY(%(tag_ids)s::bigint[])
        AND l.source IN ('human', 'human_confirmed')
    ) d
    WHERE m.cohort_id = %(cohort_id)s
"""

_IS_MEMBER_SQL = """
    SELECT 1 FROM tag_exam_members
    WHERE cohort_id = %(cohort_id)s AND image_id = %(image_id)s
"""

# Warm-up images come from OUTSIDE the exam on purpose: they exist to settle the
# operator's hand, and spending real exam images on that would shrink the sample
# that has to grade everything.
_WARMUP_SQL = f"""
    SELECT DISTINCT l.image_id, i.storage_path
    FROM image_tag_labels l
    JOIN images i ON i.id = l.image_id AND i.storage_path IS NOT NULL
    WHERE l.source IN ('human', 'human_confirmed')
      AND l.state = 'positive'
      {exclusion_for("l")}
    ORDER BY l.image_id
    LIMIT %(limit)s
"""


def next_question(
    conn: psycopg.Connection, *, cohort_id: int, tag_ids: list[int],
) -> dict[str, Any] | None:
    """The next exam image with no human verdict yet, in draw order."""
    with conn.cursor() as cur:
        cur.execute(_NEXT_MEMBER_SQL, {"cohort_id": cohort_id, "tag_ids": tag_ids})
        row = cur.fetchone()
    if row is None:
        return None
    return {"image_id": int(row[0]), "position": int(row[1]), "storage_path": row[2]}


def progress(
    conn: psycopg.Connection, *, cohort_id: int, tag_ids: list[int],
) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(_PROGRESS_SQL, {
            "cohort_id": cohort_id, "tag_ids": tag_ids, "tag_count": len(tag_ids),
        })
        row = cur.fetchone()
    total, answered = (int(row[0]), int(row[1])) if row else (0, 0)
    return {"total": total, "answered": answered, "remaining": total - answered}


def warmup_images(
    conn: psycopg.Connection, *, limit: int = 10,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_WARMUP_SQL, {"limit": limit})
        return [{"image_id": int(r[0]), "storage_path": r[1]} for r in cur.fetchall()]


def is_member(conn: psycopg.Connection, *, cohort_id: int, image_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(_IS_MEMBER_SQL, {"cohort_id": cohort_id, "image_id": image_id})
        return cur.fetchone() is not None


def record_answer(
    conn: psycopg.Connection, *, cohort_id: int, image_id: int,
    tag_ids: list[int], picked: list[int], skipped: list[int] | None = None,
    cant_tell: bool = False, answered_by: str = "operator",
) -> dict[str, Any]:
    """Write one exam answer across ALL routing tags.

    Four verdicts, restating the BRIEF's three-state rule rather than inventing a
    new one — the first exam UI collapsed it for speed, which turned out to bend
    the labeling calculus to fit a screen:

      * picked    -> positive: the photo is OF this thing (a co-subject counts).
      * skipped   -> excluded/'pruned': the subject is clearly and substantially
                     present but the photo is of something else. The brief: "never
                     mark a tag negative ... leave it out of that head instead."
                     An excluded cell trains nothing and grades nothing — the
                     standard treatment of contested ground truth (VOC's
                     'difficult' flag is the same idea).
      * unpicked  -> negative: does not apply. An incidental hint in the
                     background is a valuable negative.
      * cant_tell -> excluded/'ambiguous' on EVERY tag: the whole image is
                     genuinely undecidable.

    The store's two leave-out reasons map exactly onto the brief's two kinds:
    'ambiguous' = undecidable, 'pruned' = deliberately left out of this head. No
    new vocabulary.

    Refuses an image outside the cohort. That refusal is the warm-up's safety rail:
    warm-up images come from outside the exam, so a mis-wired client cannot write
    practice answers into the measurement.
    """
    from toolkit import tag_annotations as ta

    if not is_member(conn, cohort_id=cohort_id, image_id=image_id):
        raise KeyError(f"image {image_id} is not in cohort {cohort_id}")
    skipped = skipped or []
    unknown = sorted((set(picked) | set(skipped)) - set(tag_ids))
    if unknown:
        raise ValueError(f"not routing tags: {unknown}")
    both = sorted(set(picked) & set(skipped))
    if both:
        # One cell cannot be a positive and a leave-out at once; refusing beats
        # letting write order decide.
        raise ValueError(f"picked and skipped overlap: {both}")

    written = 0
    for tag_id in tag_ids:
        if cant_tell:
            state, reason = "excluded", ta.EXCLUDED_AMBIGUOUS
        elif tag_id in skipped:
            state, reason = "excluded", ta.EXCLUDED_PRUNED
        else:
            state, reason = ("positive" if tag_id in picked else "negative"), None
        ta.set_state(
            conn, image_id=image_id, tag_id=tag_id, state=state,
            created_by=answered_by, source=ta.SOURCE_HUMAN, excluded_reason=reason,
        )
        written += 1
    return {"image_id": image_id, "cells_written": written,
            "picked": sorted(picked), "skipped": sorted(skipped),
            "cant_tell": cant_tell}
