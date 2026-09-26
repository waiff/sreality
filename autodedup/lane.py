"""The autodedup lane: one dispatch-only entry point behind `.github/workflows/autodedup.yml`.

    python3 -m autodedup.lane --mode census --args "min_n=800,top=60" --out out/
    python3 -m autodedup.lane --mode probes --args "source=remax" --out out/
    python3 -m autodedup.lane --mode export --args "blocks=town:563510 quarter:490245" --out out/
    python3 -m autodedup.lane --mode judge --args "export_run=123,tier=text,n=400,max_usd=5"
    python3 -m autodedup.lane --mode labels --args "generation=g4" --out out/
    python3 -m autodedup.lane --mode record --args "wave=W0,title=Region census,cost_usd=0"
    python3 -m autodedup.lane --mode record --args "id=12,cost_usd=3.10,status=done"
    python3 -m autodedup.lane --mode apply --args "generation=g12" --out out/
    python3 -m autodedup.lane --mode unapply --args "generation=g12,dry_run=0" --out out/

`probes` carries the corpus-wide measurements (ingest rate, one portal's location posture)
that are block-independent, so the census never pays for them once per block. `export` dumps
the ruled cohort as one gzipped JSONL artifact so every later wave iterates locally at $0.

Every mode run is ALSO one row in `autodedup.iterations` — `running` at the start, a terminal
status plus the mode summary, metrics and artifact link at the end — because that ledger is
what the `/autodedup/progress` page renders (PROGRAM.md section 12). The ledger is best
effort: a database without migration 528, or no database at all, costs the row, never the
run. `record` is the one unwrapped mode: it writes its own finished row for work that
happened outside a lane (a migration, an operator session, a wave that closed before the
ledger existed), so wrapping it would file the same iteration twice. That holds doubly for
its `id=<n>` form, which CORRECTS an existing row — a wrapper row there would append the very
iteration the operator came to edit.

A mode is a callable registered in `MODES`; `--args` is a free-form `k=v,k=v` string each mode
validates for itself, so a new mode needs no workflow edit. `out/summary.json` is written on
every path — success, unknown mode, or a raised exception — because the workflow uploads
`out/` with `if: always()` and a run that failed silently is a run nobody can debug.

That artifact is readable by anyone who can see the run and GitHub masks secrets in the LOG
only, never inside an artifact file, so the error text and traceback are scrubbed of every
credential this lane is handed before they are written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autodedup import iterations
from autodedup.apply import run_apply, run_unapply
from autodedup.census import run_census, run_probes, write_json
from autodedup.export import run_export
from autodedup.incremental_lane import run_incremental, run_rt_seed
from autodedup.iterations import run_record
from autodedup.judge_lane import run_judge
from autodedup.labels_lane import run_labels
from autodedup.parity import run_parity
from autodedup.rt_equivalence import run_equivalence
from autodedup.score_lane import run_score
from autodedup.town_probe import run_town

Mode = Callable[[Callable[[], Any], dict[str, str], Path], dict[str, Any]]

MODES: dict[str, Mode] = {
    "census": run_census,
    "probes": run_probes,
    "export": run_export,
    "judge": run_judge,
    "score": run_score,
    "incremental": run_incremental,
    "rt_seed": run_rt_seed,
    "rt_parity": run_parity,
    "rt_equivalence": run_equivalence,
    "labels": run_labels,
    "record": run_record,
    "town": run_town,
    "apply": run_apply,
    "unapply": run_unapply,
}

# `incremental`, `rt_seed`, `rt_parity`, `rt_equivalence` and `town` are deliberately ABSENT
# below, so they run unwrapped — and for the three read-only instruments that is a CONTRACT,
# not an economy: an `iterations` row would be the one write each promises never to make. The pass is
# a `*/10` schedule: a ledger row per pass would file 144 iterations a day, and
# `autodedup.iterations` is the operator's NARRATIVE of the program (one line per unit of
# work a person can read), not a machine log — `autodedup.runs` and the workflow's own run
# summary carry the per-pass detail.
#
# What each mode's ledger row says: the wave it belongs to, the sentence the progress page
# shows, and the "Tools used" chips PROGRAM.md section 14 lists per wave. A mode missing
# from here runs unwrapped — `record`, which files its own row on the way in and, with
# `id=<n>`, edits one that already exists rather than adding another.
ITERATION_META: dict[str, dict[str, Any]] = {
    "census": {
        "wave": "W0",
        "title": "Region census",
        "approach": (
            "Read-only census of all-time listings per town and Praha quarter — portal "
            "breadth, category spread, signal coverage and raw in-block pair counts — so "
            "the operator picks the trial cohort off a table instead of a guess."
        ),
        "tools": ["autodedup.census", "autodedup.lane", "GitHub Actions", "psql"],
    },
    "probes": {
        "wave": "W0b",
        "title": "Corpus-wide probes",
        "approach": (
            "The block-independent measurements — ingest rate L and one portal's location "
            "posture — paid for once rather than once per censused block."
        ),
        "tools": ["autodedup.census", "autodedup.lane", "GitHub Actions"],
    },
    "judge": {
        "wave": "W3",
        "title": "LLM judge",
        "approach": (
            "A deterministic stratified sample of one engine pass, put to the four-way judge "
            "under a pre-flight --max-usd that binds before each call — text, vision or three "
            "deep votes — so the uncertain band gets an arbiter and the programme gets ground "
            "truth built fresh inside autodedup.*."
        ),
        "tools": [
            "autodedup.judge", "autodedup.judge_lane", "autodedup.lane", "api.llm_client",
            "Cloudflare R2", "GitHub Actions",
        ],
    },
    "score": {
        "wave": "W5",
        "title": "Engine pass into the store",
        "approach": (
            "One engine pass over an exported cohort, persisted: every scored pair into "
            "autodedup.pairs and the whole generation of clusters, members and refused "
            "unions rebuilt under one autodedup.runs row — so the validation UI filters a "
            "table instead of an artifact, and an operator verdict has a row to land on."
        ),
        "tools": [
            "autodedup.score_lane", "autodedup.harness", "autodedup.lane", "GitHub Actions",
            "Postgres (schema autodedup)",
        ],
    },
    "labels": {
        "wave": "W6",
        "title": "Operator labels",
        "approach": (
            "The operator's own rulings — every explicit pair verdict plus the member pairs "
            "of every confirmed group, each with the engine's view of that pair and the "
            "permanent must-not-links, plus the operator's Browse merges as groups (E299) — "
            "exported as one artifact, so a fit reads the top label tier from a file instead "
            "of an ad-hoc query."
        ),
        "tools": [
            "autodedup.labels_lane", "autodedup.labels", "autodedup.lane", "GitHub Actions",
            "Postgres (schema autodedup)",
        ],
    },
    "apply": {
        "wave": "A1",
        "title": "Apply a generation's groups",
        "approach": (
            "One generation's groups planned into production merges - survivor = the "
            "oldest record (first_seen_at, then the lowest id); every refusal recorded with "
            "its reason - and, only when dry_run=0 and inside the autodedup_apply_scope row, "
            "written through merge_property_set with source 'autodedup' and one merge "
            "group per engine group."
        ),
        "tools": [
            "autodedup.apply", "toolkit.property_identity", "autodedup.lane", "GitHub Actions",
            "Postgres (schema autodedup)",
        ],
    },
    "unapply": {
        "wave": "A1",
        "title": "Undo the engine's merges",
        "approach": (
            "Every live merge group one generation, one apply run or one time window applied, "
            "undone newest-first through unmerge_group and marked undone in "
            "autodedup.applied_merges - a group a later engine merge builds on waits for that "
            "one; dry_run=1 lists them."
        ),
        "tools": [
            "autodedup.apply", "toolkit.property_identity", "autodedup.lane", "GitHub Actions",
            "Postgres (schema autodedup)",
        ],
    },
    "export": {
        "wave": "W1",
        "title": "Cohort export",
        "approach": (
            "Dump the ruled cohort as one gzipped JSONL artifact — scrubbed descriptions, "
            "hashed broker keys, location, price paths, pHash and float16 CLIP vectors — so "
            "blocking, features, model fitting and clustering all iterate locally at $0."
        ),
        "tools": [
            "autodedup.export", "autodedup.cohort", "autodedup.lane", "GitHub Actions",
            "gzip JSONL artifact",
        ],
    },
}

ACTIONS_RUN_URL = iterations.ACTIONS_RUN_URL


def parse_kv_args(raw: str | None) -> dict[str, str]:
    """`"a=1, b=x:y"` -> `{"a": "1", "b": "x:y"}`. A token without `=` is fatal."""
    out: dict[str, str] = {}
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(f"malformed arg {token!r}; expected key=value")
        key, _, value = token.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"malformed arg {token!r}; empty key")
        if key in out:
            raise ValueError(f"duplicate arg {key!r}")
        out[key] = value.strip()
    return out


SECRET_ENV_VARS: tuple[str, ...] = (
    "SUPABASE_DB_URL", "SUPABASE_DB_SESSION_URL", "OPENAI_API_KEY", "QWEN_API_KEY",
    "RUNPOD_API_KEY", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
)

# A malformed DSN is echoed back verbatim by psycopg (`invalid dsn: ...`), so the value-based
# pass is backed by a shape-based one.
_DSN = re.compile(r"postgres(?:ql)?://[^\s'\"]+", re.IGNORECASE)
_REDACTED = "***"


def scrub(text: str, env: dict[str, str] | None = None) -> str:
    """Replace every credential this lane is handed, and any DSN-shaped run, with `***`."""
    source = os.environ if env is None else env
    for name in SECRET_ENV_VARS:
        value = source.get(name) or ""
        if len(value) >= 8:
            text = text.replace(value, _REDACTED)
    return _DSN.sub(_REDACTED, text)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_conn_factory() -> Any:
    from scraper import db

    return db.connect()


def _run_id() -> str | None:
    return os.environ.get("GITHUB_RUN_ID") or None


def _artifacts() -> dict[str, str] | None:
    run_id = _run_id()
    return {"actions_run": f"{ACTIONS_RUN_URL}{run_id}"} if run_id else None


def _metrics(result: Any) -> dict[str, Any] | None:
    """The handful of keys a mode summary exposes as headline numbers. `sample_stats` keeps
    the whole summary; `metrics` is what the progress page puts in its numbers column."""
    if not isinstance(result, dict):
        return None
    picked = {
        key: result[key]
        for key in ("counts", "timings", "bytes", "drawn", "done", "spent_usd")
        if key in result
    }
    return picked or None


def _ledger_open(
    mode: str, conn_factory: Callable[[], Any], summary: dict[str, Any]
) -> tuple[Any, int | None]:
    """Open this pass's `autodedup.iterations` row. A ledger failure is recorded on the
    summary and never raised: the artifact this lane exists to produce outranks its own
    bookkeeping."""
    meta = ITERATION_META.get(mode)
    if meta is None:
        return None, None
    conn: Any = None
    try:
        conn = conn_factory()
        iteration_id = iterations.start_iteration(
            conn,
            wave=meta["wave"],
            title=meta["title"],
            approach=meta.get("approach"),
            tools=meta.get("tools", ()),
            run_id=_run_id(),
            artifacts=_artifacts(),
        )
        return conn, iteration_id
    except Exception as exc:  # noqa: BLE001 — bookkeeping never fails the lane
        summary["iteration_error"] = scrub(f"{type(exc).__name__}: {exc}")
        # `run()` only sees the connection through the tuple, so a connection opened before
        # the failure is closed HERE or it holds a pooler slot for the whole mode run.
        if conn is not None:
            close = getattr(conn, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 — a closed-connection error is not a result
                    pass
        return None, None


def _spent_usd(result: Any) -> float | None:
    """PROGRAM.md: measured spend, read from `llm_calls` by the lane — never a forecast."""
    if not isinstance(result, dict):
        return None
    value = result.get("spent_usd")
    return float(value) if isinstance(value, (int, float)) else None


def _ledger_close(
    conn: Any, iteration_id: int | None, summary: dict[str, Any], *, status: str
) -> None:
    try:
        iterations.finish_iteration(
            conn,
            iteration_id,
            status=status,
            sample_stats=summary.get("result"),
            metrics=_metrics(summary.get("result")),
            # W3 is the first PAID mode: without this the progress page shows $0 for a run
            # that was billed. None on the free modes keeps the column's existing value.
            cost_usd=_spent_usd(summary.get("result")),
            artifacts=_artifacts(),
            notes=summary.get("error"),
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping never fails the lane
        summary["iteration_error"] = scrub(f"{type(exc).__name__}: {exc}")
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 — a closed-connection error is not a result
                pass


def run(
    mode: str,
    raw_args: str | None,
    out_dir: Path,
    conn_factory: Callable[[], Any] | None = None,
) -> int:
    started = _now()
    summary: dict[str, Any] = {
        "mode": mode,
        "args_raw": raw_args or "",
        "args": {},
        "started_at": started,
        "finished_at": None,
        "ok": False,
        "error": None,
        "result": None,
    }
    out_path = Path(out_dir) / "summary.json"
    factory = conn_factory or _default_conn_factory
    ledger_conn: Any = None
    iteration_id: int | None = None
    try:
        if mode not in MODES:
            raise SystemExit(
                f"unknown mode {mode!r}; known modes: {', '.join(sorted(MODES))}"
            )
        args = parse_kv_args(raw_args)
        summary["args"] = args
        ledger_conn, iteration_id = _ledger_open(mode, factory, summary)
        summary["iteration_id"] = iteration_id
        summary["result"] = MODES[mode](factory, args, Path(out_dir))
        summary["ok"] = True
        code = 0
    except SystemExit as exc:
        summary["error"] = scrub(str(exc))
        code = 2
    except Exception as exc:  # noqa: BLE001 — the summary is the artifact; re-raising loses it
        summary["error"] = scrub(f"{type(exc).__name__}: {exc}")
        summary["traceback"] = scrub(traceback.format_exc())
        code = 1
    if iteration_id is not None:
        _ledger_close(
            ledger_conn, iteration_id, summary, status="done" if summary["ok"] else "failed"
        )
    elif ledger_conn is not None:
        close = getattr(ledger_conn, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 — a closed-connection error is not a result
                pass
    summary["finished_at"] = _now()
    if summary["result"] is None:
        # A mode that failed may ALREADY have written this file with everything that prices the
        # failure — which GPU was rented, how long it booted, what the run had spent. Writing
        # over it leaves the operator an exception string and nothing else, which is the exact
        # shape `judge_lane`'s boot-failure path exists to prevent (it writes the artifact
        # first, then raises). So what it wrote is carried, never dropped.
        partial = _read_json(out_path)
        if partial is not None:
            summary["partial_result"] = partial
    write_json(out_path, summary)
    print(out_path.read_text(encoding="utf-8"))
    return code


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autodedup.lane")
    parser.add_argument("--mode", default="census")
    parser.add_argument("--args", default="")
    parser.add_argument("--out", default="out/")
    ns = parser.parse_args(argv)
    return run(ns.mode, ns.args, Path(ns.out))


if __name__ == "__main__":
    sys.exit(main())
