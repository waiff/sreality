"""NEW DEDUP Labeling program (docs/design/new-dedup/PROGRAM.md, Wave 1) —
taxonomy CRUD, sample management, and proposal review over
`toolkit/dedup_sim_labeling.py`.

Mounted under `/new-dedup/labeling/*`, admin-gated. The live consumer is the
Labeling page (a ClipAudit clone minus the dedup block, plus a "new tag vs
original tag" toggle, sample management, and tag add/rename/remove + batch
tooling). Separate module from `api/routes/new_dedup.py` (settings CRUD) —
same split as `api/labeling.py` vs `api/property_merge.py`: one file per
concern.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import dependencies as deps
from toolkit import dedup_sim_labeling as dsl

router = APIRouter(
    prefix="/new-dedup/labeling", tags=["new-dedup-labeling"],
    dependencies=[Depends(deps.require_admin)],
)


class AddTaxonomyLabelIn(BaseModel):
    label: str
    family: str | None = None


class RenameTaxonomyLabelIn(BaseModel):
    label: str


class GrowSampleIn(BaseModel):
    count: int
    category_main: str | None = None


class ProposalActionIn(BaseModel):
    image_id: int
    model: str


class BulkProposalActionIn(BaseModel):
    model: str
    image_ids: list[int]


@router.get("/overview")
def get_overview(conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """Every taxonomy label with confirmed/pending/dismissed counts, plus
    the current sample size — the single call the page's coverage strip
    renders from."""
    return {"data": dsl.taxonomy_overview(conn)}


@router.post("/taxonomy")
def post_taxonomy_label(
    body: AddTaxonomyLabelIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Add one label to the Taxonomy v1 vocabulary."""
    try:
        return {"data": dsl.add_taxonomy_label(conn, label=body.label, family=body.family)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/taxonomy/{label_id}")
def put_taxonomy_label(
    label_id: int, body: RenameTaxonomyLabelIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Rename a taxonomy label — cascades to every training example and
    proposal currently carrying the old text."""
    try:
        return {"data": dsl.rename_taxonomy_label(conn, label_id=label_id, new_label=body.label)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"taxonomy label {label_id} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/taxonomy/{label_id}")
def delete_taxonomy_label(
    label_id: int, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Remove a taxonomy label — its training examples and proposals go
    with it; the images themselves are untouched."""
    try:
        return {"data": dsl.remove_taxonomy_label(conn, label_id=label_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"taxonomy label {label_id} not found") from exc


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
    status: str | None = None, label: str | None = None, limit: int = 100,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """List proposals, optionally filtered by status ('pending' /
    'confirmed' / 'dismissed') and/or label."""
    return {"data": dsl.list_proposals(conn, status=status, label=label, limit=limit)}


@router.post("/proposals/confirm")
def post_confirm_proposal(
    body: ProposalActionIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Accept a proposal into the confirmed training set
    (image_training_examples)."""
    try:
        return {"data": dsl.confirm_proposal(conn, image_id=body.image_id, model=body.model)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="proposal not found") from exc


@router.post("/proposals/dismiss")
def post_dismiss_proposal(
    body: ProposalActionIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Reject a proposal."""
    try:
        return {"data": dsl.dismiss_proposal(conn, image_id=body.image_id, model=body.model)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="proposal not found") from exc


@router.post("/proposals/bulk-confirm")
def post_bulk_confirm_proposals(
    body: BulkProposalActionIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Accept many pending proposals for one model at once — the review
    queue's batch action."""
    try:
        return {
            "data": dsl.bulk_confirm_proposals(
                conn, model=body.model, image_ids=body.image_ids,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/proposals/bulk-dismiss")
def post_bulk_dismiss_proposals(
    body: BulkProposalActionIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Reject many pending proposals for one model at once."""
    try:
        return {
            "data": dsl.bulk_dismiss_proposals(
                conn, model=body.model, image_ids=body.image_ids,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
