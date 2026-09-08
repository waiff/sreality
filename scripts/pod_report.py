"""The step reporter a rented pod runs DURING its bootstrap, before the repo exists.

WHY THIS FILE IS NOT PART OF THE REPO THE POD CHECKS OUT: it has to work when the
checkout is exactly what failed. `scripts/pod_bootstrap.py` embeds this source verbatim
into the start command's heredoc, so it is on the box before `git fetch` runs and it
runs on the IMAGE's own Python (3.10) — never the 3.12 venv the bootstrap builds later,
which does not have `psycopg` and may not exist.

THE BLINDNESS THIS ENDS, paid for twice on 2026-09-08 (pods `u1yvcktjn6dbrt` ~$0.50 and
`bsg9k5ee9y6jcm` ~$0.08): RunPod's Pod logs endpoint answers 400, and the first
heartbeat used to be written by the payload AFTER the whole bootstrap succeeded. So "the
2 GB torch download is slow" and "the clone is dead" looked identical from the runner,
and the watchdog's verdict could only ever be "the pod never reported Python running".
Now every step reports, and the EXIT trap ships the failing step plus the tail of the
bootstrap log into the same row.

GENERIC BY CONSTRUCTION. It knows no table: `HEARTBEAT_SQL` carries an UPDATE with
`%(note)s` and `%(run_id)s` placeholders and `HEARTBEAT_RUN_ID` names the row, both
delivered in the pod's REST-body env by whichever lane launched it. With either absent
it prints and exits 0 — an un-wired lane loses the heartbeat, never the bootstrap.

NOTHING HERE MAY FAIL THE BOOTSTRAP. Every path returns 0; the caller adds `|| true`
anyway. A heartbeat that kills the job it is reporting on would be worse than blindness.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
from datetime import datetime, timezone

# One line, one row, latest-wins: the lane's UPDATE replaces any previous line carrying
# this prefix, so the note holds the newest step and never grows without bound.
LINE_PREFIX = "pod step "
# Enough of the log to carry a pip resolver error or a git failure, small enough to sit
# in a note beside the lane's own text.
TAIL_CHARS = 3000


def _tail(path: str, limit: int = TAIL_CHARS) -> str:
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(-limit * 4, os.SEEK_END)
            except OSError:
                fh.seek(0)
            return fh.read().decode("utf-8", "replace")[-limit:]
    except Exception as exc:  # noqa: BLE001 - a missing log is itself the report
        return f"<log unreadable: {exc!r}>"


def build_line(message: str, *, tail: str = "") -> str:
    """The single line written into the note. JSON so an error tail's newlines and
    quotes cannot break the one-line-per-heartbeat contract the UPDATE relies on."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "msg": message,
        "pod": os.environ.get("RUNPOD_POD_ID") or socket.gethostname(),
        "python": platform.python_version(),
    }
    if tail:
        record["tail"] = tail
    return LINE_PREFIX + json.dumps(record, ensure_ascii=False)


def report(line: str) -> str:
    """Write the line where the lane said, or explain why it could not. Never raises."""
    sql = os.environ.get("HEARTBEAT_SQL")
    run_id = os.environ.get("HEARTBEAT_RUN_ID")
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not sql or not run_id or not db_url:
        return "not reported (HEARTBEAT_SQL/HEARTBEAT_RUN_ID/SUPABASE_DB_URL unset)"
    try:
        import psycopg

        with psycopg.connect(db_url, autocommit=True, prepare_threshold=None,
                             connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"note": line,
                                  "run_id": int(run_id) if run_id.isdigit() else run_id})
        return "reported"
    except Exception as exc:  # noqa: BLE001 - see the module docstring: never fatal
        return f"report FAILED (ignored): {exc!r}"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    tail_path = ""
    words: list[str] = []
    while args:
        arg = args.pop(0)
        # The tail is read from the file HERE rather than interpolated by the shell: a
        # bootstrap log holds quotes, backticks and half-written bytes.
        if arg == "--tail-file" and args:
            tail_path = args.pop(0)
        else:
            words.append(arg)
    line = build_line(" ".join(words), tail=_tail(tail_path) if tail_path else "")
    print(f"heartbeat: {line[:4000]}")
    print(f"heartbeat: {report(line)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
