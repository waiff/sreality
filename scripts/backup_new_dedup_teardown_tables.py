"""One-off pg_dump backup of the tables CUTOFF.md marks for drop, to R2.

Run once before the NEW DEDUP teardown migration (docs/design/new-dedup/CUTOFF.md §4/§7 step 4).
Not a recurring job — invoked manually via the new_dedup_teardown_backup workflow_dispatch.
Uses SUPABASE_DB_SESSION_URL (session-mode pooler) because pg_dump needs a stable session,
unlike the transaction-mode pooler the rest of the codebase uses (see the `database` skill).
"""

from __future__ import annotations

import argparse
import gzip
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

from scraper.image_storage import R2Client

LOG = logging.getLogger(__name__)

# CUTOFF.md §4 "Drop" list — base tables with real storage. The listed views/matviews with
# zero rows (dedup_engine_runs_public, dedup_scan_state_public, dedup_engine_flow_public,
# dedup_queue_snapshot_public, dedup_recency_backlog, dedup_label_events) are pure passthroughs
# with nothing to back up; their definitions live in migration history. The two non-empty
# matviews are included since they're cheap and derived-but-not-trivially-recomputable once the
# source engine is gone.
TABLES = (
    "property_identity_candidates",
    "property_identity_candidates_archive",
    "dedup_dirty_properties",
    "dedup_scan_state",
    "dedup_batches",
    "dedup_batch_requests",
    "dedup_engine_runs",
    "dedup_funnel_resolutions_mv",
    "dedup_llm_cost_by_category_mv",
)


def _dump_table(db_url: str, table: str) -> bytes:
    proc = subprocess.run(
        ["pg_dump", db_url, "--no-owner", "--no-privileges", "--table", table],
        capture_output=True,
        check=True,
    )
    return gzip.compress(proc.stdout)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Dump and report sizes without uploading"
    )
    args = parser.parse_args()

    db_url = os.environ.get("SUPABASE_DB_SESSION_URL") or os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        LOG.error("SUPABASE_DB_SESSION_URL / SUPABASE_DB_URL not set")
        return 1

    run_prefix = f"backups/new-dedup-teardown/{datetime.now(timezone.utc):%Y-%m-%d}"
    r2 = None if args.dry_run else R2Client.from_env()

    failures = []
    for table in TABLES:
        try:
            data = _dump_table(db_url, table)
        except subprocess.CalledProcessError as exc:
            LOG.error("FAILED dumping %s: %s", table, exc.stderr.decode(errors="replace"))
            failures.append(table)
            continue
        key = f"{run_prefix}/{table}.sql.gz"
        LOG.info("%s -> %s (%d bytes gzipped)", table, key, len(data))
        if r2 is not None:
            r2.upload_bytes(key, data, content_type="application/gzip")

    if failures:
        LOG.error("Failed tables: %s", ", ".join(failures))
        return 1
    LOG.info("Backup complete: %d tables under %s", len(TABLES), run_prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
