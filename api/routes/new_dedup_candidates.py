"""NEW DEDUP Level 0 — what the Candidate audit page reads (docs/design/new-dedup/PROGRAM.md,
Wave 2, ledger 2026-09-10 (a)/(b)). Read-only, admin-gated.

A GENERATION is one run of the candidate lane under one parameter set. The audit numbers are
computed once, at the end of that run, onto the generation row (`stats`) — so this surface is
three small reads of `dedup_sim.candidate_generations`, never a scan of the pair table.

The store lives in migration 492. Until that migration is applied the page must still render:
every route here answers `store_ready: false` with empty content instead of failing, so an
un-migrated database shows "the store is not created yet" and not a 500.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api import dependencies as deps
from toolkit import dedup_candidates as dc

try:  # the two SQLSTATEs a missing store raises, if the probe above ever misses it
    from psycopg import errors as _pg_errors

    _MISSING_RELATION: tuple[type[BaseException], ...] = (
        _pg_errors.UndefinedTable,
        _pg_errors.InvalidSchemaName,
    )
except Exception:  # noqa: BLE001 — psycopg absent (tests run on fake connections)
    _MISSING_RELATION = ()

router = APIRouter(
    prefix="/new-dedup/candidates",
    tags=["new-dedup"],
    dependencies=[Depends(deps.require_admin)],
)

# How many runs the page's picker offers.
RECENT_LIMIT = 20

# The audit page shows a column per path from day one, including the ones that do not exist
# yet — an empty column is the honest answer, an omitted one hides the gap. Only path C is a
# `PathDef` in the registry; A and B are named here with no rungs and `built: false`.
_UNBUILT_PATHS: dict[str, dict[str, str]] = {
    "A": {
        "label": "street / geo / radius",
        "explanation": (
            "Path A would pair two listings that share a street or sit within so many "
            "metres of each other. It is not built: the location data on input is only "
            "reliably right at town grain, which is exactly what path C uses instead. "
            "The column stays here, empty, so the gap is visible rather than hidden."
        ),
    },
    "B": {
        "label": "image similarity",
        "explanation": (
            "Path B would pair two listings whose photographs look like the same rooms, "
            "with no location test at all. It is Wave 3 work and has produced nothing "
            "yet, so every number in this column is empty."
        ),
    },
}

_UNKNOWN_PATH: dict[str, str] = {
    "label": "not described",
    "explanation": "This path has no definition in the registry yet.",
}


def _paths() -> list[dict[str, Any]]:
    """The path registry as the page renders it: one row per path, built or not, each with
    the plain-language explanation of what it looks for and the rungs it can take."""
    out: list[dict[str, Any]] = []
    for code in dc.PATH_CODES:
        pd = dc.PATHS.get(code)
        if pd is None:
            static = _UNBUILT_PATHS.get(code, _UNKNOWN_PATH)
            out.append(
                {
                    "code": code,
                    "label": static["label"],
                    "built": False,
                    "block_key": None,
                    "explanation": static["explanation"],
                    "rungs": [],
                }
            )
            continue
        out.append(
            {
                "code": pd.code,
                "label": pd.label,
                "built": True,
                "block_key": pd.block_key,
                "explanation": pd.explanation,
                "rungs": [
                    {
                        "code": r.code,
                        "label": r.label,
                        "needs": list(r.needs),
                        "explanation": r.explanation,
                    }
                    for r in pd.rungs
                ],
            }
        )
    return out


def _not_ready() -> dict[str, Any]:
    """The un-migrated answer: the path registry still renders (it is code, not data), the
    run-shaped fields are empty, and `store_ready` tells the page to say so."""
    return {
        "data": {
            "store_ready": False,
            "paths": _paths(),
            "generation": None,
            "stats": None,
            "recent": [],
        }
    }


def _generation_payload(gen: dc.Generation, times: dict[str, Any] | None) -> dict[str, Any]:
    times = times or {}
    return {
        "id": gen.id,
        "simulation_run_id": gen.simulation_run_id,
        "inputs_id": gen.inputs_id,
        "path": gen.path,
        "fingerprint": gen.fingerprint,
        "status": gen.status,
        "created_at": times.get("created_at"),
        "started_at": times.get("started_at"),
        "completed_at": times.get("completed_at"),
        "inputs": gen.inputs,
        "progress": gen.progress,
        "error_message": times.get("error_message"),
    }


@router.get("/overview")
def overview(
    generation_id: int | None = None, conn: Any = Depends(deps.get_db_conn)
) -> dict[str, Any]:
    """The whole page in one call: the path registry, the shown generation with its parameter
    set and its stats, and the recent runs to pick between. `generation_id` shows any run (to
    compare two parameter sets); without it, the newest SUCCESSFUL path C run."""
    if not dc.store_ready(conn):
        return _not_ready()
    try:
        gen = (
            dc.get_generation(conn, generation_id)
            if generation_id is not None
            else dc.latest_generation(conn, "C", status="success")
        )
    except _MISSING_RELATION:
        return _not_ready()
    # A run the operator asked for by id and that does not exist is a 404 — never an empty
    # page pretending it was found. Raised outside the handlers above so it can't be caught.
    if generation_id is not None and gen is None:
        raise HTTPException(status_code=404, detail=f"generation {generation_id} not found")
    try:
        times = dc.generation_timestamps(conn, gen.id) if gen is not None else None
        recent = dc.list_recent_generations(conn, "C", RECENT_LIMIT)
    except _MISSING_RELATION:
        return _not_ready()
    return {
        "data": {
            "store_ready": True,
            "paths": _paths(),
            "generation": _generation_payload(gen, times) if gen is not None else None,
            "stats": gen.stats if gen is not None else None,
            "recent": recent,
        }
    }


@router.get("/generations")
def list_generations(conn: Any = Depends(deps.get_db_conn)) -> dict[str, Any]:
    """The run picker on its own — same rows as `overview.recent`."""
    if not dc.store_ready(conn):
        return {"data": []}
    try:
        return {"data": dc.list_recent_generations(conn, "C", RECENT_LIMIT)}
    except _MISSING_RELATION:
        return {"data": []}
