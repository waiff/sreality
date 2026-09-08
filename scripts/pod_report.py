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

A CRASH LOOP MUST NOT ERASE ITS OWN EVIDENCE (pod `lg5oy1ivlgoyh7`, 2026-09-08 (h)).
This file used to write ONE line, latest-wins, so when RunPod restarted the start command
the second pass's `step=deps ok` overwrote the first pass's `exit=… step=torch` report
before the runner's 60 s poll ever saw it — the one line that would have named the cause.
It now keeps a BOUNDED HISTORY: the last `HISTORY_LIMIT` records as a JSON array under a
single `pod steps [...]` marker, in which the FIRST `exit=` record is never dropped and is
the last tail sacrificed to the size cap. Staleness is answered by the per-record `ts` and
`pass=N`, not by erasure. The history lives in a file on the pod's own disk
(`PODBOOT_HISTORY`), which is what makes it survive the restart it exists to describe.

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

# One line, one row: the lane's UPDATE replaces any previous line carrying this prefix,
# so the note holds the current bounded history and never grows without bound.
LINE_PREFIX = "pod steps "
# The legacy single-record marker. Kept only so a lane's UPDATE and the watchdog can
# still recognise a note written by a pod launched before 2026-09-08 (h).
LEGACY_LINE_PREFIX = "pod step "
# Enough of the log to carry a pip resolver error or a git failure, small enough to sit
# in a note beside the lane's own text.
TAIL_CHARS = 3000
# How many records the note carries. Eight covers a whole bootstrap (start → deps →
# fetch → uv → venv → disk → torch → repo) or two short crash-loop passes.
HISTORY_LIMIT = 8
# The whole line's ceiling. The lane's UPDATE keeps this line intact and truncates the
# rest of the note, so this is what "bounded" actually means.
MAX_LINE_CHARS = 7000


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


def build_record(message: str, *, tail: str = "") -> dict[str, object]:
    """One heartbeat: what was said, when, by which pod, on which interpreter."""
    record: dict[str, object] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "msg": message,
        "pod": os.environ.get("RUNPOD_POD_ID") or socket.gethostname(),
        "python": platform.python_version(),
    }
    if tail:
        record["tail"] = tail
    return record


def _is_exit(record: dict[str, object]) -> bool:
    return "exit=" in str(record.get("msg", ""))


def trim(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """The last `HISTORY_LIMIT` records, except that the FIRST `exit=` report is always
    among them — it names the original cause, and everything after it is a symptom."""
    if len(records) <= HISTORY_LIMIT:
        return list(records)
    first_exit = next((i for i, r in enumerate(records) if _is_exit(r)), None)
    recent = records[-HISTORY_LIMIT:]
    if first_exit is None or records[first_exit] in recent:
        return recent
    return [records[first_exit]] + recent[1:]


def build_line(records: list[dict[str, object]]) -> str:
    """The single line written into the note: `pod steps [<json array>]`, JSON so an
    error tail's newlines and quotes cannot break the one-line-per-note contract.

    Over the size cap the tails go first, OLDEST symptom first — so what survives is the
    original cause and the most recent evidence — and the first `exit=` report's tail is
    the last thing surrendered."""
    kept = [dict(r) for r in trim(records)]
    line = LINE_PREFIX + json.dumps(kept, ensure_ascii=False)
    if len(line) <= MAX_LINE_CHARS:
        return line
    keep = next((i for i, r in enumerate(kept) if _is_exit(r)), None)
    for i in range(len(kept)):
        if len(line) <= MAX_LINE_CHARS:
            return line
        if i == keep:
            continue
        kept[i].pop("tail", None)
        line = LINE_PREFIX + json.dumps(kept, ensure_ascii=False)
    # Only the original cause's tail is left; keep its END, which is where the error is.
    while len(line) > MAX_LINE_CHARS and keep is not None and kept[keep].get("tail"):
        tail = str(kept[keep]["tail"])
        kept[keep]["tail"] = tail[len(tail) // 2:] if len(tail) > 200 else ""
        if not kept[keep]["tail"]:
            kept[keep].pop("tail", None)
        line = LINE_PREFIX + json.dumps(kept, ensure_ascii=False)
    return line


def load_history(path: str) -> list[dict[str, object]]:
    """Previous records, or none. A history we cannot read is not a reason to stop
    reporting — it only costs the record of what came before."""
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 - first call, or a half-written file
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def save_history(path: str, records: list[dict[str, object]]) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - see the module docstring: never fatal
        pass


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
    history_path = os.environ.get("PODBOOT_HISTORY", "")
    record = build_record(" ".join(words), tail=_tail(tail_path) if tail_path else "")
    records = trim(load_history(history_path) + [record])
    save_history(history_path, records)
    line = build_line(records)
    print(f"heartbeat: {json.dumps(record, ensure_ascii=False)[:4000]}")
    print(f"heartbeat: {report(line)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
