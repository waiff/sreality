"""Export the portal_raw_pages HTML archive to R2 (location-data program, W0 item 0o).

The ~447k-row / 14 GB archive is the only surviving copy of several portals' best
location signal (remax's subject address line, ceskereality's accented street in
<title>, idnes's no-exact-address disclaimer, realitymix's breadcrumb chain), and
portals do not serve delisted pages again. Migration 099's header called these rows
"safe to delete once parsed"; that policy is superseded — the table is preservation
substrate for the location re-mine wave, deletion is CI-guarded
(tests/test_portal_raw_pages_guard.py), and this script keeps an off-database copy.

Chunked by BYTE budget (not row count — index-archive rows can be ~60x wider than
detail rows), resumable: keyset pagination over id, NDJSON.gz chunks uploaded to
backups/portal-raw-pages/<snapshot>/, a state.json cursor updated after every chunk,
and a manifest.json written on completion. Re-dispatching the workflow with the same
snapshot date resumes where the previous run stopped; a transient R2 error reading
state.json FAILS the run rather than silently restarting from id 0 (a
review-confirmed hazard). --verify compares manifest totals against the DB count at
the recorded snapshot boundary.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

import psycopg

from scraper.image_storage import R2Client

LOG = logging.getLogger(__name__)

PREFIX = "backups/portal-raw-pages"
COLUMNS = (
    "id",
    "source",
    "source_id_native",
    "source_url",
    "page_kind",
    "html",
    "http_status",
    "fetched_at",
    "parsed_at",
    "parse_error",
)
_SELECT_SQL = (
    f"SELECT {', '.join(COLUMNS)} FROM portal_raw_pages"
    " WHERE id > %(after)s AND id <= %(boundary)s ORDER BY id LIMIT %(limit)s"
)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unserializable {type(value)!r}")


def _is_not_found(exc: Exception) -> bool:
    code = getattr(exc, "response", None)
    if isinstance(code, dict):  # botocore ClientError.response
        err = (code.get("Error") or {}).get("Code")
        return err in ("NoSuchKey", "404", "NotFound")
    return False


def _download_json(r2: R2Client, key: str) -> dict[str, Any] | None:
    """The object as JSON, or None ONLY when it does not exist.

    Any other failure (R2 5xx, throttle, stream reset) propagates: treating a
    transient read error as "no state yet" would silently restart the 14 GB
    export from id 0 and overwrite the real resume cursor.
    """
    try:
        return json.loads(r2.download_bytes(key))
    except Exception as exc:  # noqa: BLE001 - filtered to not-found right below
        if _is_not_found(exc):
            return None
        raise


def _upload_json(r2: R2Client, key: str, payload: dict[str, Any]) -> None:
    r2.upload_bytes(
        key,
        json.dumps(payload, default=_json_default).encode(),
        content_type="application/json",
    )


def export(
    db_url: str,
    r2: R2Client | None,
    snapshot: str,
    fetch_rows: int,
    chunk_mb: int,
    max_chunks: int | None = None,
) -> int:
    prefix = f"{PREFIX}/{snapshot}"
    state_key = f"{prefix}/state.json"
    state = _download_json(r2, state_key) if r2 is not None else None
    chunk_budget = chunk_mb * 1024 * 1024

    # prepare_threshold=None: safe on both pooler modes (see the `database` skill);
    # the fallback URL is the transaction-mode pooler, where auto-prepare breaks.
    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        if state is None:
            with conn.cursor() as cur:
                cur.execute("SELECT coalesce(max(id), 0) FROM portal_raw_pages")
                boundary = int(cur.fetchone()[0])
            state = {
                "snapshot": snapshot,
                "boundary_id": boundary,
                "last_exported_id": 0,
                "chunks": [],
                "rows": 0,
                "raw_bytes": 0,
                "gz_bytes": 0,
            }
            LOG.info("EXPORT start snapshot=%s boundary_id=%d", snapshot, boundary)
        else:
            LOG.info(
                "EXPORT resume snapshot=%s boundary_id=%d after id=%d (%d rows done)",
                snapshot,
                state["boundary_id"],
                state["last_exported_id"],
                state["rows"],
            )

        chunks_this_run = 0
        buf: list[bytes] = []
        buf_bytes = 0
        buf_rows = 0
        first_id: int | None = None
        last_id = int(state["last_exported_id"])

        def flush() -> None:
            nonlocal buf, buf_bytes, buf_rows, first_id, chunks_this_run
            if not buf:
                return
            body = b"".join(buf)
            gz = gzip.compress(body)
            chunk_key = f"{prefix}/chunk-{first_id:010d}-{last_id:010d}.ndjson.gz"
            LOG.info(
                "CHUNK %s rows=%d raw=%dB gz=%dB",
                chunk_key, buf_rows, len(body), len(gz),
            )
            if r2 is not None:
                r2.upload_bytes(chunk_key, gz, content_type="application/gzip")
            state["chunks"].append(
                {"key": chunk_key, "rows": buf_rows, "gz_bytes": len(gz)}
            )
            state["rows"] += buf_rows
            state["raw_bytes"] += len(body)
            state["gz_bytes"] += len(gz)
            state["last_exported_id"] = last_id
            if r2 is not None:
                _upload_json(r2, state_key, state)
            buf, buf_bytes, buf_rows, first_id = [], 0, 0, None
            chunks_this_run += 1

        done = False
        while not done:
            if max_chunks is not None and chunks_this_run >= max_chunks:
                LOG.info("EXPORT pausing after --max-chunks=%d", max_chunks)
                return 0
            with conn.cursor() as cur:
                cur.execute(
                    _SELECT_SQL,
                    {
                        "after": last_id,
                        "boundary": state["boundary_id"],
                        "limit": fetch_rows,
                    },
                )
                rows = cur.fetchall()
            if not rows:
                done = True
            for row in rows:
                rec = dict(zip(COLUMNS, row))
                # Trailing newline per record: concatenated chunks must
                # decompress to valid NDJSON (the restore path is the
                # load-bearing path for a preservation artifact).
                line = (
                    json.dumps(rec, ensure_ascii=False, default=_json_default)
                    + "\n"
                ).encode()
                if first_id is None:
                    first_id = rec["id"]
                buf.append(line)
                buf_bytes += len(line)
                buf_rows += 1
                last_id = rec["id"]
                if buf_bytes >= chunk_budget:
                    flush()
                    if max_chunks is not None and chunks_this_run >= max_chunks:
                        LOG.info("EXPORT pausing after --max-chunks=%d", max_chunks)
                        return 0
        flush()

    state["completed_at"] = datetime.now(timezone.utc).isoformat()
    if r2 is not None:
        _upload_json(r2, f"{prefix}/manifest.json", state)
    LOG.info(
        "EXPORT done snapshot=%s rows=%d chunks=%d raw=%.2fGB gz=%.2fGB",
        snapshot,
        state["rows"],
        len(state["chunks"]),
        state["raw_bytes"] / 1e9,
        state["gz_bytes"] / 1e9,
    )
    return 0


def verify(db_url: str, r2: R2Client, snapshot: str) -> int:
    prefix = f"{PREFIX}/{snapshot}"
    manifest = _download_json(r2, f"{prefix}/manifest.json")
    if manifest is None:
        LOG.error("VERIFY no manifest at %s/manifest.json (export incomplete?)", prefix)
        return 1
    with psycopg.connect(
        db_url, autocommit=True, prepare_threshold=None
    ) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM portal_raw_pages WHERE id <= %s",
            (manifest["boundary_id"],),
        )
        db_rows = int(cur.fetchone()[0])
    chunk_rows = sum(c["rows"] for c in manifest["chunks"])
    LOG.info(
        "VERIFY snapshot=%s db_rows=%d manifest_rows=%d chunk_sum=%d",
        snapshot, db_rows, manifest["rows"], chunk_rows,
    )
    if db_rows != manifest["rows"] or chunk_rows != manifest["rows"]:
        LOG.error("VERIFY MISMATCH — do not treat the archive export as complete")
        return 1
    LOG.info("VERIFY OK — every archived row at the snapshot boundary is in R2")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        default=f"{datetime.now(timezone.utc):%Y-%m-%d}",
        help="Snapshot prefix date (default: today UTC); reuse to resume. The "
        "workflow resolves this at dispatch time so a re-run across UTC "
        "midnight cannot silently start a second full export.",
    )
    parser.add_argument(
        "--fetch-rows", "--batch-rows", dest="fetch_rows", type=int, default=200,
        help="Rows per keyset SELECT (memory-bounded; chunking is by bytes)",
    )
    parser.add_argument(
        "--chunk-mb", type=int, default=48,
        help="Raw NDJSON bytes per uploaded chunk (gz is ~5-10x smaller)",
    )
    parser.add_argument(
        "--max-chunks", type=int, default=None,
        help="Stop after N chunks this run (resumable); default: run to completion",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Compare the snapshot manifest against the DB instead of exporting",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read + chunk without uploading (no state, not resumable)",
    )
    args = parser.parse_args()

    db_url = os.environ.get("SUPABASE_DB_SESSION_URL") or os.environ.get(
        "SUPABASE_DB_URL"
    )
    if not db_url:
        LOG.error("SUPABASE_DB_SESSION_URL / SUPABASE_DB_URL not set")
        return 1

    if args.verify:
        return verify(db_url, R2Client.from_env(), args.snapshot)
    r2 = None if args.dry_run else R2Client.from_env()
    return export(db_url, r2, args.snapshot, args.fetch_rows, args.chunk_mb, args.max_chunks)


if __name__ == "__main__":
    sys.exit(main())
