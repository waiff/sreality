"""Backfill listings.street_name_key from the stored street.

street_name_key (migration 256) is the dedup street-group NAME key — the bare,
diacritics-folded, decoration-stripped form the dedup engine groups on, stored so
the real-time --dirty drain can SCOPE its eligible load to the claimed properties'
street groups instead of scanning the whole market. Going forward it is stamped at
every street-write path (scraper.db.upsert_listing / write_detail_batch); this
fills the ~existing rows ONCE.

It re-derives the key from data we ALREADY have (the stored `street`) — NO re-fetch,
NO LLM, NO Mapy spend. street_name_key is out of the content hash, so NO snapshot is
written (a data-quality fill, not a new real-world state — same posture as the street
/ coords backfills). The derivation is the ONE source, scraper.street.street_name_key,
so the stored value matches what the engine computes (a parity test guards it).

Idempotent + resumable: keyset-paginates by the PK over the rows still missing a key
(a partial index backs the scan, migration 256), so each chunk is bounded and the
selection self-empties. Rerun until "pending=0". `--all` re-derives EVERY street-bearing
row (use after a street_name_key logic change, not for the initial fill).

Usage:  python -m scripts.backfill_street_name_key [--all] [--limit N] [--max-seconds S]
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

from scraper import db
from scraper.street import street_name_key

LOG = logging.getLogger("backfill_street_name_key")

# Rows that carry a street (so a key is derivable). The default run additionally
# requires street_name_key IS NULL (the not-yet-filled tail, backed by the migration
# 256 partial index); --all drops that to re-derive every street-bearing row.
_BASE_PREDICATE = "l.street IS NOT NULL AND l.street <> ''"
_NULL_PREDICATE = _BASE_PREDICATE + " AND l.street_name_key IS NULL"

_SELECT_SQL = """
    SELECT l.sreality_id, l.street
    FROM listings l
    WHERE {predicate}
      AND l.sreality_id > %(cursor)s
    ORDER BY l.sreality_id
    LIMIT %(chunk)s
"""

# Set-based: one UPDATE per chunk (unnest join on the PK), never a per-row round-trip
# over the pooler. street_name_key is out of the content hash and geom is untouched, so
# no snapshot and no admin-geo re-derive. IS DISTINCT FROM skips rows whose stored key
# already equals the recomputed one — under MVCC an "identical" UPDATE still writes a
# dead tuple (and fires triggers), so a full --all re-key over ~200k mostly-unchanged
# rows would otherwise bloat the table for zero effect.
_UPDATE_SQL = """
    UPDATE listings l
    SET street_name_key = d.key
    FROM unnest(%(ids)s::bigint[], %(keys)s::text[]) AS d(id, key)
    WHERE l.sreality_id = d.id
      AND l.street_name_key IS DISTINCT FROM d.key
"""

_CHUNK = 5000
_CURSOR_MIN = -(10 ** 18)


def process(conn: Any, predicate: str, limit: int, deadline: float | None) -> dict[str, int]:
    sql = _SELECT_SQL.format(predicate=predicate)
    cursor = _CURSOR_MIN
    processed = updated = 0
    while processed < limit:
        if deadline is not None and time.monotonic() > deadline:
            LOG.info("BACKFILL stopping: --max-seconds reached")
            break
        chunk_size = min(_CHUNK, limit - processed)
        with conn.cursor() as cur:
            cur.execute(sql, {"cursor": cursor, "chunk": chunk_size})
            rows = cur.fetchall()
        if not rows:
            break
        cursor = int(rows[-1][0])
        ids: list[int] = []
        keys: list[str | None] = []
        for sid, street in rows:
            ids.append(int(sid))
            keys.append(street_name_key(street))
        with conn.cursor() as cur:
            cur.execute(_UPDATE_SQL, {"ids": ids, "keys": keys})
            updated += cur.rowcount or 0
        processed += len(rows)
        LOG.info("BACKFILL processed=%d updated=%d cursor=%d", processed, updated, cursor)
    LOG.info("BACKFILL done processed=%d updated=%d", processed, updated)
    return {"processed": processed, "updated": updated}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true",
                        help="Re-derive EVERY street-bearing row (default: only rows "
                             "whose street_name_key is still NULL).")
    parser.add_argument("--limit", type=int, default=10_000_000,
                        help="Max rows processed this run (default: effectively all).")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Wall-clock budget; stop claiming and exit cleanly.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report whether any rows are pending and exit.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    predicate = _BASE_PREDICATE if args.all else _NULL_PREDICATE
    deadline = time.monotonic() + args.max_seconds if args.max_seconds else None

    with db.connect() as conn:
        if args.dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    _SELECT_SQL.format(predicate=predicate),
                    {"cursor": _CURSOR_MIN, "chunk": 1},
                )
                pending = cur.fetchone() is not None
            LOG.info("BACKFILL %s pending=%s",
                     "all" if args.all else "null-only", "yes" if pending else "none")
            return 0
        process(conn, predicate, args.limit, deadline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
