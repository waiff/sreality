"""The sealed exam (migration 458) — membership, and the one door training reads through.

The exam grades the per-tag probes, so no training read may see it. That obligation
is discharged in exactly two ways and no third:

  * `HOLDOUT_EXCLUSION` — the anti-join, formatted into any statement that shapes or
    reports the training population. ONE constant, so the fifth copy cannot drift
    from the first.
  * `training_label_rows` — the sanctioned door for reading training labels. A
    future trainer that fetches labels and embeddings in two separate statements and
    joins them in numpy would carry no join for a SQL guard to inspect; giving it a
    door to come through is what makes the rule enforceable instead of aspirational.

`tests/test_holdout_exclusion_census.py` fails on any statement reading
image_tag_labels that neither excludes nor appears in its exempt census with a
reason. That census is the real rail — this module is only where the text lives.

Note the constant is `HOLDOUT_EXCLUSION`, not `*_SQL` or `*_QUERY`:
`tests/sql_corpus.py` hands anything with those suffixes to the PREPARE sweep, which
would choke on a bare `AND NOT EXISTS (…)` fragment.
"""

from __future__ import annotations

from typing import Any

import psycopg

# Membership alone excludes — sealed or not. Protecting only sealed cohorts would
# leave the whole drawing window open, and that window is exactly when the images
# are chosen but not yet answered: a training run inside it would consume the exam
# before the exam existed. `sealed_at` means "finished", never "protected".
#
# `{alias}` is the statement's own alias for the row carrying image_id.
HOLDOUT_EXCLUSION = """
      AND NOT EXISTS (
            SELECT 1 FROM tag_exam_members hx
            WHERE hx.image_id = {alias}.image_id
          )"""


def exclusion_for(alias: str) -> str:
    """The anti-join bound to one statement's alias. Callers format this in rather
    than writing their own NOT EXISTS, so the census can find every copy and the
    predicate has one definition."""
    return HOLDOUT_EXCLUSION.format(alias=alias)


_TRAINING_LABEL_ROWS_SQL = f"""
    SELECT itl.image_id, itl.state
    FROM image_tag_labels itl
    WHERE itl.tag_id = %(tag_id)s::bigint
      AND itl.source IN ('human', 'human_confirmed')
      AND itl.state = ANY(%(states)s::text[])
      {exclusion_for("itl")}
    ORDER BY itl.image_id
"""

_COHORT_SQL = """
    SELECT id, name, frame_size, model, revision, drawn_at, sealed_at, sealed_by, note
    FROM tag_exam_cohorts WHERE name = %(name)s
"""

_COHORT_SIZE_SQL = """
    SELECT count(*)::int FROM tag_exam_members WHERE cohort_id = %(cohort_id)s
"""

_HOLDOUT_SIZE_SQL = "SELECT count(*)::int FROM tag_exam_members"

TRAINING_STATES = ("positive", "negative")


def training_label_rows(
    conn: psycopg.Connection, *, tag_id: int,
    states: tuple[str, ...] = TRAINING_STATES,
) -> list[tuple[int, str]]:
    """Every (image_id, state) a probe for this tag may train on.

    THE one sanctioned read of training labels. A trainer that reaches
    image_tag_labels directly is a censused exception or a bug — see this module's
    docstring for why the door exists rather than a rule saying "remember the
    anti-join".

    `excluded` is deliberately not a training state: an image nobody could decide is
    not a negative, and feeding it as one teaches the probe the operator's confusion.
    """
    bad = [s for s in states if s not in TRAINING_STATES]
    if bad:
        raise ValueError(f"not trainable states: {', '.join(bad)}")
    with conn.cursor() as cur:
        cur.execute(_TRAINING_LABEL_ROWS_SQL, {"tag_id": tag_id, "states": list(states)})
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


def get_cohort(conn: psycopg.Connection, *, name: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_COHORT_SQL, {"name": name})
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("id", "name", "frame_size", "model", "revision", "drawn_at",
            "sealed_at", "sealed_by", "note")
    return dict(zip(keys, row))


def cohort_size(conn: psycopg.Connection, *, cohort_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(_COHORT_SIZE_SQL, {"cohort_id": cohort_id})
        row = cur.fetchone()
    return int(row[0]) if row else 0


def holdout_size(conn: psycopg.Connection) -> int:
    """Every image under exam protection, across all cohorts — what the training
    reads are excluding."""
    with conn.cursor() as cur:
        cur.execute(_HOLDOUT_SIZE_SQL)
        row = cur.fetchone()
    return int(row[0]) if row else 0
