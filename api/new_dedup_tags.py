"""The tag model's read surface (docs/design/new-dedup/PROGRAM.md ledger
2026-09-09 (b); migration 490).

Mounted under `/new-dedup/tags/*`, admin-gated, READ-ONLY: every write into
`tag_head_models` / `tag_head_model_heads` / `image_tag_scores` comes from
`scripts/tag_model.py` (promote / score / activate), never from a browser.

TWO ROUTES, and each answers a different question:

  GET /models             — which model versions exist, which one is ACTIVE, how
                            many heads each carries and how many images it has
                            scored. "What could we be tagging with?"
  GET /images/{image_id}  — one photo's whole score map and its winner under the
                            active model (or a named `version`). "What does the
                            model think this photo is?"

THE ONE CONVENTION THAT RUNS THROUGH BOTH (operator ruling 2026-09-09): the tag
is the ARGMAX over that model's heads, ties broken toward the lower tag_id, and
there is no per-head yes/no anywhere. `scores` therefore carries every head's
probability and no boolean, and no threshold is applied on the way out — a
consumer that wants a minimum confidence applies its own floor to `winner_score`.
An image that has not been scored by that version is a 404, not an empty tag: the
store is filled incrementally and "not scored yet" and "no tag" are different
facts.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api import dependencies as deps
from toolkit import tag_models as tm

router = APIRouter(
    prefix="/new-dedup/tags", tags=["new-dedup-tags"],
    dependencies=[Depends(deps.require_admin)],
)


@router.get("/models")
def list_models(
    limit: int = Query(default=50, ge=1, le=200),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Every tag model version, newest first, plus which one is active.

    {"data": {
      "active_version": "v1" | null,
      "models": [{
        "id": 1, "version": "v1", "label": "run 1, dinov2-l14 @504",
        "status": "candidate"|"active"|"retired",
        "mode": "pos_neg",
        "heads": [11, 12, 19],           # the tag ids the winner is an argmax over
        "n_heads": 11,                   # rows in tag_head_model_heads
        "n_scored": 9514,                # rows in image_tag_scores for this model
        "source_run_id": 1, "source_arm": "dinov2-l14-reg@504/bf16",
        "dataset_hash": "3c9f…", "note": null,
        "created_at": "2026-09-09T10:00:00+00:00",
        "activated_at": "2026-09-09T12:00:00+00:00" | null,
        "model": "facebook/dinov2-large", "revision": "…", "library": "transformers",
        "pooling": "cls", "resolution": 504, "preprocessing": "letterbox_pad",
        "dtype": "bfloat16"
      }]
    }}

    `status` is the whole lifecycle: a `candidate` has weights and may be
    half-scored — nothing reads it; exactly one row can be `active`; a version
    that was active and was replaced is `retired`. `n_scored` beside `n_heads` is
    how the operator sees whether a candidate is ready to activate.
    """
    models = tm.list_models(conn, limit=limit)
    active = next((m for m in models if m.status == tm.STATUS_ACTIVE), None)
    if active is None:
        # A model outside the page window can still be the active one.
        active = tm.active_model(conn)
    return {"data": {
        "active_version": active.version if active else None,
        "models": [m.as_dict() for m in models],
    }}


@router.get("/models/{version}/heads")
def model_heads(
    version: str, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """One version's heads, with the numbers COPIED onto them at promotion.

    {"data": {"version": "v1", "heads": [{
      "tag_id": 19, "tag_label": "kuchyně",
      "threshold": 0.5,                # what the bake-off measured it at; NOT a
                                       # gate — the product applies no per-head cut
      "kind": "tag_head_binary_logreg", "dimension": 1024,
      "metrics": {"cv": {...}, "bakeoff": {...}, "n_positive": 231, ...}
    }]}}

    The artifact's weights are deliberately NOT returned: they are megabytes of
    floats no page can use, and scoring happens server-side.
    """
    model = tm.get_model(conn, version=version)
    if model is None:
        raise HTTPException(status_code=404, detail=f"no tag model {version!r}")
    return {"data": {"version": model.version,
                     "heads": [h.as_dict()
                               for h in tm.model_heads(conn, model_id=model.id)]}}


@router.get("/images/{image_id}")
def image_scores(
    image_id: int,
    version: str | None = Query(default=None,
                                description="Omit for the ACTIVE model."),
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """One image's scores and winner under the active model (or a named version).

    {"data": {
      "image_id": 84211, "model_id": 1, "version": "v1",
      "winner_tag_id": 19, "winner_score": 0.9713,
      "scores": {"11": 0.0021, "12": 0.4410, "19": 0.9713},  # EVERY head
      "scored_at": "2026-09-09T11:02:00+00:00"
    }}

    404 when no model is active (nothing to read with), when the named version
    does not exist, or when this version has not scored this image yet — the last
    of those is a real state, because the store is filled incrementally.
    """
    try:
        found = tm.winners(conn, image_ids=[image_id], version=version)
    except tm.TagModelError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    winner = found.get(int(image_id))
    if winner is None:
        raise HTTPException(
            status_code=404,
            detail=f"image {image_id} has not been scored by "
                   f"{version or 'the active'} model")
    return {"data": winner.as_dict()}
