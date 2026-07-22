"""FastAPI routes for the cross-source dedup review surface.

Mounted under `/dedup/*`, admin-gated by `require_admin` (is_admin claim;
the legacy operator token passes during the dual-auth window) — these are
mutating operator actions (merge / dismiss / unmerge).

The transaction mechanics live in `toolkit.property_identity`; this router is a
thin HTTP layer over `api.property_dedup`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api import dependencies as deps
from api import model_compare
from api import property_dedup as dedup
from api.location_filter import parse_district_chips_csv
from toolkit.asset_identity import (
    AssetError,
    get_asset,
    link_properties,
    unlink_property,
)
from toolkit.property_identity import MergeError

router = APIRouter(prefix="/dedup", tags=["dedup"])


class ClusterAction(BaseModel):
    candidate_ids: list[int]


class PropertySetAction(BaseModel):
    property_ids: list[int]


class ModelCompareAction(BaseModel):
    # None/omitted => the oldest-undecided top-`limit` (queue-level button);
    # a list => exactly those proposed candidates (per-card button).
    candidate_ids: list[int] | None = None
    limit: int = 25


class AssetLinkAction(BaseModel):
    property_ids: list[int]
    note: str | None = None


class AssetUnlinkAction(BaseModel):
    property_id: int


class DecisionFeedbackAction(BaseModel):
    left_property_id: int
    right_property_id: int
    is_incorrect: bool = True
    expected_outcome: str | None = None  # should_merge | should_dismiss | unsure
    note: str | None = None
    category_main: str | None = None


class ImageAnnotationAction(BaseModel):
    image_id: int
    tag_flagged: bool = False
    render_flagged: bool = False
    note: str | None = None


class PhashNoteAction(BaseModel):
    image_id_a: int
    image_id_b: int
    note: str | None = None


class TrainingExampleAction(BaseModel):
    image_id: int
    label: str


class BulkTrainingExampleAction(BaseModel):
    image_ids: list[int]
    label: str


class BorderCaseAction(BaseModel):
    image_id: int


@router.get("/summary")
def get_summary(
    status: str = "proposed",
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Cumulative review backlog + breakdown by reason (drives the dashboard)."""
    return dedup.summary(conn, status=status)


@router.get("/clip-coverage")
def get_clip_coverage(
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """CLIP backfill progress (totals + priority tiers) for the /dedup tracker."""
    return dedup.clip_coverage(conn)


@router.get("/pipeline-overview")
def get_pipeline_overview(
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """The top-of-page dedup funnel: per-stage count + last-24h movement."""
    return dedup.pipeline_overview(conn)


@router.get("/pipeline-timeline")
def get_pipeline_timeline(
    bucket: str = Query(default="day", pattern="^(hour|day)$"),
    points: int | None = Query(default=None, ge=1, le=168),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Dedup-funnel throughput (tagged / candidates / merged / dismissed) per `bucket`
    ('hour' over ~2 days, or 'day' over ~2 weeks)."""
    return dedup.pipeline_timeline(conn, bucket=bucket, points=points)


@router.get("/audit")
def get_pair_audit(
    outcome: str | None = None,
    category_main: str | None = None,
    source: str | None = None,
    stage: str | None = None,
    factor: str | None = Query(
        default=None, pattern="^(phash|cosine|visual|address|floor_plan)$",
    ),
    factor_min: float | None = None,
    factor_max: float | None = None,
    verdict: str | None = Query(default=None, pattern="^(High|Medium|Low)$"),
    room_type: str | None = None,
    property_id: int | None = None,
    property_id_in: str | None = Query(
        default=None, description="CSV of property_ids — batches many properties' "
        "decisions into one call (e.g. the /clip-audit page's on-screen cards).",
    ),
    flagged: bool | None = None,
    districts: str | None = None,
    districts_ctx: str | None = None,
    districts_excl: str | None = None,
    districts_lvl: str | None = None,
    districts_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """The unified Decision history feed (merged / dismissed, engine + operator).
    Filterable by property type, outcome, source, stage, the decision FACTOR
    (`factor` + numeric `factor_min`/`factor_max`, or `verdict` for visual),
    `room_type` (the compared room/plan tag), `property_id` (the decisions that built
    one property — the listing-detail link) or its batched form `property_id_in`,
    `flagged` (only decisions the operator flagged as incorrect), and `districts`
    (the same `districts`/`districts_ctx`/`districts_excl`/`districts_lvl`/
    `districts_id` CSV shape Browse's URL uses — matches if EITHER side of the
    decision's pair touches the picked place)."""
    pids = (
        [int(x) for x in property_id_in.split(",") if x.strip()]
        if property_id_in else None
    )
    return dedup.list_pair_audit(
        conn, outcome=outcome, category_main=category_main, source=source,
        stage=stage, factor=factor, factor_min=factor_min, factor_max=factor_max,
        verdict=verdict, room_type=room_type, property_id=property_id,
        property_id_in=pids, flagged=flagged,
        districts=parse_district_chips_csv(
            districts, districts_ctx, districts_excl, districts_lvl, districts_id,
        ),
        limit=limit, offset=offset,
    )


@router.get("/decision-evidence")
def get_decision_evidence(
    a: int,
    b: int,
    stage: str | None = None,
    reason: str | None = None,
    room_type: str | None = None,
    category_main: str | None = None,
    per_side: int = Query(default=4, ge=1, le=8),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """The SPECIFIC pictures behind a decision: the pHash matched pairs, the compared
    plans, or the deciding room — resolved at read time so it works on every historical
    row. Stage/reason pick the evidence the engine's gate actually used."""
    return dedup.decision_evidence(
        conn, left_sreality_id=a, right_sreality_id=b, stage=stage, reason=reason,
        room_type=room_type, category_main=category_main, per_side=per_side,
    )


@router.post("/feedback")
def post_decision_feedback(
    body: DecisionFeedbackAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Flag a dedup decision/candidate pair as INCORRECT (with a note + the expected
    correct outcome). Property-pair-keyed, so it attaches on both the history feed and
    the queue and never orphans on a recompute; idempotent upsert."""
    try:
        return dedup.set_decision_feedback(
            conn, left_property_id=body.left_property_id,
            right_property_id=body.right_property_id, is_incorrect=body.is_incorrect,
            expected_outcome=body.expected_outcome, note=body.note,
            category_main=body.category_main,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/feedback")
def delete_decision_feedback(
    a: int,
    b: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Un-flag a pair (remove its incorrect-decision flag). `a`/`b` are the two
    property_ids of the pair."""
    return dedup.delete_decision_feedback(conn, left_property_id=a, right_property_id=b)


@router.post("/image-annotation")
def post_image_annotation(
    body: ImageAnnotationAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """/clip-audit: flag one image's CLIP tag and/or render score as wrong, with a
    note. Idempotent upsert, image-grain."""
    return dedup.set_image_annotation(
        conn, image_id=body.image_id, tag_flagged=body.tag_flagged,
        render_flagged=body.render_flagged, note=body.note,
    )


@router.delete("/image-annotation")
def delete_image_annotation(
    image_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Clear an image's annotation."""
    return dedup.delete_image_annotation(conn, image_id=image_id)


@router.post("/phash-note")
def post_phash_note(
    body: PhashNoteAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """/phash-audit: a note on one image pair. Idempotent upsert, image-pair-grain."""
    try:
        return dedup.set_phash_note(
            conn, image_id_a=body.image_id_a, image_id_b=body.image_id_b,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/phash-note")
def delete_phash_note(
    a: int,
    b: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Clear a phash-pair note. `a`/`b` are the two image ids."""
    return dedup.delete_phash_note(conn, image_id_a=a, image_id_b=b)


@router.post("/training-example")
def post_training_example(
    body: TrainingExampleAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """/phash-audit "Train": upsert one image's linear-probe training-set label."""
    try:
        return dedup.set_training_example(
            conn, image_id=body.image_id, label=body.label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/training-examples/bulk")
def post_bulk_training_examples(
    body: BulkTrainingExampleAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """/clip-audit batch relabel: put MANY images under one training-set label."""
    try:
        return dedup.bulk_set_training_examples(
            conn, image_ids=body.image_ids, label=body.label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/training-example")
def delete_training_example(
    image_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Remove an image from the training set."""
    return dedup.delete_training_example(conn, image_id=image_id)


@router.delete("/training-examples/by-label")
def delete_training_label(
    label: str,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Remove every training example under one label (the /clip-audit chip trash)."""
    try:
        return dedup.delete_training_label(conn, label=label)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/border-case")
def post_border_case(
    body: BorderCaseAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Flag an image as a border case — even a human isn't confident about it."""
    return dedup.set_border_case(conn, image_id=body.image_id)


@router.delete("/border-case")
def delete_border_case(
    image_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Unflag an image as a border case."""
    return dedup.delete_border_case(conn, image_id=image_id)


@router.get("/phash-audit")
def get_phash_audit(
    hamming_min: int = Query(default=0, ge=0, le=64),
    hamming_max: int = Query(default=15, ge=0, le=64),
    category_main: str | None = None,
    outcome: str | None = None,
    room_types: str | None = Query(
        default=None, description="CSV of CLIP logical tags — both images in a "
        "returned pair must share the SAME tag, which must be one of these.",
    ),
    training_only: bool = Query(
        default=False, description="Only pairs where at least one of the two shown "
        "images already has a linear-probe training-set label.",
    ),
    training_label: str | None = Query(
        default=None, description="Narrows training_only to one SPECIFIC label "
        "(implies training_only regardless of its literal value).",
    ),
    training_exclude: bool = Query(
        default=False, description="Inverse of training_only — pairs where NEITHER "
        "shown image is in the training set yet. Takes priority if both are set.",
    ),
    limit: int = Query(default=100, ge=1, le=200),
    scan_offset: int = Query(
        default=0, ge=0, description="Opaque cursor — pass back the previous "
        "response's next_scan_offset to continue scanning.",
    ),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """/phash-audit: matching-photo image pairs, from decisions the engine already made,
    whose live Hamming distance falls in [hamming_min, hamming_max] — evidence for
    whether the current merge bar (Hamming <= 6) could safely widen. Read-only; no
    engine/threshold change. Paginates the SCOPE in bounded chunks (see phash_audit's
    docstring) — a `data` shorter than `limit` with a null `next_scan_offset` means the
    ceiling or the true population was exhausted, not an arbitrary stop."""
    types = [t for t in room_types.split(",") if t.strip()] if room_types else None
    return dedup.phash_audit(
        conn, hamming_min=hamming_min, hamming_max=hamming_max,
        category_main=category_main, outcome=outcome, room_types=types,
        training_only=training_only, training_label=training_label,
        training_exclude=training_exclude, limit=limit, scan_offset=scan_offset,
    )


@router.get("/candidates")
def get_candidates(
    status: str | None = "proposed",
    tier: str | None = None,
    reason: str | None = None,
    verdict: str | None = None,
    category_main: str | None = None,
    districts: str | None = None,
    districts_ctx: str | None = None,
    districts_excl: str | None = None,
    districts_lvl: str | None = None,
    districts_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """`category_main` and `districts` both narrow to pairs where EITHER candidate
    property matches (the same property-type tabs Decision history uses, and the
    same `districts`/`districts_ctx`/`districts_excl`/`districts_lvl`/`districts_id`
    CSV shape Browse's URL uses) — lets the operator prioritise the manual review
    backlog by property type or location."""
    return dedup.list_candidates(
        conn, status=status, tier=tier, reason=reason, verdict=verdict,
        category_main=category_main,
        districts=parse_district_chips_csv(
            districts, districts_ctx, districts_excl, districts_lvl, districts_id,
        ),
        limit=limit, offset=offset,
    )


@router.post("/candidates/{candidate_id}/merge")
def post_merge_candidate(
    candidate_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    try:
        result = dedup.merge_candidate(conn, candidate_id)
    except MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    return result


@router.post("/candidates/bulk-merge")
def post_bulk_merge_candidates(
    body: ClusterAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Scoped bulk-approve: merge each given candidate as its own reversible pair.

    Per-pair tolerant (a conflicting pair is skipped, not fatal). The operator-facing
    /dedup surface sends the loaded STRONG candidates of one category here.
    """
    return dedup.bulk_merge_candidates(conn, body.candidate_ids)


@router.post("/candidates/archive-reset")
def post_archive_reset_candidates(
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Archive the proposed candidate queue to a backup table + clear it, so the
    engine regenerates fresh. Merges/dismissals are untouched."""
    return dedup.archive_reset_candidates(conn)


@router.post("/model-compare")
def post_model_compare(
    body: ModelCompareAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Convene all connected vision models on undecided pairs (decision support): snapshot the
    pair(s) + dispatch every model against them; verdicts land on /model-testing. `candidate_ids`
    None = the oldest-undecided top-`limit`; a list = exactly those proposed candidates."""
    return model_compare.compare_models(
        conn, candidate_ids=body.candidate_ids, limit=body.limit,
    )


@router.post("/candidates/{candidate_id}/dismiss")
def post_dismiss_candidate(
    candidate_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = dedup.dismiss_candidate(conn, candidate_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail="candidate not found or not proposed",
        )
    return result


@router.post("/clusters/merge")
def post_merge_cluster(
    body: ClusterAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Merge a cluster of candidates into one property under one reversible group."""
    try:
        result = dedup.merge_cluster(conn, body.candidate_ids)
    except MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="no candidates found")
    return result


@router.post("/properties/merge")
def post_merge_property_set(
    body: PropertySetAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Merge an explicit operator-chosen set of properties into one (subset merge)."""
    try:
        result = dedup.merge_property_set(conn, body.property_ids)
    except MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=400, detail="need at least two properties")
    return result


@router.post("/clusters/dismiss")
def post_dismiss_cluster(
    body: ClusterAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = dedup.dismiss_cluster(conn, body.candidate_ids)
    if result is None:
        raise HTTPException(
            status_code=404, detail="no proposed candidates found",
        )
    return result


@router.get("/merges")
def get_merges(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    return dedup.list_merges(conn, limit=limit, offset=offset)


@router.post("/merges/{merge_group_id}/unmerge")
def post_unmerge(
    merge_group_id: str,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    try:
        return dedup.unmerge(conn, merge_group_id, undone_by="operator")
    except MergeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ----- asset links (same physical building, kept as separate cohorts) -------
# Unlike a merge these never collapse properties — both category facets survive.
# It is the surface for the cross-category sameness merge_properties refuses.


@router.post("/assets/link")
def post_asset_link(
    body: AssetLinkAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Link the chosen properties into one asset (same building)."""
    try:
        return link_properties(
            conn, property_ids=body.property_ids, source="operator",
            reason="manual_link", note=body.note, created_by="operator",
        )
    except AssetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/assets/unlink")
def post_asset_unlink(
    body: AssetUnlinkAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Remove one property from its asset (dissolves the asset if <2 remain)."""
    try:
        return unlink_property(
            conn, property_id=body.property_id, reason="manual_unlink",
            created_by="operator",
        )
    except AssetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/assets/{asset_id}")
def get_asset_route(
    asset_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    result = get_asset(conn, asset_id)
    if result is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return result
