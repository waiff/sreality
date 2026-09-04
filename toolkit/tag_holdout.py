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

# HOLDOUT membership excludes — sealed or not. Protecting only sealed cohorts
# would leave the whole drawing window open, and that window is exactly when the
# images are chosen but not yet answered: a training run inside it would consume
# the exam before the exam existed. `sealed_at` means "finished", never
# "protected".
#
# Since migration 464 a cohort carries a PURPOSE, and only 'holdout' cohorts
# exclude: 'curated' cohorts hold operator-marked images re-labeled carefully
# through the exam UI, and their answers ARE training material — excluding them
# would defeat the reason they were seated. The one-exam-per-image index keeps
# the split sound in the direction that matters: an image in a curated cohort
# can never later be drawn into a holdout, so nothing trained-on ever grades.
#
# `{alias}` is the statement's own alias for the row carrying image_id.
HOLDOUT_EXCLUSION = """
      AND NOT EXISTS (
            SELECT 1 FROM tag_exam_members hx
            JOIN tag_exam_cohorts hc
              ON hc.id = hx.cohort_id AND hc.purpose = 'holdout'
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

# The operator's 2026-09-01 ruling: exam (holdout) labels MAY be used for
# training — "there is no reason we should not". This is the explicit door for
# it: identical to the guarded read minus the anti-join, opened only by name.
# What it costs, stated where the door is: a model trained through this door
# can no longer be graded on the holdout it consumed, so its evaluation must
# come from a fresh holdout or k-fold — the trainer that opens it owns that.
_TRAINING_LABEL_ROWS_ALL_SQL = """
    SELECT itl.image_id, itl.state
    FROM image_tag_labels itl
    WHERE itl.tag_id = %(tag_id)s::bigint
      AND itl.source IN ('human', 'human_confirmed')
      AND itl.state = ANY(%(states)s::text[])
    ORDER BY itl.image_id
"""

_COHORT_SQL = """
    SELECT id, name, purpose, frame_size, model, revision, drawn_at, sealed_at,
           sealed_by, note
    FROM tag_exam_cohorts WHERE name = %(name)s
"""

_COHORT_SIZE_SQL = """
    SELECT count(*)::int FROM tag_exam_members WHERE cohort_id = %(cohort_id)s
"""

_HOLDOUT_SIZE_SQL = (
    "SELECT count(*)::int FROM tag_exam_members hx "
    "JOIN tag_exam_cohorts hc ON hc.id = hx.cohort_id AND hc.purpose = 'holdout'"
)

TRAINING_STATES = ("positive", "negative")


def training_label_rows(
    conn: psycopg.Connection, *, tag_id: int,
    states: tuple[str, ...] = TRAINING_STATES, include_holdout: bool = False,
) -> list[tuple[int, str]]:
    """Every (image_id, state) a probe for this tag may train on.

    `include_holdout=True` opens the operator-sanctioned door to the exam's own
    labels (see _TRAINING_LABEL_ROWS_ALL_SQL for what that costs). Default off:
    a caller that wants the holdout has to say so, in code, by name.

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
    sql = _TRAINING_LABEL_ROWS_ALL_SQL if include_holdout else _TRAINING_LABEL_ROWS_SQL
    with conn.cursor() as cur:
        cur.execute(sql, {"tag_id": tag_id, "states": list(states)})
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


def get_cohort(conn: psycopg.Connection, *, name: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_COHORT_SQL, {"name": name})
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("id", "name", "purpose", "frame_size", "model", "revision", "drawn_at",
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
