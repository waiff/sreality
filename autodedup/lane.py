"""The autodedup lane: one dispatch-only entry point behind `.github/workflows/autodedup.yml`.

    python3 -m autodedup.lane --mode census --args "min_n=800,top=60" --out out/
    python3 -m autodedup.lane --mode probes --args "source=remax" --out out/

`probes` carries the corpus-wide measurements (ingest rate, one portal's location posture)
that are block-independent, so the census never pays for them once per block.

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
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autodedup.census import run_census, run_probes, write_json

Mode = Callable[[Callable[[], Any], dict[str, str], Path], dict[str, Any]]

MODES: dict[str, Mode] = {
    "census": run_census,
    "probes": run_probes,
}


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
    try:
        if mode not in MODES:
            raise SystemExit(
                f"unknown mode {mode!r}; known modes: {', '.join(sorted(MODES))}"
            )
        args = parse_kv_args(raw_args)
        summary["args"] = args
        summary["result"] = MODES[mode](conn_factory or _default_conn_factory, args, Path(out_dir))
        summary["ok"] = True
        code = 0
    except SystemExit as exc:
        summary["error"] = scrub(str(exc))
        code = 2
    except Exception as exc:  # noqa: BLE001 — the summary is the artifact; re-raising loses it
        summary["error"] = scrub(f"{type(exc).__name__}: {exc}")
        summary["traceback"] = scrub(traceback.format_exc())
        code = 1
    summary["finished_at"] = _now()
    write_json(out_path, summary)
    print(out_path.read_text(encoding="utf-8"))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autodedup.lane")
    parser.add_argument("--mode", default="census")
    parser.add_argument("--args", default="")
    parser.add_argument("--out", default="out/")
    ns = parser.parse_args(argv)
    return run(ns.mode, ns.args, Path(ns.out))


if __name__ == "__main__":
    sys.exit(main())
