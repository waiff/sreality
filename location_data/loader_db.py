"""Connection + bookkeeping shared by the registry loaders, plus the statement-budget
helpers (`env_timeout_s`, `bounded`) every location batch lane uses.

Connection mode (04 C1.7 rule 1): a registry load wants a session it owns — a 3.02 M-row
COPY, an index build and a `statement_timeout = 0` are all wrong on the transaction-mode
pooler, where each statement can land on a different backend. `LOCATION_DB_DIRECT_URL`
takes precedence when the operator has a direct 5432 URL; otherwise `SUPABASE_DB_SESSION_URL`
(session-mode pooler, dedicated backend), which is the closest thing this project has to a
direct connection.

`scraper.db.connect_session()` falls back to the TRANSACTION pooler when the session URL is
missing; that fallback is deliberately not inherited here — it silently discards the session
GUCs below and would run the COPY under the pooler's statement timeout. The mode is chosen
explicitly, the GUCs are read back after they are set, and a load that cannot get a session
it owns aborts naming the env var it wants.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

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

# `statement_timeout = 0` above is right for the BULK phases and only for those. On
# 2026-08-10 a boundary pack ran 2 h 04 min without emitting one line — no error, no
# reconnect, no lock wait (lock_timeout is 5 s) — inside the per-unit loop, because with
# no statement timeout there is nothing that can turn a runaway PostGIS statement into an
# exception the loader's existing skip/reconnect resilience already knows how to handle.
# So every per-statement phase re-arms a bounded timeout for the length of ONE transaction
# via `bounded()`, and the session default stays 0 for COPY.
#
# `set_config(..., true)` rather than `SET LOCAL <literal>`: the value is a bound
# parameter, so the budget can come from an env var without string-building SQL. The third
# argument IS the LOCAL flag — it reverts at transaction end, which is the whole point (a
# session-level SET would silently outlive the phase and clamp the next COPY).
_TIMEOUT_GUARD_SQL = """
SELECT set_config('statement_timeout', %(statement_timeout)s, true),
       set_config('lock_timeout', %(lock_timeout)s, true)
"""

DEFAULT_LOCK_TIMEOUT_S = 5


def env_timeout_s(name: str, default: int) -> int:
    """Seconds for a `SET LOCAL statement_timeout`, overridable per environment.

    A non-numeric or non-positive value is the default, not a crash: a typo in a workflow
    input must not take a lane down, and 0 ("no timeout") is exactly the state this whole
    mechanism exists to stop, so it is never reachable from an env var.
    """
    return env_positive_int(name, default)


def env_positive_int(name: str, default: int) -> int:
    """A positive-integer knob, overridable per environment.

    Shared with the non-timeout budgets (chunk sizes, version caps) because the
    discipline is the same one: a typo or a non-positive value is the default, not a
    crash — and for every knob that reaches this helper, 0 means "no bound at all",
    which is exactly the state each of them exists to stop.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOG.warning("LOADER %s=%r is not an integer; using %d", name, raw, default)
        return default
    if value <= 0:
        LOG.warning("LOADER %s=%r is not positive; using %d", name, raw, default)
        return default
    return value


@contextlib.contextmanager
def bounded(
    conn: psycopg.Connection,
    statement_timeout_s: int,
    *,
    lock_timeout_s: int = DEFAULT_LOCK_TIMEOUT_S,
) -> Iterator[psycopg.Cursor]:
    """One transaction whose statements are bounded, yielding its cursor.

    Mirrors `scripts/location_mapy_inventory.guarded`. Use it for per-statement /
    per-unit phases; a genuine bulk phase (COPY, index build, whole-table rebuild) keeps
    the session's `statement_timeout = 0` and must NOT be wrapped.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                _TIMEOUT_GUARD_SQL,
                {
                    "statement_timeout": f"{statement_timeout_s}s",
                    "lock_timeout": f"{lock_timeout_s}s",
                },
            )
            yield cur


class LoadAborted(RuntimeError):
    """A blocking load-time control failed; nothing was published."""


def _endpoint(url: str) -> str:
    """host:port only — never the credentials."""
    parts = urlsplit(url)
    return f"{parts.hostname or '?'}:{parts.port or 5432}"


def open_loader_connection() -> psycopg.Connection:
    direct = os.environ.get("LOCATION_DB_DIRECT_URL")
    session = os.environ.get("SUPABASE_DB_SESSION_URL")
    if direct:
        mode, url, conn = "direct", direct, db.connect(direct)
    elif session:
        mode, url, conn = "session-pooler", session, db.connect_session(session)
    else:
        raise LoadAborted(
            "no session-mode connection: set LOCATION_DB_DIRECT_URL (a direct 5432 URL) or "
            "SUPABASE_DB_SESSION_URL. SUPABASE_DB_URL alone is the TRANSACTION pooler, which "
            "rebinds every statement to a different backend — the session GUCs a 3 M-row COPY "
            "needs would be discarded (04 C1.7 rule 1)."
        )
    try:
        with conn.cursor() as cur:
            for statement in _SESSION_GUC:
                cur.execute(statement)
            cur.execute("SHOW statement_timeout")
            timeout = str(cur.fetchone()[0])
            cur.execute("SHOW lock_timeout")
            lock_timeout = str(cur.fetchone()[0])
    except Exception:
        conn.close()
        raise
    if timeout not in ("0", "0ms"):
        conn.close()
        raise LoadAborted(
            f"session GUCs did not take on the {mode} connection to {_endpoint(url)}: "
            f"statement_timeout={timeout!r}, expected '0'. A 3 M-row COPY cannot run under a "
            "statement timeout; point LOCATION_DB_DIRECT_URL at a direct 5432 URL."
        )
    LOG.info(
        "LOADER connection mode=%s endpoint=%s statement_timeout=%s lock_timeout=%s",
        mode, _endpoint(url), timeout, lock_timeout,
    )
    return conn


def scalar(conn: psycopg.Connection, sql: str, params: Any = None) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else row[0]


_DISCREPANCY_SQL = """
INSERT INTO registry_load_discrepancies
       (registry_version_id, entity_kind, entity_code, discrepancy, detail)
VALUES (%s, %s::ruian_level, %s, %s, %s::jsonb)
ON CONFLICT (registry_version_id, entity_kind, entity_code, discrepancy)
DO UPDATE SET detail = EXCLUDED.detail
"""


def record_discrepancy(
    conn: psycopg.Connection | None,
    version_id: int,
    *,
    entity_kind: str,
    entity_code: int,
    discrepancy: str,
    detail: dict[str, Any] | None = None,
    own_connection: bool = False,
) -> None:
    """Append to `registry_load_discrepancies` (01 §3.1). Idempotent on the PK.

    `own_connection=True` is the FAILURE-PATH mode and NEVER raises: whatever broke the
    load may have taken the connection with it, so the row gets a fresh short-lived
    connection of its own and swallows its own errors. A bookkeeping write must never
    mask the exception that caused it — the 2026-08 boundary run died reporting "the
    connection is closed" from this INSERT instead of the SSL drop that actually killed
    it (same reasoning as `scripts/location_mapy_inventory.record_failure`). `conn` is
    ignored in that mode; callers on a possibly-dead handle pass None.
    """
    params = (version_id, entity_kind, entity_code, discrepancy, json.dumps(detail or {}))
    if not own_connection:
        assert conn is not None
        with conn.cursor() as cur:
            cur.execute(_DISCREPANCY_SQL, params)
        return
    try:
        # The loader's own opener, not scraper.db.connect(): a load may be configured
        # with LOCATION_DB_DIRECT_URL alone, and the failure path must not need a second
        # env var to be able to say why it failed.
        with open_loader_connection() as fresh:
            with fresh.cursor() as cur:
                cur.execute(_DISCREPANCY_SQL, params)
    except Exception:  # noqa: BLE001 - a failed failure-record must never mask the cause
        LOG.exception(
            "LOADER could not record discrepancy version=%s kind=%s code=%s %s",
            version_id, entity_kind, entity_code, discrepancy,
        )


def abort(
    conn: psycopg.Connection | None,
    version_id: int,
    *,
    reason: str,
    detail: dict[str, Any],
    own_connection: bool = False,
) -> None:
    """Record the aborted load and raise. There is no separate failure table: an aborted
    load is a `registry_load_discrepancies` row with `discrepancy='load_aborted'`
    (01 §3.1 (b)), which is what gives 04 §4.5.1's page condition a data source.

    `entity_kind` is the `ruian_level` enum, so it cannot name an artefact — the failing
    assertion, its expected/actual values and the retained staging relations live in
    `detail`, and the row is anchored at ('stat', 0).

    The bookkeeping row is best-effort in BOTH modes: `LoadAborted` carries the real
    reason, so a discrepancy write that fails is logged and the abort is raised anyway
    (`own_connection=True` for callers whose connection may already be dead).
    """
    try:
        record_discrepancy(
            conn,
            version_id,
            entity_kind="stat",
            entity_code=0,
            discrepancy="load_aborted",
            detail={"reason": reason, **detail},
            own_connection=own_connection,
        )
    except Exception:  # noqa: BLE001 - the abort below is the message that matters
        LOG.exception("LOADER could not record load_aborted version=%s", version_id)
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
