"""Operator image labeling + annotation CRUD — the hand-labelled ground truth.

Four independent stores, all image-grain, all written only by the operator:
`image_training_examples` (one free-text label per image, the linear-probe training
set), `image_border_cases` (even a human isn't confident), `image_tag_annotations`
(this image's CLIP tag / render score is wrong, plus a note) and `phash_pair_notes`
(a note on one image PAIR). Data collection: nothing here decides anything.

Mounted under `/labeling/*`, admin-gated. The live consumer is the /clip-audit
labeling surface.
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import dependencies as deps

router = APIRouter(prefix="/labeling", tags=["labeling"])

TRAINING_LABEL_MAX_CHARS = 100  # image_training_examples' CHECK (migration 309)
BULK_TRAINING_LABEL_MAX = 500


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


def set_image_annotation(
    conn: psycopg.Connection,
    *,
    image_id: int,
    tag_flagged: bool = False,
    render_flagged: bool = False,
    note: str | None = None,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Upsert the operator's correction on ONE image's CLIP call (the "this tag /
    render score is wrong" flag + note, migration 308). Idempotent: a repeat call
    edits the existing row."""
    clean_note = (note or "").strip() or None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO image_tag_annotations (image_id, tag_flagged, render_flagged, "
            "  note, created_by) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (image_id) DO UPDATE SET "
            "  tag_flagged = excluded.tag_flagged, "
            "  render_flagged = excluded.render_flagged, "
            "  note = excluded.note, "
            "  updated_at = now() "
            "RETURNING tag_flagged, render_flagged, note, updated_at",
            (image_id, bool(tag_flagged), bool(render_flagged), clean_note, created_by),
        )
        r = cur.fetchone()
    return {
        "data": {
            "image_id": image_id, "tag_flagged": bool(r[0]), "render_flagged": bool(r[1]),
            "note": r[2], "updated_at": r[3],
        }
    }


def delete_image_annotation(conn: psycopg.Connection, *, image_id: int) -> dict[str, Any]:
    """Clear an image's annotation row entirely. No-op if it had none."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM image_tag_annotations WHERE image_id = %s", (image_id,),
        )
        deleted = cur.rowcount
    return {"data": {"deleted": bool(deleted)}}


def _canon_image_pair(a: int, b: int) -> tuple[int, int]:
    """Canonical (low, high) image-id pair — the phash_pair_notes key order."""
    a, b = int(a), int(b)
    return (a, b) if a < b else (b, a)


def set_phash_note(
    conn: psycopg.Connection,
    *,
    image_id_a: int,
    image_id_b: int,
    note: str | None,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Upsert the operator's note on ONE image PAIR (migration 308) — canonically
    ordered so the same two images always hit the same row, whichever way round
    the caller passes them."""
    lo, hi = _canon_image_pair(image_id_a, image_id_b)
    if lo == hi:
        raise ValueError("a phash note needs two distinct images")
    clean_note = (note or "").strip() or None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO phash_pair_notes (image_id_a, image_id_b, note, created_by) "
            "VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (image_id_a, image_id_b) DO UPDATE SET "
            "  note = excluded.note, updated_at = now() "
            "RETURNING note, updated_at",
            (lo, hi, clean_note, created_by),
        )
        r = cur.fetchone()
    return {"data": {"image_id_a": lo, "image_id_b": hi, "note": r[0], "updated_at": r[1]}}


def delete_phash_note(
    conn: psycopg.Connection, *, image_id_a: int, image_id_b: int,
) -> dict[str, Any]:
    """Clear an image-pair note. No-op if it had none."""
    lo, hi = _canon_image_pair(image_id_a, image_id_b)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM phash_pair_notes WHERE image_id_a = %s AND image_id_b = %s",
            (lo, hi),
        )
        deleted = cur.rowcount
    return {"data": {"deleted": bool(deleted)}}


def _clean_training_label(label: str) -> str:
    """Normalize a free-text training label at the write boundary (trim + collapse
    internal whitespace). Shared by the single- and bulk-write paths so the two can't
    drift into storing differently-spaced spellings of the same class.

    Length is checked here, not left to the table's CHECK, so an over-long label comes
    back as a 422 naming the problem instead of a 500 — and so one bad label can't
    abort an entire batch write."""
    clean = " ".join((label or "").split())
    if not clean:
        raise ValueError("a training example needs a non-empty label")
    if len(clean) > TRAINING_LABEL_MAX_CHARS:
        raise ValueError(f"a training label is at most {TRAINING_LABEL_MAX_CHARS} characters")
    return clean


def set_training_example(
    conn: psycopg.Connection,
    *,
    image_id: int,
    label: str,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Upsert ONE image's linear-probe training-set label — one label per image
    (migration 309), overwritten on a repeat Train click with a different label.
    `label` is free text (open-vocabulary), not constrained to the CLIP taxonomy —
    but IS normalized here (trim + collapse internal whitespace), the write boundary,
    so every reader (this table's distinct-label list feeds the training combobox's
    suggestions) sees already-clean values instead of re-deriving normalization at
    each read site."""
    clean = _clean_training_label(label)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO image_training_examples (image_id, label, created_by) "
            "VALUES (%s,%s,%s) "
            "ON CONFLICT (image_id) DO UPDATE SET "
            "  label = excluded.label, updated_at = now() "
            "RETURNING label, updated_at",
            (image_id, clean, created_by),
        )
        r = cur.fetchone()
    return {"data": {"image_id": image_id, "label": r[0], "updated_at": r[1]}}


def bulk_set_training_examples(
    conn: psycopg.Connection,
    *,
    image_ids: list[int],
    label: str,
    created_by: str = "operator",
) -> dict[str, Any]:
    """Relabel MANY images at once — the training-label browser's batch dropdown,
    where the operator reviews one label's class as a whole and moves the wrong ones
    somewhere else in one go. Same upsert semantics as set_training_example (an image
    not yet in the set gets added), just set-at-a-time.

    Ids are de-duplicated first: ON CONFLICT DO UPDATE cannot affect the same row
    twice in one statement, so a repeated id would abort the whole write."""
    clean = _clean_training_label(label)
    ids = list(dict.fromkeys(int(i) for i in image_ids))
    if not ids:
        raise ValueError("no images selected")
    if len(ids) > BULK_TRAINING_LABEL_MAX:
        raise ValueError(f"at most {BULK_TRAINING_LABEL_MAX} images per batch")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO image_training_examples (image_id, label, created_by) "
            "SELECT u, %s, %s FROM unnest(%s::bigint[]) AS u "
            "ON CONFLICT (image_id) DO UPDATE SET "
            "  label = excluded.label, updated_at = now()",
            (clean, created_by, ids),
        )
        updated = cur.rowcount
    return {"data": {"updated": updated, "label": clean, "image_ids": ids}}


def delete_training_example(conn: psycopg.Connection, *, image_id: int) -> dict[str, Any]:
    """Remove an image from the training set. No-op if it wasn't in it."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM image_training_examples WHERE image_id = %s", (image_id,),
        )
        deleted = cur.rowcount
    return {"data": {"deleted": bool(deleted)}}


def delete_training_label(conn: psycopg.Connection, *, label: str) -> dict[str, Any]:
    """Remove EVERY training example carrying one label (the label chips' trash
    affordance). Only the training assignments go; the images stay. For an
    open-vocabulary label this also retires the label itself (it existed only as
    its rows), while a taxonomy label just drops to zero coverage. Normalized through
    the same cleaner as the writes, so the delete target can't miss rows over
    whitespace spelling."""
    clean = _clean_training_label(label)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM image_training_examples WHERE label = %s", (clean,),
        )
        deleted = cur.rowcount
    return {"data": {"deleted": deleted, "label": clean}}


def set_border_case(
    conn: psycopg.Connection, *, image_id: int, created_by: str = "operator",
) -> dict[str, Any]:
    """Flag ONE image as a border case (migration 310) — even a human isn't confident
    about its room/plan classification. A separate concern from image_training_examples:
    no label required, and independent of whether the image has one. Idempotent — a
    repeat flag is a no-op, not a second row (image_id is unique)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO image_border_cases (image_id, created_by) VALUES (%s,%s) "
            "ON CONFLICT (image_id) DO NOTHING "
            "RETURNING created_at",
            (image_id, created_by),
        )
        r = cur.fetchone()
        if r is None:
            cur.execute(
                "SELECT created_at FROM image_border_cases WHERE image_id = %s", (image_id,),
            )
            r = cur.fetchone()
    return {"data": {"image_id": image_id, "created_at": r[0]}}


def delete_border_case(conn: psycopg.Connection, *, image_id: int) -> dict[str, Any]:
    """Unflag an image as a border case. No-op if it wasn't flagged."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM image_border_cases WHERE image_id = %s", (image_id,),
        )
        deleted = cur.rowcount
    return {"data": {"deleted": bool(deleted)}}


@router.post("/image-annotation")
def post_image_annotation(
    body: ImageAnnotationAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Flag one image's CLIP tag and/or render score as wrong, with a note.
    Idempotent upsert, image-grain."""
    return set_image_annotation(
        conn, image_id=body.image_id, tag_flagged=body.tag_flagged,
        render_flagged=body.render_flagged, note=body.note,
    )


@router.delete("/image-annotation")
def delete_image_annotation_route(
    image_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Clear an image's annotation."""
    return delete_image_annotation(conn, image_id=image_id)


@router.post("/phash-note")
def post_phash_note(
    body: PhashNoteAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """A note on one image pair. Idempotent upsert, image-pair-grain."""
    try:
        return set_phash_note(
            conn, image_id_a=body.image_id_a, image_id_b=body.image_id_b,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/phash-note")
def delete_phash_note_route(
    a: int,
    b: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Clear an image-pair note. `a`/`b` are the two image ids."""
    return delete_phash_note(conn, image_id_a=a, image_id_b=b)


@router.post("/training-example")
def post_training_example(
    body: TrainingExampleAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """The Train CTA: upsert one image's linear-probe training-set label."""
    try:
        return set_training_example(
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
    """Batch relabel: put MANY images under one training-set label."""
    try:
        return bulk_set_training_examples(
            conn, image_ids=body.image_ids, label=body.label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/training-example")
def delete_training_example_route(
    image_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Remove an image from the training set."""
    return delete_training_example(conn, image_id=image_id)


@router.delete("/training-examples/by-label")
def delete_training_label_route(
    label: str,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Remove every training example under one label (the label chip's trash)."""
    try:
        return delete_training_label(conn, label=label)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/border-case")
def post_border_case(
    body: BorderCaseAction,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Flag an image as a border case — even a human isn't confident about it."""
    return set_border_case(conn, image_id=body.image_id)


@router.delete("/border-case")
def delete_border_case_route(
    image_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Unflag an image as a border case."""
    return delete_border_case(conn, image_id=image_id)
