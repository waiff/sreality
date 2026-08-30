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
from toolkit import tag_candidates
from toolkit import tag_definition_render as tdr
from toolkit import tag_definitions as td
from toolkit import exam_suggestions, tag_exam, tag_holdout

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


# `drawn_by` is deliberately not a request field, for the same reason `source` is
# not (migration 446): a browser that can name its own provenance can corrupt the
# record the provenance exists to protect.
class DrawCandidatesIn(BaseModel):
    count: int = tag_candidates.DEFAULT_DRAW_COUNT
    category_main: str | None = None


# `source` (migration 446) is deliberately NOT a request field on any model
# below. This router is admin-gated and human-driven, so every write through it
# is a human decision; the toolkit's default — or dedup_sim_labeling's
# confirm-vs-correct derivation — is the only thing allowed to set it. A browser
# that could name its own provenance could corrupt the very record 445 exists to
# protect. `excluded_reason` is a request field because only the operator knows
# whether a cell was genuinely ambiguous or deliberately pruned.
class ProposalStateIn(BaseModel):
    image_id: int
    model: str
    state: str
    # Set when the operator corrects a wrong suggestion before deciding —
    # the decision lands on this tag instead of the proposed one. Unset (or
    # blank) decides against the proposal's own label.
    label: str | None = None
    # Only meaningful with state='excluded'. Unset means 'ambiguous' is chosen by
    # the client; the server does not guess one.
    excluded_reason: str | None = None


class BulkProposalStateIn(BaseModel):
    model: str
    image_ids: list[int]
    state: str
    excluded_reason: str | None = None


class SetAnnotationIn(BaseModel):
    image_id: int
    state: str
    excluded_reason: str | None = None


class BulkSetAnnotationIn(BaseModel):
    image_ids: list[int]
    state: str
    excluded_reason: str | None = None


class ImageIdsIn(BaseModel):
    image_ids: list[int]


class BulkSetImageTagsIn(BaseModel):
    tag_ids: list[int]
    state: str
    excluded_reason: str | None = None


class DoesNotCountIn(BaseModel):
    case: str
    goes_to_tag_id: int | None = None


class ConfusableWithIn(BaseModel):
    tag_id: int
    tell: str


class SaveDefinitionIn(BaseModel):
    means: str
    counts: list[str] = []
    does_not_count: list[DoesNotCountIn] = []
    confusable_with: list[ConfusableWithIn] = []
    leave_out_when: str | None = None
    example_image_ids: list[int] = []
    # The version the editor loaded, null for "this tag had no definition". An
    # assertion, not a hint: a save written against a version that is no longer
    # active is refused (422) instead of reverting the newer one.
    base_version: int | None = None


def _check_state(state: str) -> None:
    if state not in tag_annotations.STATES:
        raise HTTPException(
            status_code=422,
            detail=f"state must be one of {', '.join(tag_annotations.STATES)}",
        )


def _check_excluded_reason(state: str, reason: str | None) -> None:
    """Loud at the edge (a 422 on a frontend bug); silently normalised in the
    toolkit, so the DB CHECK can never surface as a 500."""
    if reason is None:
        return
    if reason not in tag_annotations.EXCLUDED_REASONS:
        raise HTTPException(
            status_code=422,
            detail=(
                "excluded_reason must be one of "
                f"{', '.join(tag_annotations.EXCLUDED_REASONS)}"
            ),
        )
    if state != "excluded":
        raise HTTPException(
            status_code=422,
            detail="excluded_reason is only valid with state='excluded'",
        )


@router.get("/overview")
def get_overview(conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """Every tag with its tri-state counts, its candidate-queue size and how
    much of it is still open — the single call the page's coverage strip
    renders from."""
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
    _check_excluded_reason(body.state, body.excluded_reason)
    try:
        return {
            "data": dsl.set_proposal_state(
                conn, image_id=body.image_id, model=body.model,
                state=body.state, label=body.label,
                excluded_reason=body.excluded_reason,
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
    _check_excluded_reason(body.state, body.excluded_reason)
    try:
        return {
            "data": dsl.bulk_set_proposal_state(
                conn, model=body.model, image_ids=body.image_ids, state=body.state,
                excluded_reason=body.excluded_reason,
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
    _check_excluded_reason(body.state, body.excluded_reason)
    try:
        return {
            "data": tag_annotations.bulk_set_state_for_image(
                conn, image_id=image_id, tag_ids=body.tag_ids, state=body.state,
                excluded_reason=body.excluded_reason,
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
    """Tag-centric browse: this tag's candidate queue plus everything already
    decided for it, each with its state for this one tag. Backs "kitchen =
    excluded" filtering — unlike /proposals, this reaches images the model never
    proposed this tag for. Queue membership is not a label."""
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
    _check_excluded_reason(body.state, body.excluded_reason)
    try:
        return {
            "data": tag_annotations.set_state(
                conn, image_id=body.image_id, tag_id=tag_id, state=body.state,
                excluded_reason=body.excluded_reason,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/tags/{tag_id}/annotations/bulk")
def post_bulk_annotation(
    tag_id: int, body: BulkSetAnnotationIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Batch version of the direct annotation write — the labeling UI's
    main throughput lever."""
    _check_state(body.state)
    _check_excluded_reason(body.state, body.excluded_reason)
    try:
        return {
            "data": tag_annotations.bulk_set_state(
                conn, image_ids=body.image_ids, tag_id=tag_id, state=body.state,
                excluded_reason=body.excluded_reason,
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


# --- candidate retrieval (migration 450) ------------------------------------


@router.get("/tags/{tag_id}/candidates")
def get_tag_candidates(
    tag_id: int, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """This tag's review queue: how many candidates it holds, how many are still
    undecided, how they were drawn (rank band and category), and whether the tag
    has enough human-verified positives to draw more."""
    try:
        return {"data": tag_candidates.candidate_summary(conn, tag_id=tag_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"tag {tag_id} not found") from exc


@router.post("/tags/{tag_id}/candidates")
def post_tag_candidates(
    tag_id: int, body: DrawCandidatesIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Draw candidates for this tag by CLIP retrieval. A tag with too few
    human-verified positives gets a 200 with status='insufficient_positives' and
    no rows — never a silently empty pool."""
    try:
        return {
            "data": tag_candidates.draw_candidates(
                conn, tag_id=tag_id, count=body.count,
                category_main=body.category_main,
            )
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"tag {tag_id} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --- tag definitions (migration 446) ----------------------------------------


@router.get("/definitions")
def get_definition_status(conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """One row per tag that HAS an active definition — the tag list's status
    column. A tag absent from this list has no definition yet."""
    return {"data": td.list_definition_status(conn)}


@router.get("/tags/{tag_id}/definition")
def get_tag_definition(tag_id: int, conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """This tag's current definition, or a null body when it has none yet — an
    unknown TAG is a 404, a known tag with no definition is a 200."""
    try:
        return {"data": td.get_active_definition(conn, tag_id=tag_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"tag {tag_id} not found") from exc


@router.get("/tags/{tag_id}/definition/card")
def get_tag_definition_card(
    tag_id: int, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """The same definition rendered two ways: `card` is the plain-language version
    a person reads while labeling, `prompt` is the instruction sheet the vision
    model reads. Both come from one stored row, so the rule the operator follows
    and the rule the machine is given cannot drift apart.

    Null body for a tag with no definition — a card invented from nothing would be
    a labeling guide nobody wrote."""
    try:
        label = td.tag_label(conn, tag_id=tag_id)
        definition = td.get_active_definition(conn, tag_id=tag_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"tag {tag_id} not found") from exc
    if definition is None:
        return {"data": None}
    return {"data": {
        "card": tdr.render_card(definition, tag_label=label),
        "prompt": tdr.render_prompt(definition, tag_label=label),
        "definition_id": definition["id"],
        "version": definition["version"],
    }}


@router.post("/tags/{tag_id}/definition/card/preview")
def preview_tag_definition_card(
    tag_id: int, body: SaveDefinitionIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Render an UNSAVED draft as its handbook card. Writes nothing.

    There are no server-side drafts — one Save is one version — so the editor
    cannot preview what is being typed by re-reading the saved definition: it
    would always show the PREVIOUS one. The obvious alternative, a TypeScript
    copy of the renderer, would put two implementations of "what this tag means"
    in the repo and let them drift, which is the exact failure this whole
    two-renderings design exists to prevent. So the draft comes here instead and
    the ONE renderer stays authoritative.

    Referenced tag labels are resolved server-side from the draft's own ids, so
    the browser never needs its own copy of that lookup either."""
    doc = body.model_dump()
    doc["referenced_tags"] = td.referenced_tags_for(
        conn,
        does_not_count=doc["does_not_count"],
        confusable_with=doc["confusable_with"],
    )
    try:
        label = td.tag_label(conn, tag_id=tag_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"tag {tag_id} not found") from exc
    return {"data": {"card": tdr.render_card(doc, tag_label=label)}}


@router.put("/tags/{tag_id}/definition")
def put_tag_definition(
    tag_id: int, body: SaveDefinitionIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Save a new version. There are no drafts: this supersedes the version the
    editor was written against and inserts the next one, in one transaction. A
    stale base_version is a 422, not a silent overwrite."""
    try:
        return {
            "data": td.save_definition(
                conn, tag_id=tag_id, means=body.means, counts=body.counts,
                does_not_count=[i.model_dump() for i in body.does_not_count],
                confusable_with=[i.model_dump() for i in body.confusable_with],
                leave_out_when=body.leave_out_when,
                example_image_ids=body.example_image_ids,
                base_version=body.base_version,
            )
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"tag {tag_id} not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tags/{tag_id}/definition/versions")
def get_tag_definition_versions(
    tag_id: int, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Newest-first version metadata — the history dropdown."""
    try:
        return {"data": td.list_definition_versions(conn, tag_id=tag_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"tag {tag_id} not found") from exc


@router.get("/tags/{tag_id}/definition/versions/{version}")
def get_tag_definition_version(
    tag_id: int, version: int, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """One historical version, read-only."""
    try:
        return {"data": td.get_definition_version(conn, tag_id=tag_id, version=version)}
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"definition v{version} for tag {tag_id} not found",
        ) from exc


@router.get("/tags/{tag_id}/positive-images")
def get_positive_images(
    tag_id: int, limit: int = 200, order: str = "recent",
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """What this tag ACTUALLY contains — every image currently positive on it,
    read straight from image_tag_labels (not the dedup_sim-scoped sample browse
    at /tags/{tag_id}/images).

    `order='outlier_first'` sorts by cosine distance from this tag's own
    centroid, farthest first, so the mis-filed images come back first. A tag with
    too few embedded human-verified positives to have a centroid is NOT an error:
    it gets a 200 carrying the rows in the default order and `order='recent'`,
    which is the server's own verdict — the UI never re-derives it."""
    if order not in td.POSITIVE_IMAGE_ORDERS:
        raise HTTPException(
            status_code=422,
            detail=f"order must be one of {', '.join(td.POSITIVE_IMAGE_ORDERS)}",
        )
    if order == "outlier_first":
        out = td.list_positive_images_outlier_first(conn, tag_id=tag_id, limit=limit)
        return {
            "data": out["images"], "order": out["order"],
            "centroid_positives": out["centroid_positives"],
            "min_positives": out["min_positives"],
        }
    return {
        "data": td.list_positive_images(conn, tag_id=tag_id, limit=limit),
        "order": "recent", "centroid_positives": None,
        "min_positives": td.MIN_POSITIVES_FOR_CENTROID,
    }


@router.get("/tags/{tag_id}/neighbours")
def get_tag_neighbours(
    tag_id: int, limit: int = 8, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """The tags whose positives sit closest to this tag's in CLIP embedding
    space. Empty (never an error) when the tag has too few embedded positives to
    have a centroid."""
    return {"data": td.nearest_tags(conn, tag_id=tag_id, limit=limit)}


# --- the sealed exam (migrations 458 + 459) ---------------------------------
#
# The exam GRADES the probes, so this is the one surface that shows an operator a
# holdout image on purpose. The rules that make it a measurement live here rather
# than in the client:
#   * an answer is refused for any image outside the cohort, which is what stops a
#     mis-wired client writing warm-up practice into the measurement;
#   * a machine suggestion travels WITH the question since 2026-08-30 — the
#     operator's own ruling, reversing the original no-suggestion posture. The
#     honest cost: a machine-assisted sitting measures agreement with a
#     machine-ANCHORED human, not blind agreement. The mitigation is provenance
#     (tag_exam_suggestions keeps every suggestion beside the final answer, so
#     anchoring stays computable per image and per tag) plus discipline in the
#     client: a suggestion is a subtle mark, never a pre-filled verdict, and it is
#     served only when it was computed against the sitting's exact question list.


class ExamAnswerIn(BaseModel):
    image_id: int
    # Which iteration's question list this answer was composed against. The server
    # re-resolves it, so question and answer can never use different lists.
    set: str | None = None
    # Which of the routing tags apply. Every routing tag in NEITHER list becomes a
    # negative — that is what lets one answer measure precision AND recall.
    picked_tag_ids: list[int] = []
    # Left out of that head (excluded/'pruned'): the brief's rule for a subject
    # clearly present in a photo that is OF something else. Trains and grades
    # nothing on that cell.
    skipped_tag_ids: list[int] = []
    cant_tell: bool = False


def _routing_tags(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, label FROM tag_taxonomy "
            "WHERE routing_categories IS NOT NULL AND active ORDER BY id"
        )
        return [{"id": int(r[0]), "label": r[1]} for r in cur.fetchall()]


def _exam_tag_set(
    conn: Any, set_name: str | None,
) -> tuple[str, int | None, list[dict[str, Any]]]:
    """Resolve one sitting's question list.

    A named set (migration 460) is the normal path: iterations of the exam are
    named, ORDERED tag lists over the same 250 images — the array order is the
    on-screen key order, so it is preserved here, never re-sorted. A tag deleted
    since the set was written is dropped silently rather than crashing the
    sitting: its column simply is not asked.

    No name means THE FIRST SET (lowest id, i.e. the sitting that has been running
    since before sets existed), and only a database with no sets at all falls back
    to the routing derivation.

    Measured reason, not a preference: the first version defaulted to the routing
    derivation, and applying migration 460 — which flags three TRAINING tags for
    draw scoping — instantly grew the live bare-URL sitting from 8 buttons to 11,
    shifting every key under the operator's fingers mid-exam. Reverted within ~90s,
    zero stray cells written, but the lesson stands: a sitting's question list must
    never be derivable from a flag that other machinery flips for its own reasons.
    """
    if set_name is None:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM tag_exam_sets ORDER BY id LIMIT 1")
            row = cur.fetchone()
        if row is None:
            return "routing", None, _routing_tags(conn)
        set_name = str(row[0])
    with conn.cursor() as cur:
        cur.execute("SELECT id, tag_ids FROM tag_exam_sets WHERE name = %s", (set_name,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"exam set {set_name!r} not found")
        set_id = int(row[0])
        ordered = [int(x) for x in row[1]]
        cur.execute(
            "SELECT id, label FROM tag_taxonomy WHERE id = ANY(%s) AND active",
            (ordered,),
        )
        labels = {int(r[0]): r[1] for r in cur.fetchall()}
    return set_name, set_id, [
        {"id": i, "label": labels[i]} for i in ordered if i in labels
    ]


def _cohort_or_404(conn: Any, name: str) -> dict[str, Any]:
    cohort = tag_holdout.get_cohort(conn, name=name)
    if cohort is None:
        raise HTTPException(status_code=404, detail=f"cohort {name!r} not found")
    return cohort


@router.get("/exam/{cohort_name}")
def get_exam_state(
    cohort_name: str, set: str | None = None, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """The exam's tags, progress, and the next unanswered question.

    `question` is null when the sitting is finished. The tag list is served with
    the question so the buttons are the server's list, never a client copy; `set`
    names which iteration's question list to sit (migration 460)."""
    cohort = _cohort_or_404(conn, cohort_name)
    set_name, set_id, tags = _exam_tag_set(conn, set)
    tag_ids = [t["id"] for t in tags]
    question = tag_exam.next_question(conn, cohort_id=cohort["id"], tag_ids=tag_ids)
    if question is not None:
        # None = nothing worth showing (not computed, errored, or computed against
        # a different question list); [] = the machine genuinely suggests none,
        # which the client shows too. The lane that fills the store is
        # scripts/suggest_exam_answers.py.
        question["suggested_tag_ids"] = (
            exam_suggestions.suggestion_for(
                conn, cohort_id=cohort["id"], image_id=question["image_id"],
                set_id=set_id, current_tag_ids=tag_ids)
            if set_id is not None else None
        )
    return {"data": {
        "cohort": {"name": cohort["name"], "sealed": cohort["sealed_at"] is not None},
        "set": set_name,
        "tags": tags,
        "progress": tag_exam.progress(conn, cohort_id=cohort["id"], tag_ids=tag_ids),
        "question": question,
    }}


@router.get("/exam/{cohort_name}/warmup")
def get_exam_warmup(
    cohort_name: str, limit: int = 10, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    """Practice images from OUTSIDE the exam. They settle the operator's hand;
    spending real exam images on that would shrink the sample that has to grade
    everything, and answers posted for them are refused by design."""
    _cohort_or_404(conn, cohort_name)
    return {"data": tag_exam.warmup_images(conn, limit=max(0, min(limit, 25)))}


@router.post("/exam/{cohort_name}/answer")
def post_exam_answer(
    cohort_name: str, body: ExamAnswerIn, conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    cohort = _cohort_or_404(conn, cohort_name)
    _, _, tags = _exam_tag_set(conn, body.set)
    tag_ids = [t["id"] for t in tags]
    try:
        return {"data": tag_exam.record_answer(
            conn, cohort_id=cohort["id"], image_id=body.image_id,
            tag_ids=tag_ids, picked=body.picked_tag_ids,
            skipped=body.skipped_tag_ids, cant_tell=body.cant_tell,
        )}
    except KeyError as exc:
        # Not in the cohort — a warm-up image, or a stale client. Refusing is the
        # rail that keeps practice out of the measurement.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
