"""Backfill raw_json.broker for existing idnes listings from staged detail HTML.

idnes broker data lives in the detail-page HTML, already staged in portal_raw_pages
(no re-fetch). This reparses that HTML with scraper.broker_idnes.parse_idnes_broker
and writes the broker block into listings.raw_json.broker — which is OUT of the
content hash (_HASH_FIELDS is typed columns only), so it never churns snapshots. The
resolver (scripts.resolve_brokers) then attributes idnes brokers from raw_json.broker
exactly like sreality's raw_json.user.

Keyset-paginated over portal_raw_pages.id, batched set-based UPDATE, autocommit per
batch (a timeout/SIGKILL just resumes from the cursor next run). --max-seconds bounds
the run; re-dispatch until pending=0.

Required env: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from scraper.broker_idnes import parse_idnes_broker

LOG = logging.getLogger("backfill_idnes_brokers")

# Claims staged pages whose listing doesn't yet carry a broker block, so re-runs
# resume cleanly (a SIGKILL/timeout mid-backfill loses no progress). The within-run
# id cursor advances past pages with no listing match / no broker so a single run
# still terminates.
_CLAIM = """
SELECT p.id, p.source_id_native, p.html
FROM portal_raw_pages p
JOIN listings l ON l.source = 'idnes' AND l.source_id_native = p.source_id_native
WHERE p.source = 'idnes' AND p.page_kind = 'detail' AND p.id > %(cursor)s
  AND NOT (l.raw_json ? 'broker')
ORDER BY p.id
LIMIT %(limit)s
"""

# Set-based write: jsonb_set the parsed broker onto the matching idnes listing.
# Direct raw_json UPDATE (not the scrape write path) → no snapshot.
_WRITE = """
UPDATE listings l
SET raw_json = jsonb_set(coalesce(l.raw_json, '{}'::jsonb), '{broker}', x.broker)
FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(native text, broker jsonb)
WHERE l.source = 'idnes' AND l.source_id_native = x.native
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-seconds", type=int, default=3000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    import psycopg

    started = time.monotonic()
    deadline = started + args.max_seconds if args.max_seconds else None
    cursor = 0
    pages = 0
    parsed = 0
    written = 0

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        while True:
            with conn.cursor() as cur:
                cur.execute(_CLAIM, {"cursor": cursor, "limit": args.batch_size})
                batch = cur.fetchall()
            if not batch:
                break
            cursor = int(batch[-1][0])
            pages += len(batch)

            rows: list[dict[str, object]] = []
            for _id, native, html in batch:
                if not native:
                    continue
                broker = parse_idnes_broker(html or "")
                if broker is not None:
                    parsed += 1
                    rows.append({"native": native, "broker": broker})

            if rows and not args.dry_run:
                with conn.cursor() as cur:
                    cur.execute(_WRITE, {"rows": json.dumps(rows)})
                    written += cur.rowcount or 0

            if pages % 5000 == 0:
                LOG.info("BACKFILL progress pages=%d parsed=%d written=%d cursor=%d",
                         pages, parsed, written, cursor)
            if deadline and time.monotonic() > deadline:
                LOG.warning("BACKFILL time budget reached at cursor=%d; resume next run", cursor)
                break

    LOG.info("BACKFILL done pages=%d parsed=%d written=%d elapsed=%.1fs%s",
             pages, parsed, written, time.monotonic() - started,
             " (dry-run)" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
