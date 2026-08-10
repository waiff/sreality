"""Connection + bookkeeping shared by the registry loaders.

Connection mode (04 C1.7 rule 1): a registry load wants a session it owns — a 3.02 M-row
COPY, an index build and a `statement_timeout = 0` are all wrong on the transaction-mode
pooler, where each statement can land on a different backend. `LOCATION_DB_DIRECT_URL`
takes precedence when the operator has a direct 5432 URL; otherwise we fall back to
`scraper.db.connect_session()` (session-mode pooler, dedicated backend), which is the
closest thing this project has to a direct connection.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import psycopg

from scraper import db

LOG = logging.getLogger("location_data.loader_db")

# Long enough for a 3 M-row COPY + index build; short lock waits so the loader queues
# behind live ingest rather than blocking it.
_SESSION_GUC = (
    "SET statement_timeout = 0",
    "SET lock_timeout = '5s'",
    "SET idle_in_transaction_session_timeout = '15min'",
)


class LoadAborted(RuntimeError):
    """A blocking load-time control failed; nothing was published."""


def open_loader_connection() -> psycopg.Connection:
    direct = os.environ.get("LOCATION_DB_DIRECT_URL")
    conn = db.connect(direct) if direct else db.connect_session()
    with conn.cursor() as cur:
        for statement in _SESSION_GUC:
            cur.execute(statement)
    return conn


def scalar(conn: psycopg.Connection, sql: str, params: Any = None) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else row[0]


def record_discrepancy(
    conn: psycopg.Connection,
    version_id: int,
    *,
    entity_kind: str,
    entity_code: int,
    discrepancy: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append to `registry_load_discrepancies` (01 §3.1). Idempotent on the PK."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO registry_load_discrepancies
                   (registry_version_id, entity_kind, entity_code, discrepancy, detail)
            VALUES (%s, %s::ruian_level, %s, %s, %s::jsonb)
            ON CONFLICT (registry_version_id, entity_kind, entity_code, discrepancy)
            DO UPDATE SET detail = EXCLUDED.detail
            """,
            (version_id, entity_kind, entity_code, discrepancy, json.dumps(detail or {})),
        )


def abort(
    conn: psycopg.Connection,
    version_id: int,
    *,
    reason: str,
    detail: dict[str, Any],
) -> None:
    """Record the aborted load and raise. There is no separate failure table: an aborted
    load is a `registry_load_discrepancies` row with `discrepancy='load_aborted'`
    (01 §3.1 (b)), which is what gives 04 §4.5.1's page condition a data source.

    `entity_kind` is the `ruian_level` enum, so it cannot name an artefact — the failing
    assertion, its expected/actual values and the retained staging relations live in
    `detail`, and the row is anchored at ('stat', 0).
    """
    record_discrepancy(
        conn,
        version_id,
        entity_kind="stat",
        entity_code=0,
        discrepancy="load_aborted",
        detail={"reason": reason, **detail},
    )
    raise LoadAborted(f"{reason}: {detail}")


def read_progress(conn: psycopg.Connection, version_id: int) -> dict[str, Any]:
    value = scalar(conn, "SELECT row_counts FROM registry_versions WHERE id = %s", (version_id,))
    return dict(value or {})


def write_progress(
    conn: psycopg.Connection,
    version_id: int,
    *,
    phase: str | None = None,
    counts: dict[str, Any] | None = None,
) -> None:
    """Checkpoint into `registry_versions.row_counts` so a killed run resumes rather than
    restarts. `_phase` / `_phases_done` are bookkeeping keys alongside the real counts —
    no extra DDL, and the version is still not `is_current` until publish."""
    progress = read_progress(conn, version_id)
    if counts:
        progress.update(counts)
    if phase:
        done = list(progress.get("_phases_done") or [])
        if phase not in done:
            done.append(phase)
        progress["_phases_done"] = done
        progress["_phase"] = phase
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE registry_versions SET row_counts = %s::jsonb WHERE id = %s",
            (json.dumps(progress, default=str), version_id),
        )


def phase_done(progress: dict[str, Any], phase: str) -> bool:
    return phase in (progress.get("_phases_done") or [])
