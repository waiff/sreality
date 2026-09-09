"""The versioned tag model: promote a bake-off's heads, score images, read winners.

WHAT A TAG MODEL IS. One frozen decision — this encoder configuration, this
training mode, this set of heads, these weights — with a `version` string as its
name. Migration 490 stores it: `tag_head_models` (the identity),
`tag_head_model_heads` (one artifact + its copied metrics per tag), and
`image_tag_scores` (one row per image per model version).

THE WINNER RULE (operator ruling 2026-09-09 (a)), which decides most of this
module's shape: **an image's tag is the head with the highest score, ties broken
toward the lower tag_id.** There are no per-head yes/no decisions in the product
at all. Three consequences, all deliberate:

  * Every head's probability is stored, not just the winner's — a winner is only
    meaningful beside the field it beat, and a later consumer may want the runner
    up.
  * No threshold and no boolean is stored. A head's own `threshold` lives inside
    its artifact because that is what the bake-off measured it at; it is evidence.
    A consumer that wants "and only if it is confident" applies its OWN floor to
    `winner_score`.
  * Adding a head is a NEW VERSION, never an edit. The winner is an argmax over a
    head set, so it changes meaning the moment the set does — which is exactly why
    `tag_head_models.heads` freezes the set and `image_tag_scores` is keyed by
    model.

THE ITERATION LOOP, which is the whole point of the shape (ruling 2026-09-09 (c):
the training set, the head set, the model and the parameters keep iterating in
parallel, and the full image pool is scored only afterwards):

    a new bake-off run  ->  promote  ->  score  ->  activate

`promote` writes a `candidate`: it exists, it has weights, nothing reads it.
`score` fills its store, resumably, at whatever pace. `activate` is a separate,
explicit, single-statement step, which is what guarantees no consumer ever meets
a half-scored version. Rolling forward is another promote; rolling back is
`activate` on the previous version.

WHERE THE LABELS COME FROM. Training goes through `tag_heads.assemble_dataset`,
whose one door is `machine_labeling.training_rows` — this module contains no SQL
naming `image_tag_labels`, deliberately, so tests/test_holdout_exclusion_census.py
has nothing here to exempt. scikit-learn is needed to PROMOTE (it trains) and
never to SCORE: inference is `tag_heads.score_embedding`, pure stdlib.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import psycopg

from toolkit import machine_labeling
from toolkit import tag_head_bakeoff as bo
from toolkit import tag_heads as th

LOG = logging.getLogger(__name__)

STATUS_CANDIDATE = "candidate"
STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"
STATUSES = (STATUS_CANDIDATE, STATUS_ACTIVE, STATUS_RETIRED)

# Two vector sources, named the way the CLI spells them.
SOURCE_PRODUCTION = "production"
SOURCE_BAKEOFF_PREFIX = "bakeoff"

DEFAULT_BATCH = 500

# Probabilities are stored to six decimals. Full float repr would roughly double
# the jsonb row for a difference no consumer can act on — a sixth decimal is
# already far below the noise of the head that produced it.
_SCORE_PLACES = 6

_ID_CHUNK = 2000


class TagModelError(RuntimeError):
    """A model that cannot be promoted, activated, scored or read honestly."""


# --- SQL --------------------------------------------------------------------

_MODEL_COLUMNS = """
    m.id, m.version, m.label, m.status, m.created_at, m.activated_at,
    m.source_run_id, m.source_arm, m.mode,
    m.model, m.revision, m.library, m.pooling, m.resolution, m.preprocessing,
    m.dtype, m.heads, m.dataset_hash, m.note
"""

_GET_MODEL_SQL = f"""
    SELECT {_MODEL_COLUMNS}
    FROM tag_head_models m
    WHERE (%(version)s::text IS NULL OR m.version = %(version)s::text)
      AND (%(model_id)s::bigint IS NULL OR m.id = %(model_id)s::bigint)
      AND (%(status)s::text IS NULL OR m.status = %(status)s::text)
    ORDER BY m.id DESC
    LIMIT 1
"""

_LIST_MODELS_SQL = f"""
    SELECT {_MODEL_COLUMNS},
           (SELECT count(*)::bigint FROM tag_head_model_heads h
             WHERE h.model_id = m.id) AS n_heads,
           (SELECT count(*)::bigint FROM image_tag_scores s
             WHERE s.model_id = m.id) AS n_scored
    FROM tag_head_models m
    ORDER BY m.id DESC
    LIMIT %(limit)s
"""

_INSERT_MODEL_SQL = """
    INSERT INTO tag_head_models (
      version, label, status, source_run_id, source_arm, mode,
      model, revision, library, pooling, resolution, preprocessing, dtype,
      heads, dataset_hash, note
    ) VALUES (
      %(version)s, %(label)s, %(status)s, %(source_run_id)s, %(source_arm)s,
      %(mode)s, %(model)s, %(revision)s, %(library)s, %(pooling)s,
      %(resolution)s, %(preprocessing)s, %(dtype)s, %(heads)s::bigint[],
      %(dataset_hash)s, %(note)s
    )
    RETURNING id
"""

_UPSERT_HEAD_SQL = """
    INSERT INTO tag_head_model_heads (model_id, tag_id, artifact, metrics)
    VALUES (%(model_id)s, %(tag_id)s, %(artifact)s::jsonb, %(metrics)s::jsonb)
    ON CONFLICT (model_id, tag_id) DO UPDATE
      SET artifact = EXCLUDED.artifact, metrics = EXCLUDED.metrics,
          created_at = now()
"""

_MODEL_HEADS_SQL = """
    SELECT h.tag_id, h.artifact, h.metrics, t.label
    FROM tag_head_model_heads h
    LEFT JOIN tag_taxonomy t ON t.id = h.tag_id
    WHERE h.model_id = %(model_id)s
    ORDER BY h.tag_id
"""

_RETIRE_ACTIVE_SQL = """
    UPDATE tag_head_models
    SET status = 'retired'
    WHERE status = 'active' AND id <> %(model_id)s
"""

_ACTIVATE_SQL = """
    UPDATE tag_head_models
    SET status = 'active', activated_at = coalesce(activated_at, now())
    WHERE id = %(model_id)s
"""

_UPSERT_SCORE_SQL = """
    INSERT INTO image_tag_scores (
      image_id, model_id, scores, winner_tag_id, winner_score, scored_at
    ) VALUES (
      %(image_id)s, %(model_id)s, %(scores)s::jsonb, %(winner_tag_id)s,
      %(winner_score)s, %(scored_at)s
    )
    ON CONFLICT (image_id, model_id) DO UPDATE
      SET scores = EXCLUDED.scores, winner_tag_id = EXCLUDED.winner_tag_id,
          winner_score = EXCLUDED.winner_score, scored_at = EXCLUDED.scored_at
"""

_SCORED_IDS_SQL = """
    SELECT s.image_id
    FROM image_tag_scores s
    WHERE s.model_id = %(model_id)s
      AND s.image_id = ANY(%(image_ids)s::bigint[])
"""

_WINNERS_SQL = """
    SELECT s.image_id, s.winner_tag_id, s.winner_score, s.scores, s.scored_at
    FROM image_tag_scores s
    WHERE s.model_id = %(model_id)s
      AND s.image_id = ANY(%(image_ids)s::bigint[])
    ORDER BY s.image_id
"""

_SCORED_COUNT_SQL = """
    SELECT count(*)::bigint FROM image_tag_scores WHERE model_id = %(model_id)s
"""

# The bake-off arm's stored image ids, paged by id — the first iteration's
# scoring population (the labelled photos plus the sealed exam, which is exactly
# what the GPU job embedded).
_BAKEOFF_IMAGE_IDS_SQL = """
    SELECT v.image_id
    FROM dedup_sim.tag_head_bakeoff_vectors v
    WHERE v.arm_id = %(arm_id)s
      AND (%(after)s::bigint IS NULL OR v.image_id > %(after)s::bigint)
    ORDER BY v.image_id
    LIMIT %(limit)s
"""

# The production population: image ids that already carry a vector under the
# model's seven identity facts. Nothing writes these for the tag-model lane yet —
# the corpus pass is a later wave — but the source must exist for that wave to be
# built against.
_PRODUCTION_IMAGE_IDS_SQL = """
    SELECT e.image_id
    FROM image_dinov3_embeddings e
    WHERE e.model = %(model)s
      AND e.revision = %(revision)s
      AND e.library = %(library)s
      AND e.pooling = %(pooling)s
      AND e.resolution = %(resolution)s::int
      AND e.preprocessing = %(preprocessing)s
      AND e.dtype = %(dtype)s
      AND (%(after)s::bigint IS NULL OR e.image_id > %(after)s::bigint)
    ORDER BY e.image_id
    LIMIT %(limit)s
"""


# --- the model --------------------------------------------------------------

@dataclass(frozen=True)
class TagModel:
    """One version's identity row."""
    id: int
    version: str
    label: str
    status: str
    mode: str
    encoder: th.EncoderIdentity
    heads: tuple[int, ...] = ()
    source_run_id: int | None = None
    source_arm: str | None = None
    dataset_hash: str | None = None
    note: str | None = None
    created_at: datetime | None = None
    activated_at: datetime | None = None
    n_heads: int | None = None
    n_scored: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "version": self.version, "label": self.label,
            "status": self.status, "mode": self.mode,
            "heads": list(self.heads),
            "source_run_id": self.source_run_id, "source_arm": self.source_arm,
            "dataset_hash": self.dataset_hash, "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "activated_at": (self.activated_at.isoformat()
                             if self.activated_at else None),
            **self.encoder.as_dict(),
        }
        if self.n_heads is not None:
            out["n_heads"] = int(self.n_heads)
        if self.n_scored is not None:
            out["n_scored"] = int(self.n_scored)
        return out


def _model_from_row(row: Sequence[Any]) -> TagModel:
    return TagModel(
        id=int(row[0]), version=str(row[1]), label=str(row[2]),
        status=str(row[3]), created_at=row[4], activated_at=row[5],
        source_run_id=None if row[6] is None else int(row[6]),
        source_arm=row[7], mode=str(row[8]),
        encoder=th.EncoderIdentity(
            model=str(row[9]), revision=str(row[10]), library=str(row[11]),
            pooling=str(row[12]), resolution=int(row[13]),
            preprocessing=str(row[14]), dtype=str(row[15])),
        heads=tuple(int(h) for h in (row[16] or [])),
        dataset_hash=row[17], note=row[18],
        n_heads=None if len(row) < 20 or row[19] is None else int(row[19]),
        n_scored=None if len(row) < 21 or row[20] is None else int(row[20]),
    )


def get_model(
    conn: psycopg.Connection, *, version: str | None = None,
    model_id: int | None = None, status: str | None = None,
) -> TagModel | None:
    """One model row by version, by id, or by status. None when there is none."""
    if version is None and model_id is None and status is None:
        raise TagModelError("name a version, an id or a status")
    with conn.cursor() as cur:
        cur.execute(_GET_MODEL_SQL, {
            "version": version, "model_id": model_id, "status": status})
        row = cur.fetchone()
    return _model_from_row(row) if row else None


def active_model(conn: psycopg.Connection) -> TagModel | None:
    """The one model consumers read. None until something is activated."""
    return get_model(conn, status=STATUS_ACTIVE)


def list_models(conn: psycopg.Connection, *, limit: int = 50) -> list[TagModel]:
    """Every version, newest first, each with its head count and how many images
    it has scored — the two numbers that say whether a candidate is ready to
    activate."""
    with conn.cursor() as cur:
        cur.execute(_LIST_MODELS_SQL, {"limit": max(1, int(limit))})
        return [_model_from_row(r) for r in cur.fetchall()]


@dataclass(frozen=True)
class ModelHead:
    """One head of one model: what scores, and how well it did when promoted."""
    tag_id: int
    artifact: dict[str, Any]
    metrics: dict[str, Any]
    label: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"tag_id": self.tag_id, "tag_label": self.label,
                "threshold": self.artifact.get("threshold"),
                "kind": self.artifact.get("kind"),
                "dimension": self.artifact.get("dimension"),
                "metrics": self.metrics}


def _as_json(raw: Any) -> dict[str, Any]:
    """jsonb arrives as a dict with psycopg3's default adapter and as text
    without one; accept both rather than depending on adapter registration."""
    if raw is None:
        return {}
    if isinstance(raw, (str, bytes)):
        return dict(json.loads(raw))
    return dict(raw)


def model_heads(conn: psycopg.Connection, *, model_id: int) -> list[ModelHead]:
    with conn.cursor() as cur:
        cur.execute(_MODEL_HEADS_SQL, {"model_id": int(model_id)})
        return [
            ModelHead(tag_id=int(r[0]), artifact=_as_json(r[1]),
                      metrics=_as_json(r[2]), label=r[3])
            for r in cur.fetchall()
        ]


def scored_count(conn: psycopg.Connection, *, model_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(_SCORED_COUNT_SQL, {"model_id": int(model_id)})
        row = cur.fetchone()
    return int(row[0]) if row else 0


# --- promotion --------------------------------------------------------------

@dataclass
class PromotedHead:
    """One head's promotion outcome — trained, or recorded as not trainable."""
    tag_id: int
    label: str
    status: str
    note: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


def _head_metrics(head: th.TrainedHead, run_metrics: Mapping[str, Any] | None,
                  ) -> dict[str, Any]:
    """What gets copied onto the head at promotion: the fit's own out-of-fold
    numbers, plus the bake-off's row for the same cell when one exists.

    A copy rather than a join. `dedup_sim` is dropped at Wave 8 and "how good was
    this head when we shipped it" must not go with it.
    """
    out: dict[str, Any] = {
        "cv": head.metrics.as_dict(),
        "threshold": float(head.threshold),
        "n_positive": head.snapshot.n_positive,
        "n_negative": head.snapshot.n_negative,
        "n_groups": head.snapshot.n_groups,
        "dataset_hash": head.snapshot.dataset_hash,
    }
    if run_metrics:
        out["bakeoff"] = {
            k: run_metrics.get(k) for k in (
                "arm", "mode", "cv_precision", "cv_recall", "cv_f1", "cv_graded_n",
                "exam_precision", "exam_recall", "exam_f1", "exam_graded_n",
                "exam_abstained_n", "threshold", "status", "note", "trained_at")
        }
    return out


def promote(
    conn: psycopg.Connection, *, run_id: int, arm: str, mode: str, version: str,
    label: str, note: str | None = None,
    tag_ids: Sequence[int] | None = None,
    n_splits: int = th.DEFAULT_N_SPLITS, C: float = th.DEFAULT_C,
    threshold: float = th.DEFAULT_THRESHOLD, seed: int = th.DEFAULT_SEED,
    trained_at: datetime | None = None,
) -> tuple[TagModel, list[PromotedHead]]:
    """Freeze one (run, arm, mode) cell of a bake-off into a `candidate` model.

    Trains ONE head per selected tag on that arm's stored vectors, refit on all
    admitted training rows — the same refit the bake-off's exam scoring already
    uses for its shipped weights, so a promoted head is the head the bake-off's
    exam column measured and not a differently-fitted cousin. The out-of-fold
    numbers come along as `metrics`, together with the bake-off's own row for the
    cell when it exists.

    The head set is the run's frozen `heads` array (what the operator's ready flag
    selected when the run executed) unless `tag_ids` names one explicitly. A tag
    that cannot be trained — too few positive listing-groups for a grouped split,
    no vectors under this arm — is REPORTED and left out of the model, never
    silently dropped: a model whose head set quietly shrank would still produce
    winners, and they would be argmaxes over a field nobody agreed to.

    Status is `candidate`. Nothing reads it until `activate`.
    """
    if mode not in th.MODES:
        raise TagModelError(f"unknown training mode {mode!r}; expected {th.MODES}")
    if not version.strip():
        raise TagModelError("a model version needs a name (e.g. 'v1')")
    if get_model(conn, version=version) is not None:
        raise TagModelError(
            f"version {version!r} already exists — a version is immutable; "
            "promote the next one under a new name")

    run = bo.get_run(conn, run_id=run_id)
    if run is None:
        raise TagModelError(f"bake-off run {run_id} does not exist")
    arms = bo.list_arms(conn, run_id=run_id, names=[arm])
    if not arms:
        raise TagModelError(f"run {run_id} has no arm named {arm!r}")
    the_arm = arms[0]

    wanted = [int(t) for t in (tag_ids if tag_ids else run.get("heads") or ())]
    if not wanted:
        wanted = [t for t, _ in bo.ready_heads(conn)]
    if not wanted:
        raise TagModelError(
            f"run {run_id} froze no head list and no tag is marked ready — "
            "name the heads explicitly")
    labels = dict(bo.ready_heads(conn, tag_ids=wanted))

    stamp = trained_at or datetime.now(timezone.utc)
    vectors = bo.arm_vectors(conn, arm_id=the_arm.id,
                             image_ids=_training_image_ids(conn, wanted))
    if not vectors:
        raise TagModelError(
            f"arm {arm!r} of run {run_id} holds no vectors for these heads — "
            "the embedding pass has not reached it")
    by_cell = _bakeoff_metrics_by_cell(conn, run_id=run_id, arm_id=the_arm.id,
                                       mode=mode)

    positives_by_tag = {
        t: {i for i, state in machine_labeling.training_rows(conn, tag_id=t)
            if state == "positive"}
        for t in wanted
    } if mode == th.MODE_POS_ONLY_FREE_NEG else {}

    # TRAIN FIRST, WRITE AFTER. A run where every head fails must leave no row
    # behind: a `candidate` with no heads would occupy its version name forever,
    # and versions are immutable by design.
    outcomes: list[PromotedHead] = []
    fitted: list[tuple[int, th.TrainedHead, dict[str, Any]]] = []
    for tag_id in sorted(wanted):
        name = labels.get(tag_id, str(tag_id))
        try:
            head = _train_one(
                conn, tag_id=tag_id, arm=the_arm, mode=mode, vectors=vectors,
                positives_by_tag=positives_by_tag, n_splits=n_splits, C=C,
                threshold=threshold, seed=seed, trained_at=stamp)
        except th.TagHeadError as exc:
            LOG.warning("TAGMODEL %s head %d (%s) NOT promoted: %s",
                        version, tag_id, name, exc)
            outcomes.append(PromotedHead(tag_id=tag_id, label=name,
                                         status="failed", note=str(exc)))
            continue
        metrics = _head_metrics(head, by_cell.get(tag_id))
        fitted.append((tag_id, head, metrics))
        outcomes.append(PromotedHead(tag_id=tag_id, label=name, status="ok",
                                     metrics=metrics))

    if not fitted:
        raise TagModelError(
            f"no head of run {run_id} arm {arm!r} mode {mode!r} could be trained "
            "— nothing to promote")

    model_id = _insert_model(
        conn, version=version, label=label, note=note, mode=mode,
        run_id=run_id, arm=the_arm, heads=[t for t, _, _ in fitted],
        dataset_hash=_model_dataset_hash(
            [h.snapshot.dataset_hash for _, h, _ in fitted]))
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_HEAD_SQL, [
            {"model_id": model_id, "tag_id": tag_id,
             "artifact": json.dumps(head.artifact, sort_keys=True),
             "metrics": json.dumps(metrics, sort_keys=True, default=str)}
            for tag_id, head, metrics in fitted])
    model = get_model(conn, model_id=model_id)
    assert model is not None
    return model, outcomes


def _model_dataset_hash(head_hashes: Sequence[str]) -> str:
    """One name for the whole model's training material: a hash over its heads'
    per-tag dataset hashes. Two promotions of the same run and arm produce the
    same string; a single relabelled image anywhere changes it."""
    return hashlib.sha256("|".join(sorted(head_hashes)).encode("utf-8")).hexdigest()


def _training_image_ids(
    conn: psycopg.Connection, tag_ids: Sequence[int],
) -> list[int]:
    """Every image any selected head trains on, read once through the one door."""
    wanted: set[int] = set()
    for tag_id in tag_ids:
        wanted |= {i for i, _ in machine_labeling.training_rows(conn, tag_id=int(tag_id))}
    return sorted(wanted)


def _train_one(
    conn: psycopg.Connection, *, tag_id: int, arm: bo.Arm, mode: str,
    vectors: dict[int, tuple[float, ...]],
    positives_by_tag: dict[int, set[int]], n_splits: int, C: float,
    threshold: float, seed: int, trained_at: datetime,
) -> th.TrainedHead:
    snapshot = th.assemble_dataset(
        conn, tag_id=tag_id, encoder=arm.encoder,
        vectors=th.mapping_vector_source(vectors))
    free: list[th.DatasetRow] = []
    if mode == th.MODE_POS_ONLY_FREE_NEG:
        free = bo.free_negatives_for(
            tag_id=tag_id, positives_by_tag=positives_by_tag,
            own_excluded=bo.excluded_image_ids(conn, tag_id=tag_id),
            groups={r.image_id: r.listing_id for r in snapshot.rows},
            vectors=vectors)
        if not free:
            raise th.TagHeadError(
                "no other promoted head contributes a usable free negative")
    return th.train_head(snapshot, trained_at=trained_at, mode=mode,
                         free_negatives=free, n_splits=n_splits, C=C,
                         threshold=threshold, seed=seed)


def _bakeoff_metrics_by_cell(
    conn: psycopg.Connection, *, run_id: int, arm_id: int, mode: str,
) -> dict[int, dict[str, Any]]:
    return {
        int(r["tag_id"]): r
        for r in bo.run_metrics(conn, run_id=run_id)
        if int(r["arm_id"]) == int(arm_id) and r["mode"] == mode
    }


def _insert_model(
    conn: psycopg.Connection, *, version: str, label: str, note: str | None,
    mode: str, run_id: int, arm: bo.Arm, heads: Sequence[int],
    dataset_hash: str | None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(_INSERT_MODEL_SQL, {
            "version": version, "label": label, "status": STATUS_CANDIDATE,
            "source_run_id": int(run_id), "source_arm": arm.arm, "mode": mode,
            "heads": [int(h) for h in heads], "dataset_hash": dataset_hash,
            "note": note, **arm.encoder.as_dict()})
        row = cur.fetchone()
    if not row:
        raise TagModelError(f"could not insert model {version!r}")
    return int(row[0])


# --- activation -------------------------------------------------------------

def activate(conn: psycopg.Connection, *, version: str) -> TagModel:
    """Make exactly one version the active one, in one transaction.

    Retire-then-activate, both statements or neither: the partial unique index in
    migration 490 refuses two active rows, so a half-applied flip would either
    leave nothing active (every consumer blind) or fail loudly mid-way. One
    transaction makes the flip atomic from a reader's point of view, which is what
    lets `score` run for hours on a candidate without anyone seeing it.
    """
    model = get_model(conn, version=version)
    if model is None:
        raise TagModelError(f"no model version {version!r}")
    if not model_heads(conn, model_id=model.id):
        raise TagModelError(
            f"version {version!r} has no heads — promote before activating")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(_RETIRE_ACTIVE_SQL, {"model_id": model.id})
            cur.execute(_ACTIVATE_SQL, {"model_id": model.id})
    out = get_model(conn, model_id=model.id)
    assert out is not None
    return out


# --- where the vectors come from -------------------------------------------

@dataclass(frozen=True)
class ScoringSource:
    """One place to score from: which images it can offer, and their vectors.

    Two exist. `bakeoff:<run_id>` reads the arm's own stored vectors — the first
    iteration scores exactly the photos the GPU pass embedded (the labelled set
    plus the sealed exam), which is what makes an iteration cheap. `production`
    reads `image_dinov3_embeddings` under the model's seven identity facts, which
    is the corpus pass a later wave runs; it works today and returns nothing,
    because nothing has populated that table for this configuration yet.
    """
    name: str
    candidates: Callable[[int | None, int], list[int]]
    vectors: th.VectorSource


def bakeoff_source(
    conn: psycopg.Connection, *, run_id: int, arm: str,
) -> ScoringSource:
    arms = bo.list_arms(conn, run_id=run_id, names=[arm])
    if not arms:
        raise TagModelError(f"run {run_id} has no arm named {arm!r}")
    arm_id = arms[0].id

    def _candidates(after: int | None, limit: int) -> list[int]:
        with conn.cursor() as cur:
            cur.execute(_BAKEOFF_IMAGE_IDS_SQL, {
                "arm_id": arm_id, "after": after, "limit": max(1, int(limit))})
            return [int(r[0]) for r in cur.fetchall()]

    def _vectors(image_ids: Sequence[int]) -> Mapping[int, Sequence[float]]:
        return bo.arm_vectors(conn, arm_id=arm_id, image_ids=image_ids)

    return ScoringSource(name=f"{SOURCE_BAKEOFF_PREFIX}:{run_id}",
                         candidates=_candidates, vectors=_vectors)


def production_source(
    conn: psycopg.Connection, *, encoder: th.EncoderIdentity,
) -> ScoringSource:
    params = encoder.as_dict()

    def _candidates(after: int | None, limit: int) -> list[int]:
        with conn.cursor() as cur:
            cur.execute(_PRODUCTION_IMAGE_IDS_SQL, {
                **params, "after": after, "limit": max(1, int(limit))})
            return [int(r[0]) for r in cur.fetchall()]

    return ScoringSource(name=SOURCE_PRODUCTION, candidates=_candidates,
                         vectors=th.encoder_vector_source(conn, encoder))


def resolve_source(
    conn: psycopg.Connection, *, model: TagModel, spec: str,
) -> ScoringSource:
    """`production`, `bakeoff` (the model's own source run) or `bakeoff:<run_id>`."""
    if spec == SOURCE_PRODUCTION:
        return production_source(conn, encoder=model.encoder)
    if spec == SOURCE_BAKEOFF_PREFIX or spec.startswith(SOURCE_BAKEOFF_PREFIX + ":"):
        _, _, tail = spec.partition(":")
        run_id = int(tail) if tail else model.source_run_id
        if run_id is None:
            raise TagModelError(
                f"model {model.version!r} records no source run — "
                "spell the run explicitly, e.g. bakeoff:1")
        if not model.source_arm:
            raise TagModelError(
                f"model {model.version!r} records no source arm to read vectors from")
        return bakeoff_source(conn, run_id=int(run_id), arm=model.source_arm)
    raise TagModelError(
        f"unknown vector source {spec!r}; expected 'production' or 'bakeoff[:run_id]'")


# --- scoring ----------------------------------------------------------------

@dataclass
class ScoreReport:
    """What one scoring pass did."""
    model_id: int
    version: str
    source: str
    considered: int = 0
    skipped_scored: int = 0
    missing_vector: int = 0
    written: int = 0
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "version": self.version,
                "source": self.source, "considered": self.considered,
                "skipped_scored": self.skipped_scored,
                "missing_vector": self.missing_vector, "written": self.written,
                "dry_run": self.dry_run}


def winner_of(scores: Mapping[int, float]) -> tuple[int, float]:
    """The argmax, ties broken toward the LOWER tag_id.

    A rule and not an accident: two heads returning the same float is common on a
    photo neither recognises, and "whichever the dict happened to yield first"
    would make the same image tag differently between two runs of the same model.
    """
    if not scores:
        raise TagModelError("a winner needs at least one head")
    best_tag, best_score = min(
        ((int(t), float(s)) for t, s in scores.items()),
        key=lambda pair: (-pair[1], pair[0]))
    return best_tag, best_score


def score_vector(
    heads: Sequence[ModelHead], embedding: Sequence[float],
) -> tuple[dict[int, float], int, float]:
    """One vector through every head: the whole score map, plus the winner."""
    scores = {
        h.tag_id: round(th.score_embedding(h.artifact, embedding), _SCORE_PLACES)
        for h in heads
    }
    tag_id, best = winner_of(scores)
    return scores, tag_id, best


def _already_scored(
    conn: psycopg.Connection, *, model_id: int, image_ids: Sequence[int],
) -> set[int]:
    out: set[int] = set()
    for i in range(0, len(image_ids), _ID_CHUNK):
        chunk = [int(x) for x in image_ids[i:i + _ID_CHUNK]]
        with conn.cursor() as cur:
            cur.execute(_SCORED_IDS_SQL, {"model_id": int(model_id),
                                          "image_ids": chunk})
            out |= {int(r[0]) for r in cur.fetchall()}
    return out


def score(
    conn: psycopg.Connection, *, model: TagModel, source: ScoringSource,
    image_ids: Sequence[int] | None = None, batch: int = DEFAULT_BATCH,
    limit: int | None = None, force: bool = False, dry_run: bool = False,
    scored_at: datetime | None = None,
) -> ScoreReport:
    """Score images under one model and upsert the winner store.

    RESUMABLE by default: an image this model has already scored is skipped, so a
    pass that dies halfway is re-runnable without cost. `force` re-scores them —
    the upsert overwrites, because the MODEL VERSION is the identity: the same
    version scoring the same photo twice must mean the same thing, and if it does
    not, the version was mutated and that is the bug.

    Inference is `tag_heads.score_embedding` — a dot product and a logistic in
    pure Python. No ML library is imported anywhere on this path.
    """
    heads = model_heads(conn, model_id=model.id)
    if not heads:
        raise TagModelError(f"model {model.version!r} has no heads to score with")
    stamp = scored_at or datetime.now(timezone.utc)
    report = ScoreReport(model_id=model.id, version=model.version,
                         source=source.name, dry_run=dry_run)
    budget = None if limit is None else max(0, int(limit))
    page = max(1, int(batch))
    after: int | None = None
    explicit = None if image_ids is None else sorted({int(i) for i in image_ids})

    while True:
        if budget is not None and budget <= 0:
            break
        if explicit is not None:
            if not explicit:
                break
            ids, explicit = explicit[:page], explicit[page:]
        else:
            ids = source.candidates(after, page)
            if not ids:
                break
            after = ids[-1]
        if budget is not None:
            ids = ids[:budget]
            budget -= len(ids)
        if not ids:
            break
        report.considered += len(ids)

        todo = ids
        if not force:
            done = _already_scored(conn, model_id=model.id, image_ids=ids)
            todo = [i for i in ids if i not in done]
            report.skipped_scored += len(ids) - len(todo)

        if not todo:
            continue
        vectors = source.vectors(todo)
        rows: list[dict[str, Any]] = []
        for image_id in todo:
            vec = vectors.get(image_id)
            if vec is None:
                # Reported, never invented: a photo with no vector under this
                # model's encoder has no opinion to record, and a zero vector
                # would be an opinion.
                report.missing_vector += 1
                continue
            scores, tag_id, best = score_vector(heads, vec)
            rows.append({
                "image_id": int(image_id), "model_id": model.id,
                "scores": json.dumps({str(k): v for k, v in sorted(scores.items())}),
                "winner_tag_id": int(tag_id), "winner_score": float(best),
                "scored_at": stamp})
        if rows and not dry_run:
            with conn.cursor() as cur:
                cur.executemany(_UPSERT_SCORE_SQL, rows)
        report.written += len(rows)
        LOG.info("TAGMODEL %s scored=%d skipped=%d missing=%d source=%s",
                 model.version, report.written, report.skipped_scored,
                 report.missing_vector, source.name)
    return report


# --- the read contract ------------------------------------------------------

@dataclass(frozen=True)
class Winner:
    """What one image is, under one model version."""
    image_id: int
    model_id: int
    version: str
    winner_tag_id: int
    winner_score: float
    scores: dict[int, float]
    scored_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id, "model_id": self.model_id,
            "version": self.version, "winner_tag_id": self.winner_tag_id,
            "winner_score": self.winner_score,
            "scores": {str(k): v for k, v in sorted(self.scores.items())},
            "scored_at": self.scored_at.isoformat() if self.scored_at else None,
        }


def winners(
    conn: psycopg.Connection, *, image_ids: Sequence[int],
    version: str | None = None,
) -> dict[int, Winner]:
    """THE READ CONTRACT for every later wave: {image_id: Winner}.

    Under the ACTIVE model unless a `version` is named. An image absent from the
    result has not been scored by that model — which is a real, reportable state
    (the corpus pass is incremental), never an implicit "no tag". Raises rather
    than returning an empty dict when no model is active: a silent nothing is how
    a consumer ships believing it read tags.
    """
    model = (get_model(conn, version=version) if version
             else active_model(conn))
    if model is None:
        raise TagModelError(
            f"no model version {version!r}" if version
            else "no tag model is active — promote and activate one first")
    ids = sorted({int(i) for i in image_ids})
    if not ids:
        return {}
    out: dict[int, Winner] = {}
    for i in range(0, len(ids), _ID_CHUNK):
        chunk = ids[i:i + _ID_CHUNK]
        with conn.cursor() as cur:
            cur.execute(_WINNERS_SQL, {"model_id": model.id, "image_ids": chunk})
            for image_id, tag_id, best, scores, stamp in cur.fetchall():
                out[int(image_id)] = Winner(
                    image_id=int(image_id), model_id=model.id,
                    version=model.version, winner_tag_id=int(tag_id),
                    winner_score=float(best),
                    scores={int(k): float(v)
                            for k, v in _as_json(scores).items()},
                    scored_at=stamp)
    return out
