"""The tagging bake-off's read surface (docs/design/new-dedup/ENCODER-DECISION.md
§5.2 "Set 1"; PROGRAM.md ledger 2026-09-08 (d)).

Mounted under `/new-dedup/tagging-bakeoff/*`, admin-gated, READ-ONLY: every write
into `dedup_sim.tag_head_bakeoff_*` comes from `scripts/tag_head_bakeoff.py` (the
CPU runner) or the GPU embedding job, never from a browser. The live consumer is
the bake-off comparison page.

SIX ROUTES, and each answers a different question:

  GET /runs                     — which experiments exist, with their arms.
  GET /runs/{id}/metrics        — the whole arm x mode x head table, one payload.
  GET /runs/{id}/images    (A)  — a page of PHOTOS, each carrying what every arm
                                  and head said about it. "Show me the picture."
  GET /runs/{id}/buckets   (B)  — one head under one arm and mode, split into its
                                  four outcome buckets plus a score histogram.
                                  "Show me what it got wrong."
  GET /runs/{id}/scores    (C)  — the same cell as (B) UNBUCKETED: every photo the
                                  head scored, ranked by that score. "Show me the
                                  whole ranking."
  GET /runs/{id}/images/{image_id}
                           (D)  — one photo, every score the run gave it, across
                                  arms, modes, heads and splits. "What did each
                                  head think this photograph was?"

RESPONSE SHAPES ARE DOCUMENTED IN EACH DOCSTRING and are the contract the frontend
codes against. Two conventions run through all of them:

  * a precision / recall / f1 of `null` means NOTHING WAS PROPOSED — it is not
    zero. `graded_n` sits beside every rate for exactly this reason (§5.2: a head
    below a graded n the operator names is "not measurable here", not a number).
  * `label: null` appears only on `split: "exam"` rows and means the human
    abstained under the ratified grading rule (an explicit leave-out, a can't-tell
    row, or an untouched migration-466 declared default). Those rows carry a real
    score and a real prediction and are excluded from every bucket and every rate.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api import dependencies as deps
from toolkit import tag_head_bakeoff as bo

router = APIRouter(
    prefix="/new-dedup/tagging-bakeoff", tags=["new-dedup-tagging-bakeoff"],
    dependencies=[Depends(deps.require_admin)],
)


@router.get("/runs")
def list_runs(
    limit: int = Query(default=25, ge=1, le=100),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Every bake-off run, newest first, each with its arms.

    {"data": [{
      "id": 3, "created_at": "2026-09-08T12:00:00+00:00",
      "label": "set-1 v1", "note": null, "status": "ok"|"running"|"failed",
      "manifest_key": "bakeoff/3/manifest.json" | null,
      "heads": [12, 13, 19],            # tag ids selected when the run executed
      "min_train_positives": 0,         # RETIRED 2026-09-08: heads are selected by
                                        # tag_taxonomy's ready flag, not a count. The
                                        # field is echoed so the contract does not move;
                                        # runs before the ruling carry their old floor.
      "arms": [{
        "id": 7, "run_id": 3, "arm": "dinov3-b16@768/bf16",
        "dim": 768 | null, "status": "ok"|"pending"|"running", "note": null,
        "model": "facebook/dinov3-vitb16", "revision": "5931719e…",
        "library": "transformers", "pooling": "cls", "resolution": 768,
        "preprocessing": "resize-shortest-768-centercrop", "dtype": "bfloat16"
      }]
    }]}
    """
    runs = bo.list_runs(conn, limit=limit)
    for run in runs:
        run["arms"] = [a.as_dict() for a in bo.list_arms(conn, run_id=run["id"])]
    return {"data": runs}


@router.get("/runs/{run_id}/metrics")
def get_metrics(
    run_id: int, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Every arm x mode x head metric row for one run, in ONE payload.

    Small by construction (a dozen arms x three modes x twenty heads), so the
    page sorts and pivots it client-side rather than asking the API to.

    {"data": [{
      "arm_id": 7, "arm": "dinov3-b16@768/bf16",
      "mode": "pos_neg"|"pos_only_free_neg"|"pos_only_centroid",
      "tag_id": 19, "tag_label": "kuchyně",
      "n_pos": 231, "n_neg": 1004, "n_groups": 612,
      "cv_precision": 0.94, "cv_recall": 0.89, "cv_f1": 0.915, "cv_graded_n": 1235,
      "cv_tp": 206, "cv_fp": 13, "cv_tn": 991, "cv_fn": 25,
      "exam_precision": null, "exam_recall": null, "exam_f1": null,
      "exam_graded_n": 0, "exam_abstained_n": 250,
      "exam_tp": 0, "exam_fp": 0, "exam_tn": 0, "exam_fn": 0,
      "threshold": 0.5, "dataset_hash": "9f2c…", "status": "ok"|"failed",
      "note": "head is not part of the exam sitting's tag list" | null,
      "trained_at": "2026-09-08T12:31:04+00:00"
    }]}

    A `status: "failed"` row is a head that could not be trained or graded (its
    `note` says why, e.g. too few positive listing-groups for a grouped split);
    every metric on it is null/zero. It is a decided cell, not a missing one.
    """
    if bo.get_run(conn, run_id=run_id) is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return {"data": bo.run_metrics(conn, run_id=run_id)}


@router.get("/runs/{run_id}/images")
def get_images(
    run_id: int,
    split: str = Query(default="cv", pattern="^(cv|exam)$"),
    arms: str | None = Query(default=None,
                             description="Comma-separated arm ids; omit for all."),
    mode: str | None = None,
    tag_id: int | None = None,
    outcome: str | None = Query(default=None, pattern="^(tp|fp|fn|tn)$"),
    after_image_id: int | None = None,
    limit: int = Query(default=60, ge=1, le=200),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """VIEW A — a page of images, each with every score the filters admit.

    Paged by `after_image_id` (ascending image id, itself the unique tiebreaker:
    scores tie constantly, so ordering on them would reshuffle rows between
    pages). `outcome` requires `tag_id` — an outcome is one head's verdict.

    {"data": {
      "images": [{
        "image_id": 84211, "listing_id": 99213,
        "storage_path": "images/2026/…/3.jpg",   # feed to GET /images/{path}
        "scores": [{
          "arm_id": 7, "arm": "dinov3-b16@768/bf16", "mode": "pos_neg",
          "tag_id": 19, "tag_label": "kuchyně", "split": "cv",
          "fold": 2 | null,                      # null on the exam split
          "label": 1 | 0 | null,                 # null = the human abstained
          "score": 0.9713, "predicted": true,
          "outcome": "tp"|"fp"|"fn"|"tn"|"abstained"
        }]
      }],
      "next_after_image_id": 84211 | null        # null = last page
    }}

    A score is a probability in [0, 1] for the two logistic modes and a COSINE in
    [-1, 1] for `pos_only_centroid`. The two scales are not comparable; compare
    `outcome` and the metrics, never raw scores across modes.
    """
    arm_ids = None
    if arms:
        try:
            arm_ids = [int(a) for a in arms.split(",") if a.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=422,
                                detail="arms must be comma-separated ids") from exc
    if bo.get_run(conn, run_id=run_id) is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    try:
        return {"data": bo.run_images(
            conn, run_id=run_id, split=split, arm_ids=arm_ids, mode=mode,
            tag_id=tag_id, outcome=outcome, after_image_id=after_image_id,
            limit=limit)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/runs/{run_id}/buckets")
def get_buckets(
    run_id: int,
    arm_id: int,
    mode: str,
    tag_id: int,
    split: str = Query(default="cv", pattern="^(cv|exam)$"),
    limit: int = Query(default=24, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """VIEW B — one head under one arm and mode, as its four outcome buckets.

    Each bucket is ordered score-descending (the most confident mistake first —
    the one worth looking at), paged by `limit`/`offset` INDEPENDENTLY per bucket,
    so "next page" advances all four together.

    {"data": {
      "arm_id": 7, "mode": "pos_neg", "tag_id": 19, "split": "cv",
      "abstained_count": 0,             # exam cells the human left out; in no bucket
      "buckets": {
        "tp": {"count": 206, "tiles": [{
          "image_id": 84211, "listing_id": 99213,
          "storage_path": "images/2026/…/3.jpg",
          "score": 0.9713, "label": 1, "predicted": true, "fold": 2
        }]},
        "fp": {...}, "fn": {...}, "tn": {...}
      },
      "histogram": {
        "bins": 20, "lo": 0.0021, "hi": 0.9987,   # measured range, not assumed
        "positive": [0, 1, …],                    # 20 counts, label = 1
        "negative": [412, 88, …],                 # 20 counts, label = 0
        "abstained": [0, 0, …]                    # 20 counts, label = null
      }
    }}

    `lo`/`hi` are the observed minimum and maximum for this cell — the logistic
    modes score in [0, 1] and the centroid mode in cosine space, and a fixed axis
    would squash one of them into a corner. Bin i spans
    lo + i*(hi-lo)/20 .. lo + (i+1)*(hi-lo)/20.
    """
    if bo.get_run(conn, run_id=run_id) is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    try:
        return {"data": bo.run_buckets(
            conn, arm_id=arm_id, mode=mode, tag_id=tag_id, split=split,
            limit=limit, offset=offset)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --- views C and D --------------------------------------------------------
#
# The SQL for these two lives HERE rather than in toolkit/tag_head_bakeoff.py
# (which holds the readers for views A and B) only because that module is under
# concurrent change; it is the same table, read the same way, and belongs beside
# its siblings once both branches have landed.
#
# `_OUTCOME_SQL` is BORROWED from the toolkit rather than restated. It is the one
# expression that decides what a false positive is — the page filter, the bucket
# counts and the tiles all share it precisely so they cannot disagree — and a
# fourth copy of that CASE is how "wrongly caught" starts meaning two things.

_SCORES_TOTAL_SQL = """
    SELECT count(*)::bigint
    FROM dedup_sim.tag_head_bakeoff_scores s
    WHERE s.arm_id = %(arm_id)s AND s.mode = %(mode)s
      AND s.tag_id = %(tag_id)s AND s.split = %(split)s
"""

# Offset paging over a TOTAL order. Scores tie constantly (a centroid mode rounds
# many rows to the same cosine), and an OFFSET over an ORDER BY that ties would
# reshuffle rows between pages — one photo shown twice and another never shown.
# image_id is the unique tiebreaker that makes the order total, and a total
# order is what makes an offset a stable position: page 3 at 50 a page is the
# same 50 photos on every visit. That stability is what lets the page offer the
# training-set page's controls (a page size, "of N", a last-page jump).
_SCORES_PAGE_SQL = f"""
    SELECT s.image_id, i.listing_id, i.storage_path, s.score, s.label,
           s.predicted, s.fold, ({bo._OUTCOME_SQL}) AS outcome
    FROM dedup_sim.tag_head_bakeoff_scores s
    JOIN images i ON i.id = s.image_id
    WHERE s.arm_id = %(arm_id)s AND s.mode = %(mode)s
      AND s.tag_id = %(tag_id)s AND s.split = %(split)s
    ORDER BY s.score DESC, s.image_id DESC
    LIMIT %(limit)s OFFSET %(offset)s
"""

_IMAGE_ROW_SQL = """
    SELECT i.id, i.listing_id, i.storage_path
    FROM images i
    WHERE i.id = %(image_id)s
"""

_IMAGE_DETAIL_SQL = f"""
    SELECT s.arm_id, a.arm, a.resolution, s.mode, s.tag_id, t.label AS tag_label,
           s.split, s.fold, s.label, s.score, s.predicted,
           ({bo._OUTCOME_SQL}) AS outcome
    FROM dedup_sim.tag_head_bakeoff_scores s
    JOIN dedup_sim.tag_head_bakeoff_arms a ON a.id = s.arm_id
    LEFT JOIN tag_taxonomy t ON t.id = s.tag_id
    WHERE a.run_id = %(run_id)s AND s.image_id = %(image_id)s
    ORDER BY s.arm_id, s.mode, s.split, s.score DESC, s.tag_id
"""


@router.get("/runs/{run_id}/scores")
def get_scores(
    run_id: int,
    arm_id: int,
    mode: str,
    tag_id: int,
    split: str = Query(default="cv", pattern="^(cv|exam)$"),
    # The same page sizes as the training-set grid (50 … 10,000): a cell is at
    # most the corpus the run was given (~10k photos), and the operator's way of
    # reading a ranking is to widen the page until the whole cell is one scroll.
    limit: int = Query(default=50, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """VIEW C — every photo one head scored, highest score first, unbucketed.

    View B shows the same cell split into its four outcome buckets; this is the
    UNION of those buckets (plus the abstentions, which are in none of them) as
    one flat ranked list. The sort key is the HEAD'S RAW SCORE — F1 is a property
    of the head, not of a photo, so it cannot order photos.

    Paged by limit/offset over a TOTAL order (score desc, then image_id as the
    unique tiebreaker), so an offset names the same photos on every visit and
    `total` lets the client draw "x–y of N" and jump to the last page. Rank in
    the cell = offset + position on the page.

    {"data": {
      "arm_id": 7, "mode": "pos_neg", "tag_id": 19, "split": "cv",
      "total": 9264,                             # the whole cell, not this page
      "limit": 50, "offset": 0,
      "rows": [{
        "image_id": 84211, "listing_id": 99213,
        "storage_path": "images/2026/…/3.jpg",   # feed to GET /images/{path}
        "score": 0.9713, "label": 1 | 0 | null, "predicted": true,
        "fold": 2 | null,                        # null on the exam split
        "outcome": "tp"|"fp"|"fn"|"tn"|"abstained"
      }]
    }}

    A score is a probability in [0, 1] for the two logistic modes and a COSINE in
    [-1, 1] for `pos_only_centroid` — the ranking is meaningful within one cell,
    and the raw numbers are not comparable across modes.
    """
    if bo.get_run(conn, run_id=run_id) is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    keys = {"arm_id": arm_id, "mode": mode, "tag_id": tag_id, "split": split}
    with conn.cursor() as cur:
        cur.execute(_SCORES_TOTAL_SQL, keys)
        row = cur.fetchone()
        total = int(row[0]) if row else 0
        cur.execute(_SCORES_PAGE_SQL, {**keys, "limit": limit, "offset": offset})
        rows = cur.fetchall()
    out = [{
        "image_id": int(r[0]),
        "listing_id": None if r[1] is None else int(r[1]),
        "storage_path": r[2],
        "score": float(r[3]),
        "label": None if r[4] is None else int(r[4]),
        "predicted": bool(r[5]),
        "fold": None if r[6] is None else int(r[6]),
        "outcome": r[7],
    } for r in rows]
    return {"data": {**keys, "total": total, "limit": limit, "offset": offset, "rows": out}}


@router.get("/runs/{run_id}/images/{image_id}")
def get_image_detail(
    run_id: int, image_id: int, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """VIEW D — one photograph, and every score this run ever gave it.

    The whole cross-section for one image: every arm x mode x head x split cell
    that mentions it. Bounded by the experiment's own size (a dozen arms x three
    modes x twenty heads x two splits), so it is one payload with no paging.

    This is RAW MODEL OUTPUT, not a metric: the probability each head assigned
    this photograph, so the strongest head is the tag a winner-takes-all reading
    would give it. Ranking heads is meaningful only WITHIN one (arm, mode, split)
    — the scales differ across modes, and two arms are two different models.

    {"data": {
      "image_id": 84211, "listing_id": 99213,
      "storage_path": "images/2026/…/3.jpg",
      "scores": [{
        "arm_id": 7, "arm": "dinov3-b16@768/bf16", "resolution": 768,
        "mode": "pos_neg", "tag_id": 19, "tag_label": "kuchyně", "split": "cv",
        "fold": 2 | null, "label": 1 | 0 | null, "score": 0.9713,
        "predicted": true, "outcome": "tp"|"fp"|"fn"|"tn"|"abstained"
      }]
    }}

    404 when this run scored no such image — a run only ever sees the corpus it
    was given, so "not in this run" is a real answer, and an empty list would
    read as "every head said nothing".
    """
    if bo.get_run(conn, run_id=run_id) is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    with conn.cursor() as cur:
        cur.execute(_IMAGE_DETAIL_SQL, {"run_id": run_id, "image_id": image_id})
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(
                status_code=404, detail=f"run {run_id} scored no image {image_id}")
        cur.execute(_IMAGE_ROW_SQL, {"image_id": image_id})
        meta = cur.fetchone()
    return {"data": {
        "image_id": image_id,
        "listing_id": None if meta is None or meta[1] is None else int(meta[1]),
        "storage_path": None if meta is None else meta[2],
        "scores": [{
            "arm_id": int(r[0]), "arm": r[1],
            "resolution": None if r[2] is None else int(r[2]),
            "mode": r[3], "tag_id": int(r[4]), "tag_label": r[5], "split": r[6],
            "fold": None if r[7] is None else int(r[7]),
            "label": None if r[8] is None else int(r[8]),
            "score": float(r[9]), "predicted": bool(r[10]), "outcome": r[11],
        } for r in rows],
    }}
