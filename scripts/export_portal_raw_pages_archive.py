"""Export the portal_raw_pages HTML archive to R2 (location-data program, W0 item 0o).

The ~447k-row / 14 GB archive is the only surviving copy of several portals' best
location signal (remax's subject address line, ceskereality's accented street in
<title>, idnes's no-exact-address disclaimer, realitymix's breadcrumb chain), and
portals do not serve delisted pages again. Migration 099's header called these rows
"safe to delete once parsed"; that policy is superseded — the table is preservation
substrate for the location re-mine wave, deletion is CI-guarded
(tests/test_portal_raw_pages_guard.py), and this script keeps an off-database copy.

Chunked and resumable: keyset pagination over id, NDJSON.gz chunks uploaded to
backups/portal-raw-pages/<snapshot>/, a state.json cursor updated after every chunk,
and a manifest.json written on completion. Re-dispatching the workflow with the same
snapshot date resumes where the previous run stopped; a new date starts a fresh full
export under its own prefix. --verify compares manifest totals against the DB count
at the recorded snapshot boundary.
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


def _download_json(r2: R2Client, key: str) -> dict[str, Any] | None:
    try:
        return json.loads(r2.download_bytes(key))
    except Exception:  # noqa: BLE001 - boto raises ClientError subclasses per backend
        return None


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
    batch_rows: int,
    max_chunks: int | None = None,
) -> int:
    prefix = f"{PREFIX}/{snapshot}"
    state_key = f"{prefix}/state.json"
    state = _download_json(r2, state_key) if r2 is not None else None

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
        while True:
            if max_chunks is not None and chunks_this_run >= max_chunks:
                LOG.info("EXPORT pausing after --max-chunks=%d", max_chunks)
                return 0
            with conn.cursor() as cur:
                cur.execute(
                    _SELECT_SQL,
                    {
                        "after": state["last_exported_id"],
                        "boundary": state["boundary_id"],
                        "limit": batch_rows,
                    },
                )
                rows = cur.fetchall()
            if not rows:
                break

            records = [dict(zip(COLUMNS, row)) for row in rows]
            body = "\n".join(
                json.dumps(rec, ensure_ascii=False, default=_json_default)
                for rec in records
            ).encode()
            gz = gzip.compress(body)
            first_id, last_id = records[0]["id"], records[-1]["id"]
            chunk_key = f"{prefix}/chunk-{first_id:010d}-{last_id:010d}.ndjson.gz"
            LOG.info(
                "CHUNK %s rows=%d raw=%dB gz=%dB",
                chunk_key,
                len(records),
                len(body),
                len(gz),
            )
            if r2 is not None:
                r2.upload_bytes(chunk_key, gz, content_type="application/gzip")
            state["chunks"].append(
                {"key": chunk_key, "rows": len(records), "gz_bytes": len(gz)}
            )
            state["rows"] += len(records)
            state["raw_bytes"] += len(body)
            state["gz_bytes"] += len(gz)
            state["last_exported_id"] = last_id
            if r2 is not None:
                _upload_json(r2, state_key, state)
            chunks_this_run += 1

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
        snapshot,
        db_rows,
        manifest["rows"],
        chunk_rows,
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
        help="Snapshot prefix date (default: today UTC); reuse to resume",
    )
    parser.add_argument("--batch-rows", type=int, default=1000)
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="Stop after N chunks this run (resumable); default: run to completion",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Compare the snapshot manifest against the DB instead of exporting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
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
    return export(db_url, r2, args.snapshot, args.batch_rows, args.max_chunks)


if __name__ == "__main__":
    sys.exit(main())
