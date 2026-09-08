"""The tagging bake-off: one head per tag, per encoder arm, per training mode.

ENCODER-DECISION.md §5.2 "Set 1" and §5.4 readout 4, made runnable. An **arm** is
one encoder configuration (the same seven identity facts migration 480 keys on,
plus a short human name); a **mode** is one of `tag_heads.MODES`; a **head** is one
tag. The cross product is trained on CPU from frozen vectors — the GPU job that
produces those vectors is a separate lane and writes
`dedup_sim.tag_head_bakeoff_vectors` before this runs.

WHAT THIS MODULE IS ALLOWED TO READ. Labels come through
`machine_labeling.training_rows` — the one door — and exam answers through
`tag_exam.answers`, graded by `exam_machine_review.human_verdict`, the ratified
rule imported rather than restated. This module contains **no SQL naming
image_tag_labels**, deliberately, exactly as `toolkit/tag_heads.py` contains none:
the holdout census (tests/test_holdout_exclusion_census.py) then has nothing to
exempt here, because there is nothing here to exempt.

TWO SCORED POPULATIONS PER HEAD, and they answer different questions.
  * `split='cv'` — every training row, scored out-of-fold on grouped folds. It
    says how the head does on the material it was built from, with the leakage
    closed. Large n, but the labels are the same operator's.
  * `split='exam'` — the sealed 250-image holdout, scored by the head refit on
    all training rows. Small n and thin per head (§5.2: ten of the eighteen tags
    were backfilled as declared negatives), but it is the only population the
    head never consumed. Both are stored; neither substitutes for the other.

EVERY MODE IS GRADED ON THE SAME ROWS. The modes differ in what reaches the fit,
never in what is graded — otherwise "positive-only did better" could just mean
"positive-only was asked an easier question". `tag_heads._fold_training_rows`
enforces that split.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import psycopg

from toolkit import exam_machine_review as emr
from toolkit import machine_labeling
from toolkit import tag_heads as th

LOG = logging.getLogger(__name__)

DEFAULT_MIN_TRAIN_POSITIVES = 100
DEFAULT_EXAM_COHORT = "exam_v1"
DEFAULT_EXAM_SET = "all"

# The bake-off's own vector store may hold a hundred thousand rows; one bounded
# array parameter per statement, as tag_heads does.
_ID_CHUNK = 2000


class BakeoffError(RuntimeError):
    """A run that cannot honestly produce a comparison."""


# --- the store --------------------------------------------------------------

_RUNS_SQL = """
    SELECT id, created_at, label, note, status, manifest_key, heads,
           min_train_positives
    FROM dedup_sim.tag_head_bakeoff_runs
    WHERE (%(run_id)s::bigint IS NULL OR id = %(run_id)s::bigint)
    ORDER BY id DESC
    LIMIT %(limit)s
"""

_ARMS_SQL = """
    SELECT id, run_id, arm, model, revision, library, pooling, resolution,
           preprocessing, dtype, dim, status, note
    FROM dedup_sim.tag_head_bakeoff_arms
    WHERE run_id = %(run_id)s
      AND (%(names)s::text[] IS NULL OR arm = ANY(%(names)s::text[]))
    ORDER BY id
"""

_ARM_VECTOR_SQL = """
    SELECT v.image_id, v.embedding
    FROM dedup_sim.tag_head_bakeoff_vectors v
    WHERE v.arm_id = %(arm_id)s
      AND v.image_id = ANY(%(image_ids)s::bigint[])
    ORDER BY v.image_id
"""

_ARM_VECTOR_COUNT_SQL = """
    SELECT count(*)::bigint FROM dedup_sim.tag_head_bakeoff_vectors
    WHERE arm_id = %(arm_id)s
"""

_UPSERT_VECTOR_SQL = """
    INSERT INTO dedup_sim.tag_head_bakeoff_vectors (arm_id, image_id, embedding)
    VALUES (%(arm_id)s, %(image_id)s, %(embedding)s::halfvec)
    ON CONFLICT (arm_id, image_id) DO UPDATE SET embedding = EXCLUDED.embedding
"""

_UPSERT_SCORE_SQL = """
    INSERT INTO dedup_sim.tag_head_bakeoff_scores (
      arm_id, mode, tag_id, image_id, split, fold, label, score, predicted
    ) VALUES (
      %(arm_id)s, %(mode)s, %(tag_id)s, %(image_id)s, %(split)s, %(fold)s,
      %(label)s, %(score)s, %(predicted)s
    )
    ON CONFLICT (arm_id, mode, tag_id, image_id, split) DO UPDATE
      SET fold = EXCLUDED.fold, label = EXCLUDED.label,
          score = EXCLUDED.score, predicted = EXCLUDED.predicted
"""

_DELETE_SCORES_SQL = """
    DELETE FROM dedup_sim.tag_head_bakeoff_scores
    WHERE arm_id = %(arm_id)s AND mode = %(mode)s AND tag_id = %(tag_id)s
"""

_UPSERT_METRICS_SQL = """
    INSERT INTO dedup_sim.tag_head_bakeoff_metrics (
      arm_id, mode, tag_id, n_pos, n_neg, n_groups,
      cv_precision, cv_recall, cv_f1, cv_graded_n, cv_tp, cv_fp, cv_tn, cv_fn,
      exam_precision, exam_recall, exam_f1, exam_graded_n, exam_abstained_n,
      exam_tp, exam_fp, exam_tn, exam_fn,
      threshold, dataset_hash, status, note, trained_at
    ) VALUES (
      %(arm_id)s, %(mode)s, %(tag_id)s, %(n_pos)s, %(n_neg)s, %(n_groups)s,
      %(cv_precision)s, %(cv_recall)s, %(cv_f1)s, %(cv_graded_n)s,
      %(cv_tp)s, %(cv_fp)s, %(cv_tn)s, %(cv_fn)s,
      %(exam_precision)s, %(exam_recall)s, %(exam_f1)s, %(exam_graded_n)s,
      %(exam_abstained_n)s, %(exam_tp)s, %(exam_fp)s, %(exam_tn)s, %(exam_fn)s,
      %(threshold)s, %(dataset_hash)s, %(status)s, %(note)s, %(trained_at)s
    )
    ON CONFLICT (arm_id, mode, tag_id) DO UPDATE SET
      n_pos = EXCLUDED.n_pos, n_neg = EXCLUDED.n_neg, n_groups = EXCLUDED.n_groups,
      cv_precision = EXCLUDED.cv_precision, cv_recall = EXCLUDED.cv_recall,
      cv_f1 = EXCLUDED.cv_f1, cv_graded_n = EXCLUDED.cv_graded_n,
      cv_tp = EXCLUDED.cv_tp, cv_fp = EXCLUDED.cv_fp,
      cv_tn = EXCLUDED.cv_tn, cv_fn = EXCLUDED.cv_fn,
      exam_precision = EXCLUDED.exam_precision, exam_recall = EXCLUDED.exam_recall,
      exam_f1 = EXCLUDED.exam_f1, exam_graded_n = EXCLUDED.exam_graded_n,
      exam_abstained_n = EXCLUDED.exam_abstained_n,
      exam_tp = EXCLUDED.exam_tp, exam_fp = EXCLUDED.exam_fp,
      exam_tn = EXCLUDED.exam_tn, exam_fn = EXCLUDED.exam_fn,
      threshold = EXCLUDED.threshold, dataset_hash = EXCLUDED.dataset_hash,
      status = EXCLUDED.status, note = EXCLUDED.note,
      trained_at = EXCLUDED.trained_at
"""

_METRICS_SQL = """
    SELECT m.arm_id, a.arm, m.mode, m.tag_id, t.label,
           m.n_pos, m.n_neg, m.n_groups,
           m.cv_precision, m.cv_recall, m.cv_f1, m.cv_graded_n,
           m.cv_tp, m.cv_fp, m.cv_tn, m.cv_fn,
           m.exam_precision, m.exam_recall, m.exam_f1, m.exam_graded_n,
           m.exam_abstained_n, m.exam_tp, m.exam_fp, m.exam_tn, m.exam_fn,
           m.threshold, m.dataset_hash, m.status, m.note, m.trained_at
    FROM dedup_sim.tag_head_bakeoff_metrics m
    JOIN dedup_sim.tag_head_bakeoff_arms a ON a.id = m.arm_id
    LEFT JOIN tag_taxonomy t ON t.id = m.tag_id
    WHERE a.run_id = %(run_id)s
    ORDER BY a.id, m.mode, m.tag_id
"""

_DONE_KEYS_SQL = """
    SELECT m.arm_id, m.mode, m.tag_id
    FROM dedup_sim.tag_head_bakeoff_metrics m
    JOIN dedup_sim.tag_head_bakeoff_arms a ON a.id = m.arm_id
    WHERE a.run_id = %(run_id)s
"""

_SET_RUN_STATUS_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_runs
    SET status = %(status)s, heads = %(heads)s::bigint[]
    WHERE id = %(run_id)s
"""

_SET_ARM_STATUS_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_arms
    SET status = %(status)s, dim = coalesce(%(dim)s, dim), note = %(note)s
    WHERE id = %(arm_id)s
"""

_ACTIVE_TAGS_SQL = """
    SELECT id, label FROM tag_taxonomy WHERE active ORDER BY id
"""


@dataclass(frozen=True)
class Arm:
    """One encoder configuration in one run."""
    id: int
    run_id: int
    arm: str
    encoder: th.EncoderIdentity
    dim: int | None = None
    status: str = "pending"
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "run_id": self.run_id, "arm": self.arm,
                "dim": self.dim, "status": self.status, "note": self.note,
                **self.encoder.as_dict()}


def _chunks(ids: Sequence[int], size: int = _ID_CHUNK) -> Iterable[list[int]]:
    for i in range(0, len(ids), size):
        yield list(ids[i:i + size])


def list_runs(
    conn: psycopg.Connection, *, run_id: int | None = None, limit: int = 25,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_RUNS_SQL, {"run_id": run_id, "limit": max(1, int(limit))})
        rows = cur.fetchall()
    return [
        {"id": int(r[0]), "created_at": r[1].isoformat() if r[1] else None,
         "label": r[2], "note": r[3], "status": r[4], "manifest_key": r[5],
         "heads": [int(x) for x in (r[6] or [])], "min_train_positives": int(r[7])}
        for r in rows
    ]


def get_run(conn: psycopg.Connection, *, run_id: int) -> dict[str, Any] | None:
    found = list_runs(conn, run_id=run_id, limit=1)
    return found[0] if found else None


def list_arms(
    conn: psycopg.Connection, *, run_id: int, names: Sequence[str] | None = None,
) -> list[Arm]:
    with conn.cursor() as cur:
        cur.execute(_ARMS_SQL, {"run_id": int(run_id),
                                "names": list(names) if names else None})
        rows = cur.fetchall()
    return [
        Arm(id=int(r[0]), run_id=int(r[1]), arm=str(r[2]),
            encoder=th.EncoderIdentity(
                model=str(r[3]), revision=str(r[4]), library=str(r[5]),
                pooling=str(r[6]), resolution=int(r[7]), preprocessing=str(r[8]),
                dtype=str(r[9])),
            dim=None if r[10] is None else int(r[10]),
            status=str(r[11]), note=r[12])
        for r in rows
    ]


def arm_vector_count(conn: psycopg.Connection, *, arm_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(_ARM_VECTOR_COUNT_SQL, {"arm_id": int(arm_id)})
        row = cur.fetchone()
    return int(row[0]) if row else 0


def arm_vectors(
    conn: psycopg.Connection, *, arm_id: int, image_ids: Sequence[int],
) -> dict[int, tuple[float, ...]]:
    """One arm's vectors for the images asked for. Missing ids are simply absent —
    `assemble_dataset` reports them as `missing_embedding` rather than training a
    head on whatever happened to have landed."""
    out: dict[int, tuple[float, ...]] = {}
    for chunk in _chunks(sorted({int(i) for i in image_ids})):
        with conn.cursor() as cur:
            cur.execute(_ARM_VECTOR_SQL, {"arm_id": int(arm_id), "image_ids": chunk})
            for image_id, vec in cur.fetchall():
                out[int(image_id)] = th.parse_vector(vec)
    return out


def write_vectors(
    conn: psycopg.Connection, *, arm_id: int,
    vectors: dict[int, Sequence[float]],
) -> int:
    """Upsert one arm's vectors. The GPU job (phase 2) is the real writer; this
    exists so a test — and a hand-run of a single arm — can populate the store
    without one."""
    rows = [
        {"arm_id": int(arm_id), "image_id": int(image_id),
         "embedding": "[" + ",".join(repr(float(x)) for x in vec) + "]"}
        for image_id, vec in sorted(vectors.items())
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_VECTOR_SQL, rows)
    return len(rows)


# --- head selection ---------------------------------------------------------

@dataclass(frozen=True)
class HeadPlan:
    """One tag, and why it was (or was not) selected."""
    tag_id: int
    label: str
    n_pos: int
    n_neg: int
    selected: bool
    reason: str = ""


def select_heads(
    conn: psycopg.Connection, *,
    min_train_positives: int = DEFAULT_MIN_TRAIN_POSITIVES,
    tag_ids: Sequence[int] | None = None,
) -> list[HeadPlan]:
    """Every active tag, with its admitted counts and whether it makes the cut.

    A FLAG, NEVER A LIST. The set of trainable heads changes every time the
    operator admits labels; a hard-coded list would be wrong within a day and
    would quietly decide the experiment's scope. `min_train_positives` is the
    whole rule, and every tag it rejects is still returned with the count that
    rejected it, so "which heads were left out and by how much" is answerable
    without re-running anything.
    """
    with conn.cursor() as cur:
        cur.execute(_ACTIVE_TAGS_SQL)
        tags = [(int(r[0]), str(r[1])) for r in cur.fetchall()]
    wanted = {int(t) for t in tag_ids} if tag_ids else None
    out: list[HeadPlan] = []
    for tag_id, label in tags:
        if wanted is not None and tag_id not in wanted:
            continue
        rows = machine_labeling.training_rows(conn, tag_id=tag_id)
        n_pos = sum(1 for _, state in rows if state == "positive")
        n_neg = sum(1 for _, state in rows if state == "negative")
        ok = n_pos >= int(min_train_positives)
        out.append(HeadPlan(
            tag_id=tag_id, label=label, n_pos=n_pos, n_neg=n_neg, selected=ok,
            reason="" if ok else
            f"{n_pos} admitted positives < {int(min_train_positives)}"))
    return out


def excluded_image_ids(conn: psycopg.Connection, *, tag_id: int) -> set[int]:
    """Images the operator LEFT OUT for this head.

    Needed only to keep them out of another head's free negatives: "left out"
    means the operator declined to call it either way, and calling it a negative
    on their behalf is precisely the judgment they refused to make. Read through
    `machine_labeling.training_set_page`, an existing door that already carries
    the holdout exclusion — this module opens none of its own.
    """
    out: set[int] = set()
    offset = 0
    while True:
        page = machine_labeling.training_set_page(
            conn, tag_id=int(tag_id), state="excluded",
            limit=machine_labeling.PAGE_MAX, offset=offset)
        out.update(int(r["image_id"]) for r in page)
        if len(page) < machine_labeling.PAGE_MAX:
            return out
        offset += len(page)


def free_negatives_for(
    *, tag_id: int, positives_by_tag: dict[int, set[int]],
    own_excluded: set[int], groups: dict[int, int | None],
    vectors: dict[int, tuple[float, ...]],
) -> list[th.DatasetRow]:
    """The OTHER selected heads' positives, as this head's free negatives.

    THE ONE INVARIANT: an image that is positive for this head, or that the
    operator left out for this head, is never one of its negatives. A photo of a
    kitchen is a perfectly good "not a bathroom" — but a photo the operator
    positively tagged `kitchen` AND `living room` is not a negative for either,
    and neither is one they refused to rule on. Everything else another head
    called positive is free material: no label-day was spent to obtain it, which
    is the entire point of the mode.
    """
    own = positives_by_tag.get(int(tag_id), set())
    forbidden = own | own_excluded
    candidates = sorted(
        {i for t, ids in positives_by_tag.items() if int(t) != int(tag_id)
         for i in ids} - forbidden)
    rows: list[th.DatasetRow] = []
    for image_id in candidates:
        vec = vectors.get(image_id)
        if vec is None:
            continue
        listing_id = groups.get(image_id)
        rows.append(th.DatasetRow(
            image_id=image_id,
            # Mirrors assemble_dataset: an unattributed image gets a singleton
            # group keyed negatively off its own id, so it can never collide with
            # a real listing and can never straddle a fold.
            listing_id=-image_id if listing_id is None else int(listing_id),
            label=0, embedding=vec))
    return rows


# --- the exam ---------------------------------------------------------------

@dataclass(frozen=True)
class ExamSitting:
    """The sealed exam, read once per run: which images, and the human verdict
    per (image, tag) under the ratified rule."""
    cohort_id: int
    set_id: int
    tag_ids: tuple[int, ...]
    rows: tuple[dict[str, Any], ...]

    @property
    def image_ids(self) -> list[int]:
        return sorted({int(r["image_id"]) for r in self.rows})

    def verdict(self, *, image_id: int, tag_id: int) -> str:
        for row in self.rows:
            if int(row["image_id"]) == int(image_id):
                return emr.human_verdict(row, int(tag_id))
        return "skip"


def load_exam(
    conn: psycopg.Connection, *, cohort: str = DEFAULT_EXAM_COHORT,
    set_name: str = DEFAULT_EXAM_SET,
) -> ExamSitting | None:
    """The sitting's answers, through `tag_exam.answers` — the same read
    `scripts/exam_agreement.py` grades, so a head and the machine review are
    measured against the identical cells. None when the cohort or set is absent
    (a run without an exam still produces CV numbers)."""
    from toolkit import exam_suggestions as sugg
    from toolkit import tag_exam, tag_holdout

    cohort_row = tag_holdout.get_cohort(conn, name=cohort)
    exam_set = sugg.get_set(conn, name=set_name)
    if cohort_row is None or exam_set is None:
        return None
    tag_ids = [int(t) for t in exam_set["tag_ids"]]
    rows = tag_exam.answers(conn, cohort_id=int(cohort_row["id"]),
                            tag_ids=tag_ids, set_id=int(exam_set["id"]))
    return ExamSitting(cohort_id=int(cohort_row["id"]), set_id=int(exam_set["id"]),
                       tag_ids=tuple(tag_ids), rows=tuple(rows))


@dataclass(frozen=True)
class ExamGrade:
    """One head's exam result, under the ratified grading rule."""
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0
    abstained: int = 0
    scores: tuple[tuple[int, int | None, float, bool], ...] = ()  # image, label, score, predicted

    @property
    def graded(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return (2 * p * r / (p + r)) if (p and r) else None


def grade_exam(
    *, sitting: ExamSitting, tag_id: int, artifact: dict[str, Any],
    threshold: float, vectors: dict[int, tuple[float, ...]],
) -> ExamGrade:
    """Score the sitting's images with a refit head and grade the cells that grade.

    THE RATIFIED RULE, unchanged (scripts/exam_agreement.py): a cell grades only
    when both sides said yes or no; an abstention on either side grades nothing;
    a declared default is not a judgment. The head never abstains — a logistic
    always has an opinion — so every abstention here is the human's, counted
    apart and stored with a NULL label so the page can still show the photo.
    Precision and recall are None, never 0.0, when nothing was proposed.
    """
    tp = fp = tn = fn = abstained = 0
    scores: list[tuple[int, int | None, float, bool]] = []
    for row in sitting.rows:
        image_id = int(row["image_id"])
        vec = vectors.get(image_id)
        if vec is None:
            continue
        score = th.score_embedding(artifact, vec)
        predicted = score >= float(threshold)
        human = emr.human_verdict(row, int(tag_id))
        if human not in emr.GRADED:
            abstained += 1
            scores.append((image_id, None, float(score), bool(predicted)))
            continue
        label = 1 if human == "yes" else 0
        if label == 1:
            tp, fn = (tp + 1, fn) if predicted else (tp, fn + 1)
        else:
            fp, tn = (fp + 1, tn) if predicted else (fp, tn + 1)
        scores.append((image_id, label, float(score), bool(predicted)))
    return ExamGrade(tp=tp, fp=fp, tn=tn, fn=fn, abstained=abstained,
                     scores=tuple(sorted(scores)))


# --- the run ----------------------------------------------------------------

@dataclass
class HeadOutcome:
    """One (arm, mode, tag) cell: what was written, or why nothing was."""
    arm_id: int
    mode: str
    tag_id: int
    status: str
    note: str = ""
    n_scores: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)


def done_keys(conn: psycopg.Connection, *, run_id: int) -> set[tuple[int, str, int]]:
    """(arm_id, mode, tag_id) triples this run already has metrics for — what
    makes the runner resumable rather than all-or-nothing over a cross product
    that takes hours."""
    with conn.cursor() as cur:
        cur.execute(_DONE_KEYS_SQL, {"run_id": int(run_id)})
        return {(int(a), str(m), int(t)) for a, m, t in cur.fetchall()}


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def write_head_result(
    conn: psycopg.Connection, *, arm_id: int, mode: str, tag_id: int,
    head: th.TrainedHead, exam: ExamGrade | None, trained_at: datetime,
    note: str = "",
) -> HeadOutcome:
    """One cell's scores and metrics, written as a replacement for whatever was
    there. The DELETE ahead of the inserts matters: a re-run over a shrunken
    label set would otherwise leave the previous run's extra rows behind, and the
    bucket counts would be a mixture of two experiments."""
    m = head.metrics
    rows: list[dict[str, Any]] = [
        {"arm_id": int(arm_id), "mode": mode, "tag_id": int(tag_id),
         "image_id": int(p.image_id), "split": "cv", "fold": int(p.fold),
         "label": int(p.label), "score": float(p.score),
         "predicted": bool(p.predicted)}
        for p in head.oof
    ]
    if exam is not None:
        rows += [
            {"arm_id": int(arm_id), "mode": mode, "tag_id": int(tag_id),
             "image_id": int(image_id), "split": "exam", "fold": None,
             "label": None if label is None else int(label),
             "score": float(score), "predicted": bool(predicted)}
            for image_id, label, score, predicted in exam.scores
        ]
    params = {
        "arm_id": int(arm_id), "mode": mode, "tag_id": int(tag_id),
        "n_pos": head.snapshot.n_positive, "n_neg": head.snapshot.n_negative,
        "n_groups": head.snapshot.n_groups,
        "cv_precision": _rate(m.true_positives, m.true_positives + m.false_positives),
        "cv_recall": _rate(m.true_positives, m.true_positives + m.false_negatives),
        "cv_f1": None, "cv_graded_n": m.graded_n,
        "cv_tp": m.true_positives, "cv_fp": m.false_positives,
        "cv_tn": m.true_negatives, "cv_fn": m.false_negatives,
        "exam_precision": exam.precision if exam else None,
        "exam_recall": exam.recall if exam else None,
        "exam_f1": exam.f1 if exam else None,
        "exam_graded_n": exam.graded if exam else 0,
        "exam_abstained_n": exam.abstained if exam else 0,
        "exam_tp": exam.tp if exam else 0, "exam_fp": exam.fp if exam else 0,
        "exam_tn": exam.tn if exam else 0, "exam_fn": exam.fn if exam else 0,
        "threshold": float(head.threshold),
        "dataset_hash": head.snapshot.dataset_hash,
        "status": "ok", "note": note or None, "trained_at": trained_at,
    }
    p, r = params["cv_precision"], params["cv_recall"]
    params["cv_f1"] = (2 * p * r / (p + r)) if (p and r) else None
    with conn.cursor() as cur:
        cur.execute(_DELETE_SCORES_SQL,
                    {"arm_id": int(arm_id), "mode": mode, "tag_id": int(tag_id)})
        if rows:
            cur.executemany(_UPSERT_SCORE_SQL, rows)
        cur.execute(_UPSERT_METRICS_SQL, params)
    return HeadOutcome(arm_id=int(arm_id), mode=mode, tag_id=int(tag_id),
                       status="ok", note=note, n_scores=len(rows), metrics=params)


def write_head_failure(
    conn: psycopg.Connection, *, arm_id: int, mode: str, tag_id: int,
    n_pos: int, n_neg: int, reason: str, trained_at: datetime,
) -> HeadOutcome:
    """A cell that could not be trained is RECORDED, not skipped silently.

    A missing row and a row that says "fewer than two positive listing-groups"
    read identically on a page unless the second one exists. It also makes the
    runner's resume honest: a failure is a decided cell, and `--force` is how you
    ask for it to be retried."""
    params = {
        "arm_id": int(arm_id), "mode": mode, "tag_id": int(tag_id),
        "n_pos": int(n_pos), "n_neg": int(n_neg), "n_groups": None,
        "cv_precision": None, "cv_recall": None, "cv_f1": None, "cv_graded_n": 0,
        "cv_tp": 0, "cv_fp": 0, "cv_tn": 0, "cv_fn": 0,
        "exam_precision": None, "exam_recall": None, "exam_f1": None,
        "exam_graded_n": 0, "exam_abstained_n": 0,
        "exam_tp": 0, "exam_fp": 0, "exam_tn": 0, "exam_fn": 0,
        "threshold": float(th.DEFAULT_THRESHOLD), "dataset_hash": "",
        "status": "failed", "note": reason[:2000], "trained_at": trained_at,
    }
    with conn.cursor() as cur:
        cur.execute(_DELETE_SCORES_SQL,
                    {"arm_id": int(arm_id), "mode": mode, "tag_id": int(tag_id)})
        cur.execute(_UPSERT_METRICS_SQL, params)
    return HeadOutcome(arm_id=int(arm_id), mode=mode, tag_id=int(tag_id),
                       status="failed", note=reason)


def set_run_status(
    conn: psycopg.Connection, *, run_id: int, status: str, heads: Sequence[int],
) -> None:
    with conn.cursor() as cur:
        cur.execute(_SET_RUN_STATUS_SQL, {
            "run_id": int(run_id), "status": status,
            "heads": [int(h) for h in heads]})


def set_arm_status(
    conn: psycopg.Connection, *, arm_id: int, status: str,
    dim: int | None = None, note: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(_SET_ARM_STATUS_SQL, {"arm_id": int(arm_id), "status": status,
                                          "dim": dim, "note": note})


def run_bakeoff(
    conn: psycopg.Connection, *, run_id: int,
    modes: Sequence[str] = th.MODES,
    arm_names: Sequence[str] | None = None,
    min_train_positives: int | None = None,
    n_splits: int = th.DEFAULT_N_SPLITS,
    C: float = th.DEFAULT_C,
    threshold: float = th.DEFAULT_THRESHOLD,
    seed: int = th.DEFAULT_SEED,
    exam_cohort: str = DEFAULT_EXAM_COHORT,
    exam_set: str = DEFAULT_EXAM_SET,
    force: bool = False,
    trained_at: datetime | None = None,
) -> list[HeadOutcome]:
    """Train and score the whole arm x mode x head cross product for one run.

    Ordered arm-outermost so one arm's vectors are read once and reused across
    every mode and head — the vectors are the expensive read, the fits are not.
    """
    bad = [m for m in modes if m not in th.MODES]
    if bad:
        raise BakeoffError(f"unknown mode(s) {bad}; expected among {th.MODES}")
    run = get_run(conn, run_id=run_id)
    if run is None:
        raise BakeoffError(f"bake-off run {run_id} does not exist")
    floor = int(min_train_positives if min_train_positives is not None
                else run["min_train_positives"])
    stamp = trained_at or datetime.now(timezone.utc)

    plans = [p for p in select_heads(conn, min_train_positives=floor) if p.selected]
    if not plans:
        raise BakeoffError(
            f"no head has {floor} admitted training positives — lower "
            "--min-train-positives, or admit more labels first")
    set_run_status(conn, run_id=run_id, status="running",
                   heads=[p.tag_id for p in plans])

    arms = list_arms(conn, run_id=run_id, names=arm_names)
    if not arms:
        raise BakeoffError(f"run {run_id} has no arms" +
                           (f" named {list(arm_names)}" if arm_names else ""))

    sitting = load_exam(conn, cohort=exam_cohort, set_name=exam_set)
    if sitting is None:
        LOG.warning("BAKEOFF no sealed exam (%s/%s) — CV numbers only",
                    exam_cohort, exam_set)

    already = set() if force else done_keys(conn, run_id=run_id)
    positives_by_tag = {
        p.tag_id: {i for i, state in machine_labeling.training_rows(conn, tag_id=p.tag_id)
                   if state == "positive"}
        for p in plans
    }
    excluded_by_tag = {p.tag_id: excluded_image_ids(conn, tag_id=p.tag_id)
                       for p in plans}

    outcomes: list[HeadOutcome] = []
    for arm in arms:
        vectors = _arm_vector_cache(conn, arm=arm, plans=plans, sitting=sitting,
                                    positives_by_tag=positives_by_tag,
                                    excluded_by_tag=excluded_by_tag)
        if not vectors:
            # The GPU job has not reached this arm. Say so on the ARM rather than
            # writing one identical "nothing to train on" failure per head — the
            # cause is one missing embedding pass, not sixty broken heads.
            LOG.warning("BAKEOFF arm=%s has no vectors — skipped", arm.arm)
            set_arm_status(conn, arm_id=arm.id, status="failed", dim=None,
                           note="no vectors stored for this arm yet")
            continue
        dim = len(next(iter(vectors.values())))
        set_arm_status(conn, arm_id=arm.id, status="running", dim=dim, note=None)
        for mode in modes:
            for plan in plans:
                key = (arm.id, mode, plan.tag_id)
                if key in already:
                    continue
                outcomes.append(_run_cell(
                    conn, arm=arm, mode=mode, plan=plan, vectors=vectors,
                    positives_by_tag=positives_by_tag,
                    excluded_by_tag=excluded_by_tag, sitting=sitting,
                    n_splits=n_splits, C=C, threshold=threshold, seed=seed,
                    trained_at=stamp))
        failures = sum(1 for o in outcomes if o.arm_id == arm.id and o.status == "failed")
        set_arm_status(conn, arm_id=arm.id, status="ok", dim=dim,
                       note=f"{failures} head(s) could not be graded" if failures else None)

    set_run_status(conn, run_id=run_id, status="ok", heads=[p.tag_id for p in plans])
    return outcomes


def _arm_vector_cache(
    conn: psycopg.Connection, *, arm: Arm, plans: Sequence[HeadPlan],
    sitting: ExamSitting | None, positives_by_tag: dict[int, set[int]],
    excluded_by_tag: dict[int, set[int]],
) -> dict[int, tuple[float, ...]]:
    """Every image this arm will be asked about, read in one pass: the training
    rows of every selected head (their positives AND negatives), and the exam."""
    wanted: set[int] = set()
    for plan in plans:
        wanted |= {i for i, _ in machine_labeling.training_rows(conn, tag_id=plan.tag_id)}
    for ids in positives_by_tag.values():
        wanted |= ids
    if sitting is not None:
        wanted |= set(sitting.image_ids)
    return arm_vectors(conn, arm_id=arm.id, image_ids=sorted(wanted))


def _run_cell(
    conn: psycopg.Connection, *, arm: Arm, mode: str, plan: HeadPlan,
    vectors: dict[int, tuple[float, ...]],
    positives_by_tag: dict[int, set[int]], excluded_by_tag: dict[int, set[int]],
    sitting: ExamSitting | None, n_splits: int, C: float, threshold: float,
    seed: int, trained_at: datetime,
) -> HeadOutcome:
    try:
        snapshot = th.assemble_dataset(
            conn, tag_id=plan.tag_id, encoder=arm.encoder,
            vectors=th.mapping_vector_source(vectors))
        free: list[th.DatasetRow] = []
        if mode == th.MODE_POS_ONLY_FREE_NEG:
            free = free_negatives_for(
                tag_id=plan.tag_id, positives_by_tag=positives_by_tag,
                own_excluded=excluded_by_tag.get(plan.tag_id, set()),
                groups={r.image_id: r.listing_id for r in snapshot.rows},
                vectors=vectors)
            if not free:
                raise th.TagHeadError(
                    "no other selected head contributes a usable free negative")
        head = th.train_head(
            snapshot, trained_at=trained_at, mode=mode, free_negatives=free,
            n_splits=n_splits, C=C, threshold=threshold, seed=seed)
    except th.TagHeadError as exc:
        LOG.warning("BAKEOFF arm=%s mode=%s tag=%d FAILED: %s",
                    arm.arm, mode, plan.tag_id, exc)
        return write_head_failure(
            conn, arm_id=arm.id, mode=mode, tag_id=plan.tag_id,
            n_pos=plan.n_pos, n_neg=plan.n_neg, reason=str(exc),
            trained_at=trained_at)

    grade: ExamGrade | None = None
    note = ""
    if sitting is None:
        note = "no sealed exam sitting available"
    elif plan.tag_id not in sitting.tag_ids:
        # Reported, not silently zero: "this head was never on the exam" and
        # "this head scored nothing on the exam" are different facts.
        note = "head is not part of the exam sitting's tag list"
    else:
        grade = grade_exam(sitting=sitting, tag_id=plan.tag_id,
                           artifact=head.artifact, threshold=head.threshold,
                           vectors=vectors)
        if grade.graded == 0:
            note = "every exam cell for this head abstained"
    return write_head_result(
        conn, arm_id=arm.id, mode=mode, tag_id=plan.tag_id, head=head,
        exam=grade, trained_at=trained_at, note=note)


# --- reading the results back ----------------------------------------------

_METRIC_FIELDS = (
    "arm_id", "arm", "mode", "tag_id", "tag_label", "n_pos", "n_neg", "n_groups",
    "cv_precision", "cv_recall", "cv_f1", "cv_graded_n",
    "cv_tp", "cv_fp", "cv_tn", "cv_fn",
    "exam_precision", "exam_recall", "exam_f1", "exam_graded_n",
    "exam_abstained_n", "exam_tp", "exam_fp", "exam_tn", "exam_fn",
    "threshold", "dataset_hash", "status", "note", "trained_at",
)


OUTCOMES = ("tp", "fp", "fn", "tn")

# One expression, used by the page filter, the bucket counts and the tiles, so
# the three cannot disagree about what a false positive is. An abstained exam
# cell (label IS NULL) belongs to NO bucket: nothing was graded, so nothing was
# right or wrong.
_OUTCOME_SQL = """
    CASE
      WHEN s.label IS NULL THEN 'abstained'
      WHEN s.label = 1 AND s.predicted THEN 'tp'
      WHEN s.label = 0 AND s.predicted THEN 'fp'
      WHEN s.label = 1 AND NOT s.predicted THEN 'fn'
      ELSE 'tn'
    END
"""

# image_id is the tiebreaker on every paged read here: scores tie constantly
# (a centroid mode rounds many rows to the same cosine), and an ORDER BY that
# ties reshuffles rows between pages.
_IMAGES_PAGE_SQL = f"""
    SELECT DISTINCT s.image_id
    FROM dedup_sim.tag_head_bakeoff_scores s
    JOIN dedup_sim.tag_head_bakeoff_arms a ON a.id = s.arm_id
    WHERE a.run_id = %(run_id)s
      AND (%(arm_ids)s::bigint[] IS NULL OR s.arm_id = ANY(%(arm_ids)s::bigint[]))
      AND (%(mode)s::text IS NULL OR s.mode = %(mode)s::text)
      AND s.split = %(split)s
      AND (%(tag_id)s::bigint IS NULL OR s.tag_id = %(tag_id)s::bigint)
      AND (%(outcome)s::text IS NULL OR ({_OUTCOME_SQL}) = %(outcome)s::text)
      AND (%(after)s::bigint IS NULL OR s.image_id > %(after)s::bigint)
    ORDER BY s.image_id
    LIMIT %(limit)s
"""

_IMAGE_SCORES_SQL = f"""
    SELECT s.image_id, s.arm_id, a.arm, s.mode, s.tag_id, t.label, s.split,
           s.fold, s.label, s.score, s.predicted, ({_OUTCOME_SQL}) AS outcome
    FROM dedup_sim.tag_head_bakeoff_scores s
    JOIN dedup_sim.tag_head_bakeoff_arms a ON a.id = s.arm_id
    LEFT JOIN tag_taxonomy t ON t.id = s.tag_id
    WHERE a.run_id = %(run_id)s
      AND s.image_id = ANY(%(image_ids)s::bigint[])
      AND (%(arm_ids)s::bigint[] IS NULL OR s.arm_id = ANY(%(arm_ids)s::bigint[]))
      AND (%(mode)s::text IS NULL OR s.mode = %(mode)s::text)
      AND s.split = %(split)s
    ORDER BY s.image_id, s.arm_id, s.mode, s.tag_id
"""

_IMAGE_META_SQL = """
    SELECT i.id, i.listing_id, i.storage_path
    FROM images i
    WHERE i.id = ANY(%(image_ids)s::bigint[])
    ORDER BY i.id
"""

_BUCKET_COUNTS_SQL = f"""
    SELECT ({_OUTCOME_SQL}) AS outcome, count(*)::bigint
    FROM dedup_sim.tag_head_bakeoff_scores s
    WHERE s.arm_id = %(arm_id)s AND s.mode = %(mode)s
      AND s.tag_id = %(tag_id)s AND s.split = %(split)s
    GROUP BY 1
"""

_BUCKET_TILES_SQL = f"""
    SELECT s.image_id, i.listing_id, i.storage_path, s.score, s.label,
           s.predicted, s.fold
    FROM dedup_sim.tag_head_bakeoff_scores s
    JOIN images i ON i.id = s.image_id
    WHERE s.arm_id = %(arm_id)s AND s.mode = %(mode)s
      AND s.tag_id = %(tag_id)s AND s.split = %(split)s
      AND ({_OUTCOME_SQL}) = %(outcome)s
    ORDER BY s.score DESC, s.image_id DESC
    LIMIT %(limit)s OFFSET %(offset)s
"""

# 20 bins over the observed range, split by label. The range is measured, not
# assumed: a logistic mode's scores live in [0, 1] and a centroid mode's are
# cosines in [-1, 1], and a fixed axis would squeeze one of them into a corner.
_HISTOGRAM_SQL = """
    WITH bounds AS (
      SELECT min(score)::double precision AS lo, max(score)::double precision AS hi
      FROM dedup_sim.tag_head_bakeoff_scores
      WHERE arm_id = %(arm_id)s AND mode = %(mode)s
        AND tag_id = %(tag_id)s AND split = %(split)s
    )
    SELECT width_bucket(s.score::double precision, b.lo,
                        CASE WHEN b.hi > b.lo THEN b.hi ELSE b.lo + 1 END, 20) AS bin,
           s.label, count(*)::bigint, b.lo, b.hi
    FROM dedup_sim.tag_head_bakeoff_scores s
    CROSS JOIN bounds b
    WHERE s.arm_id = %(arm_id)s AND s.mode = %(mode)s
      AND s.tag_id = %(tag_id)s AND s.split = %(split)s
    GROUP BY 1, 2, b.lo, b.hi
    ORDER BY 1, 2
"""

_HISTOGRAM_BINS = 20


def run_images(
    conn: psycopg.Connection, *, run_id: int, split: str = "cv",
    arm_ids: Sequence[int] | None = None, mode: str | None = None,
    tag_id: int | None = None, outcome: str | None = None,
    after_image_id: int | None = None, limit: int = 60,
) -> dict[str, Any]:
    """View A: a page of images, each with every score the filters admit.

    The page is a page of IMAGES, not of score rows: the question it answers is
    "show me this photo and what each arm said about it", so an image appears
    once with its scores nested. `outcome` needs `tag_id` — an outcome is a
    verdict about one head, and "false positive across all heads at once" is not
    a thing.
    """
    if split not in ("cv", "exam"):
        raise ValueError(f"split must be cv or exam, got {split!r}")
    if outcome is not None:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
        if tag_id is None:
            raise ValueError("outcome needs a tag_id — it is one head's verdict")
    ids = [int(a) for a in arm_ids] if arm_ids else None
    page_limit = max(1, min(int(limit), 200))
    with conn.cursor() as cur:
        cur.execute(_IMAGES_PAGE_SQL, {
            "run_id": int(run_id), "arm_ids": ids, "mode": mode, "split": split,
            "tag_id": tag_id, "outcome": outcome, "after": after_image_id,
            "limit": page_limit})
        image_ids = [int(r[0]) for r in cur.fetchall()]
    if not image_ids:
        return {"images": [], "next_after_image_id": None}

    with conn.cursor() as cur:
        cur.execute(_IMAGE_META_SQL, {"image_ids": image_ids})
        meta = {int(r[0]): {"listing_id": None if r[1] is None else int(r[1]),
                            "storage_path": r[2]} for r in cur.fetchall()}
        cur.execute(_IMAGE_SCORES_SQL, {
            "run_id": int(run_id), "image_ids": image_ids, "arm_ids": ids,
            "mode": mode, "split": split})
        scores = cur.fetchall()

    by_image: dict[int, list[dict[str, Any]]] = {i: [] for i in image_ids}
    for row in scores:
        by_image.setdefault(int(row[0]), []).append({
            "arm_id": int(row[1]), "arm": row[2], "mode": row[3],
            "tag_id": int(row[4]), "tag_label": row[5], "split": row[6],
            "fold": None if row[7] is None else int(row[7]),
            "label": None if row[8] is None else int(row[8]),
            "score": float(row[9]), "predicted": bool(row[10]), "outcome": row[11],
        })
    images = [
        {"image_id": i, **meta.get(i, {"listing_id": None, "storage_path": None}),
         "scores": by_image.get(i, [])}
        for i in image_ids
    ]
    return {"images": images,
            "next_after_image_id": image_ids[-1] if len(image_ids) == page_limit
            else None}


def run_buckets(
    conn: psycopg.Connection, *, arm_id: int, mode: str, tag_id: int,
    split: str = "cv", limit: int = 24, offset: int = 0,
) -> dict[str, Any]:
    """View B: one head under one arm and mode, as its four outcome buckets.

    Each bucket carries its own count and a page of tiles ordered by score
    descending — the most confident mistake first, which is the one worth
    looking at (`tag_heads_eval.mistakes` orders its diagnostics the same way).
    `abstained` is reported beside the four, never inside one.
    """
    if split not in ("cv", "exam"):
        raise ValueError(f"split must be cv or exam, got {split!r}")
    keys = {"arm_id": int(arm_id), "mode": mode, "tag_id": int(tag_id),
            "split": split}
    tile_limit = max(1, min(int(limit), 200))
    with conn.cursor() as cur:
        cur.execute(_BUCKET_COUNTS_SQL, keys)
        counts = {str(r[0]): int(r[1]) for r in cur.fetchall()}
        buckets: dict[str, Any] = {}
        for outcome in OUTCOMES:
            cur.execute(_BUCKET_TILES_SQL, {
                **keys, "outcome": outcome, "limit": tile_limit,
                "offset": max(0, int(offset))})
            buckets[outcome] = {
                "count": counts.get(outcome, 0),
                "tiles": [
                    {"image_id": int(r[0]),
                     "listing_id": None if r[1] is None else int(r[1]),
                     "storage_path": r[2], "score": float(r[3]),
                     "label": None if r[4] is None else int(r[4]),
                     "predicted": bool(r[5]),
                     "fold": None if r[6] is None else int(r[6])}
                    for r in cur.fetchall()
                ],
            }
        cur.execute(_HISTOGRAM_SQL, keys)
        raw = cur.fetchall()
    lo = float(raw[0][3]) if raw and raw[0][3] is not None else 0.0
    hi = float(raw[0][4]) if raw and raw[0][4] is not None else 1.0
    positive = [0] * _HISTOGRAM_BINS
    negative = [0] * _HISTOGRAM_BINS
    abstained = [0] * _HISTOGRAM_BINS
    for bin_no, label, count, _lo, _hi in raw:
        # width_bucket returns 21 for a value exactly at the top of the range;
        # it belongs in the last bin, not in a phantom one past the end.
        index = min(max(int(bin_no) - 1, 0), _HISTOGRAM_BINS - 1)
        target = (abstained if label is None
                  else positive if int(label) == 1 else negative)
        target[index] += int(count)
    return {
        "arm_id": int(arm_id), "mode": mode, "tag_id": int(tag_id), "split": split,
        "abstained_count": counts.get("abstained", 0),
        "buckets": buckets,
        "histogram": {"bins": _HISTOGRAM_BINS, "lo": lo, "hi": hi,
                      "positive": positive, "negative": negative,
                      "abstained": abstained},
    }


def run_metrics(conn: psycopg.Connection, *, run_id: int) -> list[dict[str, Any]]:
    """Every arm x mode x tag metric row for one run, in one payload. Small by
    construction — a dozen arms x three modes x twenty heads is under a thousand
    rows — so the page never pages it."""
    with conn.cursor() as cur:
        cur.execute(_METRICS_SQL, {"run_id": int(run_id)})
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        record = dict(zip(_METRIC_FIELDS, row))
        for key in ("arm_id", "tag_id", "n_pos", "n_neg", "n_groups",
                    "cv_graded_n", "cv_tp", "cv_fp", "cv_tn", "cv_fn",
                    "exam_graded_n", "exam_abstained_n",
                    "exam_tp", "exam_fp", "exam_tn", "exam_fn"):
            if record[key] is not None:
                record[key] = int(record[key])
        for key in ("cv_precision", "cv_recall", "cv_f1", "exam_precision",
                    "exam_recall", "exam_f1", "threshold"):
            if record[key] is not None:
                record[key] = float(record[key])
        stamp = record["trained_at"]
        record["trained_at"] = stamp.isoformat() if stamp else None
        out.append(record)
    return out
