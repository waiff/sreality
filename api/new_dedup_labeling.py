"""NEW DEDUP Labeling program (docs/design/new-dedup/PROGRAM.md, Wave 1;
docs/design/tag-annotation-matrix.md, Wave A/B) — tag taxonomy CRUD, sample
management, and the tri-state (positive/negative/excluded) annotation matrix
over `toolkit/tag_annotations.py` and `toolkit/dedup_sim_labeling.py`.

Mounted under `/new-dedup/labeling/*`, admin-gated. The live consumer is the
Labeling page (`frontend/src/pages/NewDedupLabeling.tsx`) — tag-centric batch
review by default, with an image-centric detail view for the multi-tag-on-
one-photo case. Separate module from `api/routes/new_dedup.py` (settings
CRUD) — same split as `api/labeling.py` (border cases) vs this file.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import dependencies as deps
from toolkit import dedup_sim_labeling as dsl
from toolkit import tag_annotations

router = APIRouter(
    prefix="/new-dedup/labeling", tags=["new-dedup-labeling"],
    dependencies=[Depends(deps.require_admin)],
)


class AddTagIn(BaseModel):
    label: str
    family: str | None = None


class RenameTagIn(BaseModel):
    label: str


class TagFlagsIn(BaseModel):
    priority: bool | None = None
    ready_for_training: bool | None = None


class GrowSampleIn(BaseModel):
    count: int
    category_main: str | None = None


class ProposalStateIn(BaseModel):
    image_id: int
    model: str
    state: str
    # Set when the operator corrects a wrong suggestion before deciding —
    # the decision lands on this tag instead of the proposed one. Unset (or
    # blank) decides against the proposal's own label.
    label: str | None = None


class BulkProposalStateIn(BaseModel):
    model: str
    image_ids: list[int]
    state: str


class SetAnnotationIn(BaseModel):
    image_id: int
    state: str


class BulkSetAnnotationIn(BaseModel):
    image_ids: list[int]
    state: str


class ImageIdsIn(BaseModel):
    image_ids: list[int]


class BulkSetImageTagsIn(BaseModel):
    tag_ids: list[int]
    state: str


def _check_state(state: str) -> None:
    if state not in tag_annotations.STATES:
        raise HTTPException(
            status_code=422,
            detail=f"state must be one of {', '.join(tag_annotations.STATES)}",
        )


@router.get("/overview")
def get_overview(conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """Every tag with its tri-state counts, plus the current sample size —
    the single call the page's coverage strip renders from."""
    return {"data": tag_annotations.tag_overview(conn)}


@router.post("/taxonomy")
def post_tag(body: AddTagIn, conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """Add one tag to the vocabulary."""
    try:
        return {"data": tag_annotations.add_tag(conn, label=body.label, family=body.family)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/taxonomy/{tag_id}")
def put_tag(
    tag_id: int, body: RenameTagIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Rename a tag. image_tag_labels rows reference tag_id, not label text,
    so this is a single-row update — no cascade rewrite."""
    try:
        return {"data": tag_annotations.rename_tag(conn, tag_id=tag_id, new_label=body.label)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"tag {tag_id} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/taxonomy/{tag_id}")
def delete_tag(tag_id: int, conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """Remove a tag — its annotations go with it; the images themselves are
    untouched."""
    try:
        return {"data": tag_annotations.remove_tag(conn, tag_id=tag_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"tag {tag_id} not found") from exc


@router.patch("/taxonomy/{tag_id}/flags")
def patch_tag_flags(
    tag_id: int, body: TagFlagsIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Set one or both operator flags (priority, ready_for_training) on a
    tag — only the fields actually sent."""
    try:
        return {
            "data": tag_annotations.set_tag_flags(
                conn, tag_id=tag_id, priority=body.priority,
                ready_for_training=body.ready_for_training,
            )
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"tag {tag_id} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sample/grow")
def post_grow_sample(
    body: GrowSampleIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Add up to `count` newest not-yet-sampled images to the relabel
    sample, optionally scoped to one property type."""
    try:
        return {
            "data": dsl.grow_sample(
                conn, count=body.count, category_main=body.category_main,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/proposals")
def get_proposals(
    status: str | None = None, label: str | None = None, original_tag: str | None = None,
    limit: int = 100, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """List proposals — the machine-suggestion queue — optionally filtered
    by status ('all' / 'pending' / 'confirmed' / 'dismissed'), by the
    Taxonomy v1 `label`, and/or by the production CLIP tagger's own
    `original_tag` (a different, fixed vocabulary — see list_original_tags).
    An unknown status or original_tag is a 422 rather than a silent
    unfiltered listing."""
    if status is not None and status not in dsl.LIST_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {', '.join(dsl.LIST_STATUSES)}",
        )
    if original_tag is not None and original_tag not in dsl.list_original_tags():
        raise HTTPException(status_code=422, detail=f"unknown original_tag {original_tag!r}")
    return {
        "data": dsl.list_proposals(
            conn, status=status, label=label, original_tag=original_tag, limit=limit,
        )
    }


@router.get("/original-tags")
def get_original_tags() -> dict[str, Any]:
    """The production CLIP tagger's fixed fine-tag vocabulary — the option
    list for the "Original tag" view's own tag filter."""
    return {"data": dsl.list_original_tags()}


@router.post("/proposals/state")
def post_proposal_state(
    body: ProposalStateIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Record the operator's tri-state verdict on one proposal: positive
    writes it into image_tag_labels and marks the proposal confirmed;
    negative/excluded do the same with that state and mark it dismissed."""
    _check_state(body.state)
    try:
        return {
            "data": dsl.set_proposal_state(
                conn, image_id=body.image_id, model=body.model,
                state=body.state, label=body.label,
            )
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="proposal not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/proposals/bulk-state")
def post_bulk_proposal_state(
    body: BulkProposalStateIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Batch version of /proposals/state — the review queue's "take the
    whole batch" action."""
    _check_state(body.state)
    try:
        return {
            "data": dsl.bulk_set_proposal_state(
                conn, model=body.model, image_ids=body.image_ids, state=body.state,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/images/{image_id}/tags")
def get_tags_for_image(image_id: int, conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """Image-centric view for the detail panel: every active tag with this
    image's current state, grouped by family."""
    return {"data": tag_annotations.list_tags_for_image(conn, image_id=image_id)}


@router.post("/images/{image_id}/tags/bulk")
def post_bulk_set_image_tags(
    image_id: int, body: BulkSetImageTagsIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Set many tags on ONE image to the same state at once — the detail
    panel's "set selected" action, the mirror of a tag-scoped bulk annotate."""
    _check_state(body.state)
    try:
        return {
            "data": tag_annotations.bulk_set_state_for_image(
                conn, image_id=image_id, tag_ids=body.tag_ids, state=body.state,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/images/tags/batch")
def post_positive_tags_for_images(
    body: ImageIdsIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Every positive tag on each of several images, one query for the whole
    visible grid — the "what's already assigned" line under each tile."""
    try:
        return {
            "data": tag_annotations.list_positive_tags_for_images(
                conn, image_ids=body.image_ids,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tags/{tag_id}/images")
def get_images_for_tag(
    tag_id: int, state: str | None = None, limit: int = 100,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Tag-centric browse: every image in the labeling sample with its
    current state for this one tag. Backs "kitchen = excluded" filtering —
    unlike /proposals, this reaches images the model never proposed this
    tag for."""
    try:
        return {
            "data": tag_annotations.list_images_for_tag(
                conn, tag_id=tag_id, state=state, limit=limit,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/tags/{tag_id}/annotations")
def post_annotation(
    tag_id: int, body: SetAnnotationIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Set one (image, tag) cell directly — the image-centric detail view's
    write path, and the tag-centric grid's write path when a tile has no
    backing proposal."""
    _check_state(body.state)
    return {
        "data": tag_annotations.set_state(
            conn, image_id=body.image_id, tag_id=tag_id, state=body.state,
        )
    }


@router.post("/tags/{tag_id}/annotations/bulk")
def post_bulk_annotation(
    tag_id: int, body: BulkSetAnnotationIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Batch version of the direct annotation write — the labeling UI's
    main throughput lever."""
    _check_state(body.state)
    try:
        return {
            "data": tag_annotations.bulk_set_state(
                conn, image_ids=body.image_ids, tag_id=tag_id, state=body.state,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/tags/{tag_id}/annotations/{image_id}")
def delete_annotation(
    tag_id: int, image_id: int, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Revert one cell to untouched."""
    return {"data": tag_annotations.clear_state(conn, image_id=image_id, tag_id=tag_id)}
