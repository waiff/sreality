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
"""

from __future__ import annotations

import json
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


# --- mode entry -------------------------------------------------------------------------

RECORD_ARG_DEFAULTS: dict[str, Any] = {
    "wave": "",
    "title": "",
    "status": "done",
    "approach": "",
    # A semicolon list, because the lane's own `k=v,k=v` parser owns the comma. Note the
    # workflow input's charset check rejects `;`, so a dispatched record entry carries one
    # tool per run or is edited in afterwards; a local run has the full list.
    "tools": "",
    "notes": "",
    "cost_usd": "",
    "run_id": "",
}

_TOOLS_SEPARATOR = ";"


def parse_record_args(args: dict[str, str]) -> dict[str, Any]:
    """Coerce the lane's k=v strings into a retroactive ledger row. Unknown keys are fatal."""
    unknown = sorted(set(args) - set(RECORD_ARG_DEFAULTS))
    if unknown:
        raise ValueError(
            f"unknown record arg(s) {', '.join(unknown)}; "
            f"known: {', '.join(sorted(RECORD_ARG_DEFAULTS))}"
        )
    out: dict[str, Any] = dict(RECORD_ARG_DEFAULTS)
    out.update(args)
    for key in ("wave", "title"):
        if not str(out[key]).strip():
            raise ValueError(f"record arg {key} is required")
    if out["status"] not in STATUSES:
        raise ValueError(f"record arg status must be one of {', '.join(STATUSES)}")
    out["tools"] = [
        part.strip() for part in str(out["tools"]).split(_TOOLS_SEPARATOR) if part.strip()
    ]
    if out["cost_usd"] in ("", None):
        out["cost_usd"] = None
    else:
        try:
            out["cost_usd"] = float(out["cost_usd"])
        except (TypeError, ValueError):
            raise ValueError(
                f"record arg cost_usd must be a number, got {out['cost_usd']!r}"
            ) from None
    out["run_id"] = _int_or_none(out["run_id"])
    for key in ("approach", "notes"):
        out[key] = str(out[key]).strip() or None
    return out


def run_record(conn_factory: Any, args: dict[str, str], out_dir: Any) -> dict[str, Any]:
    """`mode=record` — write ONE already-finished iteration row from the dispatch arguments.

    The retroactive entry point: a wave that closed before this ledger existed, or an
    iteration whose work happened outside a lane run (a migration applied by
    `apply_migration.yml`, an operator review session), still gets its row on the progress
    page. It is the one mode the lane does not wrap in start/finish — it writes its own
    terminal row, and wrapping it would file the bookkeeping twice.
    """
    params = parse_record_args(args)
    conn = conn_factory()
    try:
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
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()
    return {"iteration_id": iteration_id, "recorded": iteration_id is not None, **params}
