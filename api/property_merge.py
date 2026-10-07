"""Property merge MECHANICS — the operator's curation surface, not a decision engine.

Everything here starts from a merge the operator (or another caller) has already
ORDERED: collapse this explicit set of properties, or state how one property's adverts split.
Nothing in this module decides *whether* two properties are the same.

The one merge and the one undo live in `toolkit.property_identity` (`merge_property_set` /
`detach_listings` — the survivor rule, the carry record and the curation routing, browse sync,
the `property_merge_events` ledger and, for the operator's merge, the rulings of MS12); the
operator's split by letters composes them in `toolkit.property_split` (MS18). This module is
the HTTP + read layer over them. Mounted under `/properties/*`, admin-gated.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api import dependencies as deps
from api import tenant_pool
from api.category_clash_text import LETTER_ENDING, MERGE_ENDING, clash_sentence
from toolkit.property_identity import (
    CategoryClash,
    MergeError,
    adverts_on,
    listing_origins,
    merge_preview,
    merge_property_set,
    resolve_active_property_id,
)
from toolkit.property_split import (
    STALE,
    SplitRefused,
    parse_choices,
    parse_letters,
    split_preview,
    split_property,
)

router = APIRouter(prefix="/properties", tags=["properties"])


class PropertySetAction(BaseModel):
    property_ids: list[int]


class SplitChoice(BaseModel):
    """One of the acting account's items: the letter that gets it, and copies for others."""

    to: str
    copies: list[str] = Field(default_factory=list)


class SplitAction(BaseModel):
    """The split by letters (MS18): every ad of the property with its letter, the acting
    account's choices per item (an item left out follows the preselection), an optional reason
    and the preview's `plan` the click was made on. Their limits (1 to 100 ads, letters A–Z, a
    reason of at most 500 characters) are `toolkit.property_split`'s, refused in Czech."""

    letters: dict[int, str]
    choices: dict[str, SplitChoice] = Field(default_factory=dict)
    # The operator's optional free-text reason, kept on every ruling's note.
    reason: str | None = None
    expect: str = ""


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


@router.get("/merge")
def get_merge_preview(
    properties: str = Query(..., max_length=2000),
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """How many "different" rulings a merge of these properties (`?properties=12,34`) would
    take back (MS12), before the click; reads only."""
    try:
        ids = sorted({int(p) for p in properties.split(",") if p.strip()})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="properties is a comma-separated id list") from exc
    if len(ids) < 2:
        raise HTTPException(status_code=400, detail="need at least two properties")
    return merge_preview(conn, ids)


def _refused(refusal: dict[str, Any] | None) -> dict[str, Any] | None:
    """A letter's join that rule 15 refuses, in E925's Czech sentence."""
    if refusal is None:
        return None
    return {"code": "refused", "ids": refusal["ids"],
            "message": clash_sentence(refusal["field"], refusal["a"], refusal["b"],
                                      ending=LETTER_ENDING)}


@router.get("/{property_id}/split")
def get_split_plan(
    property_id: int,
    letters: str = Query(..., max_length=4000),
    choices: str | None = Query(None, max_length=8000),
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """The split's preview (`?letters=94020:A,94492:B`, every ad; `?choices=` the click's own
    choices as JSON): where each letter and each of the acting account's items would land, what
    the click would skip, the rulings it would write and take back, and `plan`, the digest the
    click sends back; reads only. A refusal is `{code, message, ids}`."""
    # the acting account's own items only (MS18); an admin with no membership has none
    account = tenant_pool.resolve_account_id(conn, claims) if claims.get("sub") else None
    try:
        preview = split_preview(conn, property_id, letters=parse_letters(letters),
                                account=account,
                                choices=parse_choices(choices) if choices else None)
    except SplitRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc
    return {**preview, "letters": [{**x, "refused": _refused(x["refused"])}
                                   for x in preview["letters"]]}


@router.post("/{property_id}/split")
def post_split(
    property_id: int,
    body: SplitAction,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """The split by letters (MS18), made true in one transaction while its preview's `plan` still
    holds: each letter but the one that keeps the property leaves (back where it came from, or
    new), a letter on two properties is joined, the acting account's items go where its choices
    say (with copies), everyone else's follow the preselection; "different" across letters. The
    answer is one receipt. A refusal is `{code, message, ids}` in Czech; nothing is written."""
    decided_by = _decider(claims)
    account = tenant_pool.resolve_account_id(conn, claims) if claims.get("sub") else None
    try:
        return split_property(
            conn, property_id, letters=body.letters, decided_by=decided_by, account=account,
            choices={item: (c.to, tuple(c.copies)) for item, c in body.choices.items()},
            reason=body.reason, expect=body.expect,
        )
    except SplitRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc
    except CategoryClash as exc:
        # a leaving letter that mixes categories rule 15 never joins (E925's sentence)
        raise HTTPException(status_code=409, detail={
            "code": "refused", "ids": list(exc.ads or ()),
            "message": clash_sentence(exc.field, exc.a, exc.b, ending=LETTER_ENDING),
        }) from exc
    except MergeError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "stale", "message": STALE, "ids": []}) from exc


@router.get("/{property_id}/origins")
def get_origins(
    property_id: int,
    conn: Any = Depends(deps.get_db_conn),
    _: dict = Depends(deps.require_admin),
) -> dict[str, Any]:
    """Each advert's origin (where a split returns it while it is still merged here) and the
    source and time of the merge that took it from there, all null when no standing merge moved
    it (the property's own advert)."""
    survivor = resolve_active_property_id(conn, property_id)
    if survivor is None:
        raise HTTPException(status_code=404, detail=f"property {property_id} not found")
    ids = adverts_on(conn, [survivor])[survivor]
    origins = listing_origins(conn, ids)
    return {"property_id": survivor, "adverts": [
        dict(zip(("listing_id", "origin_property_id", "merge_source", "merged_at"),
                 (lid, *origins.get(lid, (None, None, None)))))
        for lid in ids]}
