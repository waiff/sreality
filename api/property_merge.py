"""Property merge MECHANICS — the operator's curation surface, not a decision engine.

Everything here starts from a merge the operator (or another caller) has already
ORDERED: collapse this explicit set of properties, or state how one property's adverts split.
Nothing in this module decides *whether* two properties are the same.

The one merge and the one undo live in `toolkit.property_identity` (`merge_property_set` /
`detach_listings` — the survivor rule, operator state, pipeline
reconcile, browse sync, the `property_merge_events` ledger and, for the operator, the
rulings of decision 8); the operator's split statement composes them in
`toolkit.property_split` (E919). This module is
the HTTP + read layer over them. Mounted under `/properties/*`, admin-gated.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import dependencies as deps
from api import tenant_pool
from api.category_clash_text import LETTER_ENDING, MERGE_ENDING, clash_sentence
from toolkit.property_identity import (
    MOVED,
    CategoryClash,
    MergeError,
    detach_outcomes,
    listing_origins,
    merge_property_set,
    resolve_active_property_id,
)
from toolkit.property_split import (
    MAX_ADVERTS,
    MAX_SEPARATED,
    REASON_MAX,
    SplitRefused,
    split_property,
    undo_split,
)

router = APIRouter(prefix="/properties", tags=["properties"])


class PropertySetAction(BaseModel):
    property_ids: list[int]


class SplitUndoVeto(BaseModel):
    """The must-not-link row a pair had before the split."""

    source: Literal["guard", "model", "llm", "operator"]
    reason: str | None = None


class SplitUndoRuling(BaseModel):
    listing_lo: int
    listing_hi: int
    verdict: str | None = None
    note: str | None = None
    reasons: list[str] = Field(default_factory=list)
    must_not_link: SplitUndoVeto | None = None


class SplitUndo(BaseModel):
    """The undo a split's response issued, posted back verbatim."""

    call_id: str
    placements: dict[int, int] = Field(default_factory=dict, max_length=MAX_ADVERTS)
    rulings: list[SplitUndoRuling] = Field(
        default_factory=list, max_length=MAX_ADVERTS * (MAX_ADVERTS - 1) // 2)


class SplitAction(BaseModel):
    """A statement (`adverts` shown, `separate` units, `keep_together`) or, alone, an `undo`."""

    adverts: list[int] | None = Field(default=None, max_length=MAX_ADVERTS)
    separate: list[Annotated[list[int], Field(max_length=MAX_ADVERTS)]] = Field(
        default_factory=list, max_length=MAX_SEPARATED)
    keep_together: bool | None = None
    # The operator's optional free-text reason (decision 8), kept on every ruling's note.
    reason: str | None = Field(default=None, max_length=REASON_MAX)
    confirm_retract: bool = False
    undo: SplitUndo | None = None


def _decider(claims: dict) -> str:
    decided_by = claims.get("email") or claims.get("sub")
    if not decided_by:
        raise HTTPException(status_code=403, detail="the admin identity carries no email")
    return str(decided_by)


# The acting account's moved items of one merge (MS15): notes counted, the rest named. A folded
# item did not move; another account's rows never leave SQL. The service role reads here, so the
# account predicate is the gate (tenancy shape 3).
_RECEIPT_SQL = """
SELECT c.table_name, count(*),
       array_remove(array_agg(DISTINCT coalesce(co.name, t.name, ps.label)), NULL)
FROM property_merge_carries c
LEFT JOIN collections co ON c.table_name = 'collection_properties' AND co.id = c.row_key
LEFT JOIN tags t ON c.table_name = 'property_tags' AND t.id = c.row_key
LEFT JOIN property_pipeline pp ON c.table_name = 'property_pipeline'
     AND pp.account_id = c.account_id AND pp.property_id = c.to_property_id
LEFT JOIN pipeline_stages ps ON ps.id = pp.stage_id
WHERE c.merge_group_id = %(group)s::uuid AND c.account_id = %(account)s
  AND c.kind = 'moved' AND c.table_name <> 'property_dismissals'
GROUP BY c.table_name
"""

# MS13: the surviving property is hidden from the acting account.
_HIDDEN_SQL = """
SELECT EXISTS (SELECT 1 FROM property_dismissals WHERE property_id = %(survivor)s
               AND account_id = %(account)s AND lifted_at IS NULL)
"""

_RECEIPT_KEYS = {"property_notes": "notes", "property_pipeline": "pipeline",
                 "collection_properties": "collections", "property_tags": "tags"}


def merge_receipt(conn: psycopg.Connection, merged: dict[str, Any],
                  account: UUID | None) -> dict[str, Any]:
    """What one merge moved of `account`'s own (MS15: notes as a count, the pipeline stage,
    collections and tags by name) and whether the survivor is hidden from it (MS13); nothing
    without an account. Read after the merge commits."""
    carried: dict[str, Any] = {"notes": 0, "pipeline": None, "collections": [], "tags": []}
    if account is None:
        return {"carried": carried, "hidden_for_you": False}
    with conn.cursor() as cur:
        cur.execute(_RECEIPT_SQL, {"group": merged["merge_group_id"], "account": account})
        for table, count, names in cur.fetchall():
            key = _RECEIPT_KEYS[table]
            if key == "notes":
                carried[key] = int(count)
            elif key == "pipeline":
                carried[key] = names[0] if names else None
            else:
                carried[key] = list(names)
        cur.execute(_HIDDEN_SQL, {"survivor": merged["survivor_id"], "account": account})
        hidden = bool(cur.fetchone()[0])
    return {"carried": carried, "hidden_for_you": hidden}


@router.post("/merge")
def post_merge_property_set(
    body: PropertySetAction,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Merge an explicit operator-chosen set of properties into its oldest record; the answer
    adds the acting account's receipt (`merge_receipt`: `carried`, `hidden_for_you`), both null
    when that read fails after the merge has committed. A category clash over the set's ads is
    a 409 `{code, message, ids}` in Czech, naming the two properties."""
    if len(set(body.property_ids)) < 2:
        raise HTTPException(status_code=400, detail="need at least two properties")
    account = tenant_pool.resolve_account_id(conn, claims) if claims.get("sub") else None
    try:
        merged = merge_property_set(
            conn, body.property_ids, source="operator", reason="manual_subset",
            decided_by=_decider(claims),
        )["data"]
    except CategoryClash as exc:
        raise HTTPException(status_code=409, detail={
            "code": "refused", "ids": list(exc.properties or ()),
            "message": clash_sentence(exc.field, exc.a, exc.b, ending=MERGE_ENDING),
        }) from exc
    except MergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        return {**merged, **merge_receipt(conn, merged, account)}
    except psycopg.Error:
        return {**merged, "carried": None, "hidden_for_you": None}


@router.post("/{property_id}/split")
def post_split(
    property_id: int,
    body: SplitAction,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """The operator's partition of one property's adverts, made true in one transaction (E919):
    each `separate` unit leaves as one record (back where it came from, or new), the rest stay,
    ruled one property when `keep_together`; `different` + a must-not-link across units. The
    property page's row split is `separate: [[id]], keep_together: false`. `{undo}` alone takes
    back the split whose response issued it. A refusal is `{code, message, ids}`; nothing is
    written."""
    decided_by = _decider(claims)
    try:
        if body.undo is not None:
            if body.model_fields_set - {"undo"}:
                raise HTTPException(status_code=400, detail={
                    "code": "invalid", "message": "an undo is sent alone", "ids": []})
            return undo_split(
                conn, property_id, call_id=body.undo.call_id, placements=body.undo.placements,
                rulings=[r.model_dump() for r in body.undo.rulings], decided_by=decided_by,
            )
        if body.adverts is None or body.keep_together is None:
            raise HTTPException(status_code=400, detail={
                "code": "invalid", "message": "a statement names adverts and keep_together",
                "ids": []})
        return split_property(
            conn, property_id, adverts=body.adverts, separate=body.separate,
            keep_together=body.keep_together, decided_by=decided_by, reason=body.reason,
            confirm_retract=body.confirm_retract,
        )
    except SplitRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc
    except CategoryClash as exc:
        # a leaving letter that mixes categories rule 15 never joins (E925's sentence)
        raise HTTPException(status_code=409, detail={
            "code": "refused", "ids": [],
            "message": clash_sentence(exc.field, exc.a, exc.b, ending=LETTER_ENDING),
        }) from exc
    except MergeError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "refused", "message": str(exc), "ids": []}) from exc


@router.get("/{property_id}/origins")
def get_origins(
    property_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Each advert's origin (where a split returns it) and the source and time of the merge
    that took it from there, all null when no standing merge moved it; what a detach would
    answer now (`detach_outcomes`), and `splittable` = that moves it."""
    survivor = resolve_active_property_id(conn, property_id)
    if survivor is None:
        raise HTTPException(status_code=404, detail=f"property {property_id} not found")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM listings WHERE property_id = %s", (survivor,))
        ids = sorted(int(r[0]) for r in cur.fetchall())
    origins, outcomes = listing_origins(conn, ids), detach_outcomes(conn, ids)
    return {"property_id": survivor, "adverts": [
        dict(zip(("listing_id", "origin_property_id", "merge_source", "merged_at",
                  "detach_outcome", "splittable"),
                 (lid, *origins.get(lid, (None, None, None)), outcomes.get(lid),
                  outcomes.get(lid) in MOVED)))
        for lid in ids]}
