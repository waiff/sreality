"""Location-quality dashboard + operator corrections — the consumer of
`listing_location`, the location program's one answer table.

Mounted under `/location/*`, admin-gated at the router (single-operator
diagnostic surface). Reads go through `toolkit/location_quality.py` on the
service-role connection — the location tables are RLS-on with anon/authenticated
revoked, so this API is the only path the SPA has.

The corrections POST is a WRITE EXCEPTION in the toolkit sense: it appends an
operator claim (`location_data/operator_corrections.py`, rank 1) and then
resolves the listing synchronously so the response already carries the
refreshed answer row — 05 5.5.5 read-your-writes. If the synchronous resolve
fails, the unconditional dirty enqueue guarantees the */15 drain converges;
the response says which happened.

W2-b deleted two route families with the tables under them: the frozen
labelled samples (`/sample/*` — old-vs-new precision scoring, and "new" is now
the only system) and the dark old-vs-new compare bench (`/compare/*`, whose
pg_cron cohort joined both dropped projections).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import dependencies as deps
from toolkit import location_quality

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
