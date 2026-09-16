"""`autodedup.iterations` — the progress ledger every lane pass writes (§12, §13).

One row per iteration of any wave: `status='running'` when the pass starts, a terminal
status plus sample statistics, metrics, cost and artifact links when it ends. A pass that
dies leaves a `running` row with a stale `started_at`, which is itself the signal the
progress page renders.

The ledger is BEST EFFORT by construction. A pass that ran against a database where
migration 528 has not been applied — a local run, a fork, the moment between a merged PR and
an applied migration — still does its work: `to_regclass` probes for the table and every
entry point returns None when it is absent. The caller treats a None id as "no ledger this
run", never as a failure.

Cost is written from what `llm_calls` actually billed (E32), never forecast; the export lane
spends nothing and writes 0.

A filed row is not frozen: `record id=<n>` patches the fields a later correction knows better
(a cost the wave only learned afterwards, a typo'd title, a terminal status for a pass that
died `running`). Only the named fields move, and never the `wave` — that is the row's
identity on the progress page, so a mis-waved entry is a new row, not an edit.
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

MAX_STATS_BYTES = 20_000

ACTIONS_RUN_URL = "https://github.com/waiff/sreality/actions/runs/"

STATUSES: tuple[str, ...] = ("running", "done", "failed", "skipped")

ITERATIONS_PRESENT_SQL = """
SELECT to_regclass('autodedup.iterations') IS NOT NULL AS present
"""

START_ITERATION_SQL = """
INSERT INTO autodedup.iterations (wave, title, status, approach, tools, artifacts, run_id, started_at)
VALUES (
    %(wave)s,
    %(title)s,
    'running',
    %(approach)s::text,
    %(tools)s::text[],
    %(artifacts)s::jsonb,
    %(run_id)s::bigint,
    now()
)
RETURNING id
"""

FINISH_ITERATION_SQL = """
UPDATE autodedup.iterations
SET status       = %(status)s,
    sample_stats = coalesce(%(sample_stats)s::jsonb, sample_stats),
    metrics      = coalesce(%(metrics)s::jsonb, metrics),
    cost_usd     = coalesce(%(cost_usd)s::numeric, cost_usd),
    artifacts    = coalesce(%(artifacts)s::jsonb, artifacts),
    notes        = coalesce(%(notes)s::text, notes),
    finished_at  = now()
WHERE id = %(id)s::bigint
RETURNING id
"""

RECORD_ITERATION_SQL = """
INSERT INTO autodedup.iterations (
    wave, title, status, approach, tools, sample_stats, metrics, cost_usd,
    artifacts, run_id, notes, started_at, finished_at
)
VALUES (
    %(wave)s,
    %(title)s,
    %(status)s,
    %(approach)s::text,
    %(tools)s::text[],
    %(sample_stats)s::jsonb,
    %(metrics)s::jsonb,
    coalesce(%(cost_usd)s::numeric, 0),
    %(artifacts)s::jsonb,
    %(run_id)s::bigint,
    %(notes)s::text,
    now(),
    now()
)
RETURNING id
"""

# The correction path behind `record id=<n>`. Every assignment is `coalesce(param, column)`,
# so a parameter left NULL is a field the caller never named and the row keeps what it had —
# ONE static statement CI can PREPARE, rather than a SET list assembled per call. `wave` is
# absent on purpose (a row's wave is its identity on the progress page) and so is `artifacts`
# (written by the run that produced them, never by a later correction). A status that turns
# terminal stamps `finished_at`: a row corrected to `done` must not keep reading as running.
UPDATE_ITERATION_SQL = """
UPDATE autodedup.iterations
SET title       = coalesce(%(title)s::text, title),
    status      = coalesce(%(status)s::text, status),
    approach    = coalesce(%(approach)s::text, approach),
    tools       = coalesce(%(tools)s::text[], tools),
    cost_usd    = coalesce(%(cost_usd)s::numeric, cost_usd),
    notes       = coalesce(%(notes)s::text, notes),
    finished_at = CASE
                      WHEN %(status)s::text IN ('done', 'failed') THEN now()
                      ELSE finished_at
                  END
WHERE id = %(id)s::bigint
RETURNING id
"""


def json_param(value: Any, *, limit: int = MAX_STATS_BYTES) -> str | None:
    """A jsonb parameter, truncated to `limit` bytes. A mode summary is free-form and one
    run's summary must never be the reason the ledger write fails, so an oversized payload
    is replaced by its own head plus the byte count it would have been."""
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    if len(text.encode("utf-8")) <= limit:
        return text
    return json.dumps(
        {"truncated": True, "bytes": len(text.encode("utf-8")), "preview": text[: limit // 2]},
        ensure_ascii=False,
    )


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Every ledger statement is a single-row read or write; 15 s is generous for all of them and
# turns a lock held by a concurrent migration into the caught exception this module already
# treats as "no ledger this run", instead of a wait that can outlast the workflow itself.
LEDGER_TIMEOUT_MS = 15_000


def _execute(conn: Any, sql: str, params: dict[str, Any] | None = None) -> Any:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {LEDGER_TIMEOUT_MS}")
            cur.execute(sql, params)
            return cur.fetchone()


def store_ready(conn: Any) -> bool:
    """True when `autodedup.iterations` exists. Any failure reads as "not ready"."""
    if conn is None:
        return False
    try:
        row = _execute(conn, ITERATIONS_PRESENT_SQL)
    except Exception:  # noqa: BLE001 — a ledger probe never fails a lane
        return False
    if not row:
        return False
    value = row[0] if isinstance(row, (list, tuple)) else row.get("present")
    return bool(value)


def _one(conn: Any, sql: str, params: dict[str, Any]) -> int | None:
    row = _execute(conn, sql, params)
    if not row:
        return None
    value = row[0] if isinstance(row, (list, tuple)) else row.get("id")
    return _int_or_none(value)


def start_iteration(
    conn: Any,
    *,
    wave: str,
    title: str,
    approach: str | None = None,
    tools: Sequence[str] | None = None,
    run_id: Any = None,
    artifacts: dict[str, Any] | None = None,
) -> int | None:
    """Open a `running` row; None when the schema is not there yet."""
    if not store_ready(conn):
        print("[iterations] autodedup.iterations absent — ledger row skipped", flush=True)
        return None
    return _one(
        conn,
        START_ITERATION_SQL,
        {
            "wave": wave,
            "title": title,
            "approach": approach,
            "tools": list(tools or []),
            "artifacts": json_param(artifacts),
            "run_id": _int_or_none(run_id),
        },
    )


def finish_iteration(
    conn: Any,
    iteration_id: int | None,
    *,
    status: str,
    sample_stats: Any = None,
    metrics: Any = None,
    cost_usd: Any = None,
    artifacts: dict[str, Any] | None = None,
    notes: str | None = None,
) -> int | None:
    """Stamp a terminal status on an open row. A None id is a no-op, so the caller does not
    branch on whether the ledger was available when the pass started."""
    if iteration_id is None or conn is None:
        return None
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; known: {', '.join(STATUSES)}")
    return _one(
        conn,
        FINISH_ITERATION_SQL,
        {
            "id": int(iteration_id),
            "status": status,
            "sample_stats": json_param(sample_stats),
            "metrics": json_param(metrics),
            "cost_usd": cost_usd,
            "artifacts": json_param(artifacts),
            "notes": notes,
        },
    )


def record_iteration(
    conn: Any,
    *,
    wave: str,
    title: str,
    status: str = "done",
    approach: str | None = None,
    tools: Sequence[str] | None = None,
    sample_stats: Any = None,
    metrics: Any = None,
    cost_usd: Any = None,
    artifacts: dict[str, Any] | None = None,
    run_id: Any = None,
    notes: str | None = None,
) -> int | None:
    """Insert one already-finished row — the retroactive entry for work done before the
    ledger existed (W0's census, W0b's migrations), so the progress page is not missing the
    waves that came first."""
    if not store_ready(conn):
        print("[iterations] autodedup.iterations absent — ledger row skipped", flush=True)
        return None
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; known: {', '.join(STATUSES)}")
    return _one(
        conn,
        RECORD_ITERATION_SQL,
        {
            "wave": wave,
            "title": title,
            "status": status,
            "approach": approach,
            "tools": list(tools or []),
            "sample_stats": json_param(sample_stats),
            "metrics": json_param(metrics),
            "cost_usd": cost_usd,
            "artifacts": json_param(artifacts),
            "run_id": _int_or_none(run_id),
            "notes": notes,
        },
    )


def update_iteration(
    conn: Any,
    iteration_id: int,
    *,
    title: str | None = None,
    status: str | None = None,
    approach: str | None = None,
    tools: Sequence[str] | None = None,
    cost_usd: Any = None,
    notes: str | None = None,
) -> int | None:
    """Patch ONLY the fields named on an existing row; None when no such row exists.

    The corrective half of the ledger: a row filed with a placeholder cost, a typo in the
    title, or left `running` by a pass that died is fixed in place rather than filed twice.
    Everything left None is untouched, so a one-field correction cannot blank the rest."""
    if conn is None:
        return None
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; known: {', '.join(STATUSES)}")
    return _one(
        conn,
        UPDATE_ITERATION_SQL,
        {
            "id": int(iteration_id),
            "title": title,
            "status": status,
            "approach": approach,
            "tools": list(tools) if tools else None,
            "cost_usd": cost_usd,
            "notes": notes,
        },
    )


# --- mode entry -------------------------------------------------------------------------

RECORD_ARG_DEFAULTS: dict[str, Any] = {
    # `id=<n>` turns the mode from "file a new row" into "correct that one".
    "id": "",
    "wave": "",
    "title": "",
    "status": "done",
    "approach": "",
    # A semicolon OR slash list, because the lane's own `k=v,k=v` parser owns the comma. The
    # workflow input's charset check rejects `;` outright, so a DISPATCHED entry writes its
    # tools as `a/b/c`; `;` stays accepted for the local runs and the entries already filed.
    "tools": "",
    "notes": "",
    "cost_usd": "",
    "run_id": "",
}

# What `id=<n>` may change. `wave` is the row's identity on the progress page and `run_id` /
# `artifacts` belong to the run that produced the row — a correction touches neither.
UPDATABLE_RECORD_ARGS: tuple[str, ...] = (
    "title", "status", "approach", "tools", "cost_usd", "notes",
)

_TOOLS_SEPARATORS = re.compile(r"[;/]")


def _split_tools(value: Any) -> list[str]:
    return [part.strip() for part in _TOOLS_SEPARATORS.split(str(value)) if part.strip()]


def _parse_cost(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"record arg cost_usd must be a number, got {value!r}") from None


def _parse_record_update(args: dict[str, str], iteration_id: int) -> dict[str, Any]:
    """The `id=<n>` form: exactly the fields the caller named, coerced."""
    frozen = sorted(set(args) - set(UPDATABLE_RECORD_ARGS) - {"id"})
    if frozen:
        why = (
            " — a row's wave is its identity on the progress page, so a mis-waved entry is "
            "a new row, not an edit" if "wave" in frozen else ""
        )
        raise ValueError(
            f"record id={iteration_id} cannot change {', '.join(frozen)}{why}; "
            f"updatable: {', '.join(UPDATABLE_RECORD_ARGS)}"
        )
    updates: dict[str, Any] = {}
    for key in UPDATABLE_RECORD_ARGS:
        if key not in args:
            continue
        raw = str(args[key]).strip()
        if not raw:
            raise ValueError(
                f"record arg {key} is empty; an update sets a value, it cannot clear one"
            )
        if key == "status" and raw not in STATUSES:
            raise ValueError(f"record arg status must be one of {', '.join(STATUSES)}")
        updates[key] = (
            _split_tools(raw) if key == "tools"
            else _parse_cost(raw) if key == "cost_usd"
            else raw
        )
    if not updates:
        raise ValueError(
            f"record id={iteration_id} names no field to update; "
            f"one of: {', '.join(UPDATABLE_RECORD_ARGS)}"
        )
    return {"id": iteration_id, "updates": updates}


def parse_record_args(args: dict[str, str]) -> dict[str, Any]:
    """Coerce the lane's k=v strings into a ledger write. Unknown keys are fatal.

    Without `id` this is the retroactive INSERT and `wave`/`title` are required; with
    `id=<n>` it is an UPDATE of that row carrying only the keys actually given."""
    unknown = sorted(set(args) - set(RECORD_ARG_DEFAULTS))
    if unknown:
        raise ValueError(
            f"unknown record arg(s) {', '.join(unknown)}; "
            f"known: {', '.join(sorted(RECORD_ARG_DEFAULTS))}"
        )
    raw_id = str(args.get("id", "")).strip()
    if raw_id:
        iteration_id = _int_or_none(raw_id)
        if iteration_id is None or iteration_id <= 0:
            raise ValueError(f"record arg id must be a positive integer, got {raw_id!r}")
        return _parse_record_update(args, iteration_id)
    out: dict[str, Any] = dict(RECORD_ARG_DEFAULTS)
    out.update(args)
    out["id"] = None
    for key in ("wave", "title"):
        if not str(out[key]).strip():
            raise ValueError(f"record arg {key} is required")
    if out["status"] not in STATUSES:
        raise ValueError(f"record arg status must be one of {', '.join(STATUSES)}")
    out["tools"] = _split_tools(out["tools"])
    out["cost_usd"] = _parse_cost(out["cost_usd"])
    out["run_id"] = _int_or_none(out["run_id"])
    for key in ("approach", "notes"):
        out[key] = str(out[key]).strip() or None
    return out


def _record_insert(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    iteration_id = record_iteration(
        conn,
        wave=params["wave"],
        title=params["title"],
        status=params["status"],
        approach=params["approach"],
        tools=params["tools"],
        cost_usd=params["cost_usd"],
        run_id=params["run_id"],
        notes=params["notes"],
        artifacts=(
            {"actions_run": f"{ACTIONS_RUN_URL}{params['run_id']}"}
            if params["run_id"]
            else None
        ),
    )
    return {"iteration_id": iteration_id, "recorded": iteration_id is not None, **params}


def _record_update(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    iteration_id = params["id"]
    updates = params["updates"]
    if not store_ready(conn):
        print("[iterations] autodedup.iterations absent — ledger update skipped", flush=True)
        return {"iteration_id": None, "recorded": False, "updated": False, **params}
    if update_iteration(conn, iteration_id, **updates) is None:
        # A silent no-op would read as a successful correction on a run nobody rechecks, so
        # a typo'd id is a hard stop rather than a green run that changed nothing.
        raise SystemExit(
            f"record id={iteration_id}: no autodedup.iterations row with that id — "
            f"nothing updated (the /autodedup/progress page lists the ids that exist)"
        )
    return {"iteration_id": iteration_id, "recorded": False, "updated": True, **params}


def run_record(conn_factory: Any, args: dict[str, str], out_dir: Any) -> dict[str, Any]:
    """`mode=record` — write ONE iteration row from the dispatch arguments, or correct one.

    The retroactive entry point: a wave that closed before this ledger existed, or an
    iteration whose work happened outside a lane run (a migration applied by
    `apply_migration.yml`, an operator review session), still gets its row on the progress
    page. With `id=<n>` it instead patches that row's named fields — the cost a wave only
    knew afterwards, a corrected title, a terminal status for a pass that died `running` —
    which is why nothing here is ever filed twice to fix a number.

    It is the one mode the lane does not wrap in start/finish: it writes its own terminal
    row, and wrapping it would file the bookkeeping twice — an `id=` correction doubly so,
    since a wrapper row would add the very iteration the operator came to edit.
    """
    params = parse_record_args(args)
    conn = conn_factory()
    try:
        if params["id"] is not None:
            return _record_update(conn, params)
        return _record_insert(conn, params)
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()
