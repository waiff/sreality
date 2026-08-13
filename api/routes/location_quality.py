"""Location-quality dashboard + labelled samples + operator corrections
(location program W1v — the FIRST consumer of the location serving projection).

Mounted under `/location/*`, admin-gated at the router (single-operator
diagnostic surface). Reads go through `toolkit/location_quality.py` /
`toolkit/location_labels.py` on the service-role connection — the location
tables are RLS-on with anon/authenticated revoked, so this API is the only
path the SPA has.

The corrections POST is a WRITE EXCEPTION in the toolkit sense: it appends an
operator claim (`location_data/operator_corrections.py`, S7 rank 1) and then
resolves the listing synchronously so the response already carries the
refreshed projection — 05 5.5.5 read-your-writes. If the synchronous resolve
fails, the unconditional dirty enqueue guarantees the */15 drain converges;
the response says which happened.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import dependencies as deps
from toolkit import location_labels, location_quality

router = APIRouter(
    prefix="/location", tags=["location"], dependencies=[Depends(deps.require_admin)]
)

_KNOWN_SOURCES = frozenset({
    "sreality", "bazos", "bezrealitky", "idnes", "mmreality", "remax",
    "ceskereality", "realitymix", "maxima",
})


def _check_source(source: str) -> str:
    if source not in _KNOWN_SOURCES:
        raise HTTPException(status_code=404, detail=f"unknown source {source!r}")
    return source


@router.get("/quality/summary")
def quality_summary(conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    return location_quality.corpus_summary(conn)


@router.get("/quality/source/{source}")
def quality_source(source: str, conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    return location_quality.source_overview(conn, _check_source(source))


@router.get("/quality/w1v-gate")
def quality_w1v_gate(conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    return location_quality.w1v_gate(conn)


@router.get("/listing/{listing_id}")
def listing_inspector(listing_id: int, conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    result = location_quality.listing_inspector(conn, listing_id=listing_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"listing {listing_id} not found")
    return result


@router.get("/listing/by-native/{source}/{native_id}")
def listing_inspector_by_native(
    source: str, native_id: str, conn: Any = Depends(deps.get_db_conn)
) -> dict[str, Any]:
    result = location_quality.listing_inspector(
        conn, source=_check_source(source), native_id=native_id
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"{source}:{native_id} not found")
    return result


@router.get("/sample/{source}")
def sample_status(
    source: str,
    unlabelled_only: bool = False,
    limit: int = 200,
    offset: int = 0,
    conn: Any = Depends(deps.get_db_conn),
) -> dict[str, Any]:
    _check_source(source)
    sample = location_labels.current_sample(conn, source)
    if sample is None:
        return {"data": {"sample": None, "members": []}}
    members = location_labels.sample_members(
        conn, source, unlabelled_only=unlabelled_only, limit=limit, offset=offset
    )
    return {"data": {"sample": sample, "members": members}}


class LabelsIn(BaseModel):
    listing_id: int
    labels: dict[str, Any] = Field(default_factory=dict)


@router.post("/sample/{source}/labels")
def save_labels(
    source: str, body: LabelsIn, conn: Any = Depends(deps.get_db_conn)
) -> dict[str, Any]:
    _check_source(source)
    try:
        updated = location_labels.save_labels(conn, source, body.listing_id, body.labels)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(
            status_code=404,
            detail=f"listing {body.listing_id} is not a member of the current"
                   f" {source} sample (membership is frozen)",
        )
    return {"data": {"saved": True, "listing_id": body.listing_id}}


@router.get("/sample/{source}/score")
def score_sample(source: str, conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    return location_labels.score_sample(conn, _check_source(source))


class CorrectionIn(BaseModel):
    listing_id: int
    claim_type: str
    value_text: str
    note: str | None = None


@router.post("/corrections")
def submit_correction(
    body: CorrectionIn, conn: Any = Depends(deps.get_db_conn)
) -> dict[str, Any]:
    # Imported lazily: the module pulls the resolver machinery on first
    # resolve_now(), and a boot-time import failure in a route module takes the
    # whole API down silently (api-docker-import-surface).
    from location_data import operator_corrections as oc

    try:
        result = oc.submit_correction(
            conn,
            listing_id=body.listing_id,
            claim_type=body.claim_type,
            value_text=body.value_text,
            note=body.note,
        )
    except oc.UnknownListingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except oc.CorrectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result["resolved"] = oc.resolve_now(conn, body.listing_id)
    result["projection"] = oc.read_projection(conn, body.listing_id)
    return {"data": result}
