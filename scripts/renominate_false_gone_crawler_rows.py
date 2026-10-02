"""Send crawler listings a sreality check falsely delisted for a page check on their own portal.

Operator decision 2026-10-02 (decision 2, option 1). Dry run is the default; `--apply`
enqueues. Dispatch through `renominate_false_gone.yml`.

THE DEFECT. A crawler listing (bazos, idnes, ...) carries a synthetic NEGATIVE
`sreality_id`. Until item 3 (#1679) a crawler image's 404 ran a SREALITY freshness fetch
of that synthetic id; sreality answered "not found", `scraper/freshness.py` flipped the row
and logged `listing_freshness_checks.outcome = 'gone'`. That verdict says nothing about the
listing's own portal. Item 3 closed the path; this repairs the rows it left behind.

THE SELECTION (`FALSE_GONE_SQL`): inactive, `source <> 'sreality'`, `sreality_id < 0`, a
'gone' freshness check on that id, `inactive_at` within one hour of that check, and no
sighting since the flip (`last_seen_at <= inactive_at`). The window is two-sided on
purpose: the old writer flipped the row and THEN logged the check, each in its own
autocommit transaction, so the flip's `inactive_at` precedes `checked_at` by milliseconds.
Two kinds of row are counted and skipped: one already in `listing_detail_queue` (the drain
has it; a given-up row stays the walk re-arm's business), and one its own portal's drain has
since read as gone (`detail_queue_completions`, after the verdict), which also keeps a re-run
from re-nominating rows this job already had checked.

THE WRITE. Each eligible row goes into the queue at `QUEUE_PRIORITY_VERIFY` through
`db.enqueue_detail` -- the exact row `enqueue_presence_checks` writes for a walk's
nomination, so the drain treats it as a presence check: the VERIFY claim reserve, exempt
from the gone-rate breaker. Not `enqueue_presence_checks` itself: its budget is a fraction
of a walk scope's ACTIVE rows and every call re-arms 50 unrelated given-up rows and records
a deferral; this job's bound is `--limit` per source, written in `--batch-size` batches.

WHAT THE DRAIN THEN DOES (rule 3: never deleted, flips stay page-verified). A live page is
an ordinary successful detail write: `listing_write` sets `is_active = true`,
`inactive_at = NULL`, `last_seen_at = now()` and appends a snapshot if the content changed.
A gone page calls `db.mark_listing_inactive`, which matches only `is_active = true`, so the
row stays inactive with its original `inactive_at`.

Measured 2026-10-01 (read-only): 7,023 crawler rows carry the sreality 'gone' verdict;
5,884 are inactive; 2,061 match the flip signature.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from scraper import db

LOG = logging.getLogger("renominate_false_gone_crawler_rows")

FALSE_GONE_SQL = """
WITH verdict AS (
    SELECT l.id AS listing_id, max(f.checked_at) AS verdict_at
      FROM listing_freshness_checks f
      JOIN listings l ON l.sreality_id = f.sreality_id
     WHERE f.outcome = 'gone'
       AND f.sreality_id < 0
       AND l.source <> 'sreality'
       AND l.is_active = false
       AND l.inactive_at BETWEEN f.checked_at - interval '1 hour'
                             AND f.checked_at + interval '1 hour'
     GROUP BY l.id
),
portal_gone AS (
    SELECT c.source, c.native_id, max(c.completed_at) AS gone_at
      FROM detail_queue_completions c
     WHERE c.outcome = 'gone'
       AND c.source <> 'sreality'
     GROUP BY c.source, c.native_id
)
SELECT l.source,
       l.source_id_native,
       l.source_url,
       l.price_czk,
       EXISTS (SELECT 1 FROM listing_detail_queue q
                WHERE q.source = l.source
                  AND q.native_id = l.source_id_native) AS queued,
       COALESCE(pg.gone_at > v.verdict_at, false) AS rechecked
  FROM verdict v
  JOIN listings l ON l.id = v.listing_id
  LEFT JOIN portal_gone pg
         ON pg.source = l.source AND pg.native_id = l.source_id_native
 WHERE l.source_id_native IS NOT NULL
   AND (l.last_seen_at IS NULL OR l.last_seen_at <= l.inactive_at)
   AND (%(source)s::text IS NULL OR l.source = %(source)s::text)
 ORDER BY l.source, l.inactive_at DESC, l.source_id_native
"""

DEFAULT_BATCH_SIZE = 500


@dataclass
class SourceTally:
    false_gone: int = 0
    already_queued: int = 0
    rechecked_gone: int = 0
    eligible: list[tuple[str, str | None, int | None]] = field(default_factory=list)
    taken: int = 0
    enqueued: int = 0


def select(conn: Any, source: str | None) -> dict[str, SourceTally]:
    """The false-gone set, tallied per source; `eligible` holds (native_id, detail_ref, price)."""
    tallies: dict[str, SourceTally] = {}
    seen: set[tuple[str, str]] = set()
    with conn.cursor() as cur:
        cur.execute(FALSE_GONE_SQL, {"source": source})
        rows = cur.fetchall()
    for src, native_id, source_url, price, queued, rechecked in rows:
        key = (str(src), str(native_id))
        if key in seen:
            continue
        seen.add(key)
        t = tallies.setdefault(key[0], SourceTally())
        t.false_gone += 1
        if queued:
            t.already_queued += 1
        elif rechecked:
            t.rechecked_gone += 1
        else:
            t.eligible.append((key[1], db.detail_ref(key[0], source_url), price))
    return tallies


def enqueue(conn: Any, source: str, rows: list[tuple[str, str | None, int | None]],
            *, batch_size: int) -> int:
    """Queue `rows` as presence checks, `batch_size` per statement batch."""
    total = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        n = db.enqueue_detail(
            conn, source,
            [(nid, ref, price, db.QUEUE_PRIORITY_VERIFY) for nid, ref, price in batch],
        )
        total += n
        LOG.debug("RENOMINATE source=%s batch=%d..%d queued=%d",
                  source, start, start + len(batch), n)
    return total


def run(conn: Any, *, apply: bool, limit: int | None, batch_size: int,
        source: str | None = None) -> dict[str, SourceTally]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive (omit it for no limit)")
    tallies = select(conn, source)
    for src in sorted(tallies):
        t = tallies[src]
        take = t.eligible if limit is None else t.eligible[:limit]
        t.taken = len(take)
        if apply:
            t.enqueued = enqueue(conn, src, take, batch_size=batch_size)
        LOG.info(
            "RENOMINATE source=%s false_gone=%d already_queued=%d rechecked_gone=%d "
            "eligible=%d %s=%d",
            src, t.false_gone, t.already_queued, t.rechecked_gone, len(t.eligible),
            "enqueued" if apply else "would_enqueue", t.enqueued if apply else t.taken,
        )
    return tallies


def _positive_int(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return n


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="enqueue. Without it the run only counts.")
    ap.add_argument("--limit", type=_positive_int, default=None,
                    help="max rows enqueued PER SOURCE this run, newest flip first "
                         "(default: every eligible row)")
    ap.add_argument("--source", default=None,
                    help="one crawler source (default: every source but sreality)")
    ap.add_argument("--batch-size", type=_positive_int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("--verbose", action="store_true")
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.source == "sreality":
        print("ERROR: sreality rows are not crawler rows; nothing to re-nominate.",
              file=sys.stderr)
        return 2
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    with db.connect() as conn:
        tallies = run(conn, apply=args.apply, limit=args.limit,
                      batch_size=args.batch_size, source=args.source or None)

    done = sum(t.enqueued if args.apply else t.taken for t in tallies.values())
    LOG.info(
        "RENOMINATE TOTAL sources=%d false_gone=%d eligible=%d %s=%d%s",
        len(tallies), sum(t.false_gone for t in tallies.values()),
        sum(len(t.eligible) for t in tallies.values()),
        "enqueued" if args.apply else "would_enqueue", done,
        "" if args.apply else " (dry run -- nothing was written)",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
