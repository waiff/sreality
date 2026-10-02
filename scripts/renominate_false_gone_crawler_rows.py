"""Send crawler listings a sreality check falsely delisted for a page check on their own portal.

Operator decision 2026-10-02 (decision 2, option 1). Dry run is the default; `--apply`
enqueues; `--readout-since` reads back what the drains made of it. Dispatch through
`renominate_false_gone.yml`.

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
read as gone since the verdict (`detail_queue_completions`). That ledger keeps 7 days
(`db.COMPLETION_RETENTION_DAYS`, pruned at every drain start), so the skip covers only rows
checked in the last 7 days: a pass run later re-fetches the pages an earlier pass already
read as gone. Harmless -- the row is already inactive and `mark_listing_inactive` leaves it
alone -- but wasted fetches, so finish every pass within a week of the first.

THE COUNT IS AN UPPER BOUND, NOT THE NUMBER OF FALSE DELISTS. The old writer had no
`is_active` guard, so it also re-stamped `inactive_at` on rows their own portal had closed
long before; image downloads include inactive listings, and a removed listing's images
usually 404, so those rows carry the same one-hour signature. Nothing tells the two apart
(and a row still on its portal's index would already have healed on the next walk sighting).
For a long-closed row the check is a no-op, but it is the first time the VERIFY lane reads
pages removed weeks ago, and three portals answer a removed URL with HTTP 200 (the
ceskereality archive, realitymix, mmreality's substitute cards) whose gone markers were
proven only on fresh removals. A parser misreading one as live reactivates the row, and every
later check misreads it the same way. Hence the default `--limit 25` per source: pilot, then
`--readout-since` (below) before releasing the rest with `--limit all`.

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
row stays inactive with its original `inactive_at`. A page that errors five times leaves a
given-up queue row that only the walk re-arm (`enqueue_presence_checks`, 50 per source per
walk, no `is_active` check) ever touches again -- an inactive row would cycle there for good.

THE READOUT (`--readout-since`, read-only; the timestamp an apply run prints). Per source:
the job's queue rows still pending / erroring / given up, and its completions -- written,
reactivated, written WITHOUT moving `last_seen_at` (must be 0), gone, gave up. Then each
reactivated row with its `raw_json` key count and price now against its last snapshot from
before the check, so an archive or substitute card read as a live page stands out (fewer
keys, other price). Release the rest only if `written_unseen` is 0, the reactivated rows
look like detail pages, and the erroring + given-up share is negligible (else first make
the re-arm skip inactive listings). Completions keep 7 days, so read it back within a week.

Measured 2026-10-01 (read-only): 7,023 crawler rows carry the sreality 'gone' verdict;
5,884 are inactive; 2,061 match the flip signature -- the upper bound above.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

# The job's rows after the fact: crawler rows carrying the sreality 'gone' verdict whose
# VERIFY queue row was enqueued at or after the apply. Not the signature -- a reactivated
# row's inactive_at is NULL by then.
PILOT_SUMMARY_SQL = """
WITH job_rows AS (
    SELECT DISTINCT l.source, l.source_id_native, l.is_active, l.last_seen_at
      FROM listing_freshness_checks f
      JOIN listings l ON l.sreality_id = f.sreality_id
     WHERE f.outcome = 'gone'
       AND f.sreality_id < 0
       AND l.source <> 'sreality'
       AND l.source_id_native IS NOT NULL
       AND (%(source)s::text IS NULL OR l.source = %(source)s::text)
),
done AS (
    SELECT j.source,
           count(*) FILTER (WHERE c.outcome = 'written') AS written,
           count(*) FILTER (WHERE c.outcome = 'written' AND j.is_active) AS reactivated,
           count(*) FILTER (WHERE c.outcome = 'written'
                              AND (j.last_seen_at IS NULL
                                   OR j.last_seen_at < c.enqueued_at)) AS written_unseen,
           count(*) FILTER (WHERE c.outcome = 'gone') AS gone,
           count(*) FILTER (WHERE c.outcome = 'given_up') AS gave_up
      FROM job_rows j
      JOIN detail_queue_completions c
        ON c.source = j.source AND c.native_id = j.source_id_native
     WHERE c.priority = %(verify)s
       AND c.enqueued_at >= %(since)s
     GROUP BY j.source
),
waiting AS (
    SELECT j.source,
           count(*) FILTER (WHERE NOT q.given_up AND q.attempts = 0) AS pending,
           count(*) FILTER (WHERE NOT q.given_up AND q.attempts > 0) AS erroring,
           count(*) FILTER (WHERE q.given_up) AS given_up
      FROM job_rows j
      JOIN listing_detail_queue q
        ON q.source = j.source AND q.native_id = j.source_id_native
     WHERE q.priority = %(verify)s
       AND q.enqueued_at >= %(since)s
     GROUP BY j.source
)
SELECT COALESCE(d.source, w.source) AS source,
       COALESCE(w.pending, 0), COALESCE(w.erroring, 0), COALESCE(w.given_up, 0),
       COALESCE(d.written, 0), COALESCE(d.reactivated, 0), COALESCE(d.written_unseen, 0),
       COALESCE(d.gone, 0), COALESCE(d.gave_up, 0)
  FROM done d
  FULL JOIN waiting w ON w.source = d.source
 ORDER BY 1
"""

# Each reactivated job row against its last snapshot from before the check, fewest-keys
# delta first: an archive page or substitute card parsed as live carries fewer keys.
PILOT_REACTIVATED_SQL = """
SELECT r.source, r.native_id, r.source_url, r.keys_now, r.keys_before,
       r.price_now, r.price_before
  FROM (
    SELECT l.source,
           l.source_id_native AS native_id,
           l.source_url,
           CASE WHEN jsonb_typeof(l.raw_json) = 'object'
                THEN (SELECT count(*) FROM jsonb_object_keys(l.raw_json)) END AS keys_now,
           CASE WHEN jsonb_typeof(b.raw_json) = 'object'
                THEN (SELECT count(*) FROM jsonb_object_keys(b.raw_json)) END AS keys_before,
           l.price_czk AS price_now,
           b.price_czk AS price_before
      FROM (SELECT c.source, c.native_id, min(c.enqueued_at) AS enqueued_at
              FROM detail_queue_completions c
             WHERE c.outcome = 'written'
               AND c.priority = %(verify)s
               AND c.enqueued_at >= %(since)s
               AND c.source <> 'sreality'
               AND (%(source)s::text IS NULL OR c.source = %(source)s::text)
             GROUP BY c.source, c.native_id) c
      JOIN listings l
        ON l.source = c.source AND l.source_id_native = c.native_id
      LEFT JOIN LATERAL (
            SELECT s.raw_json, s.price_czk
              FROM listing_snapshots s
             WHERE s.listing_id = l.id
               AND s.scraped_at < c.enqueued_at
             ORDER BY s.scraped_at DESC
             LIMIT 1
           ) b ON true
     WHERE l.is_active = true
       AND l.sreality_id < 0
       AND EXISTS (SELECT 1 FROM listing_freshness_checks f
                    WHERE f.sreality_id = l.sreality_id AND f.outcome = 'gone')
  ) r
 ORDER BY r.source, r.keys_now - r.keys_before NULLS FIRST, r.native_id
 LIMIT %(max_rows)s
"""

DEFAULT_BATCH_SIZE = 500
DEFAULT_LIMIT = 25
READOUT_MAX_ROWS = 200


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
        raise ValueError("limit must be positive (None for no limit)")
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


@dataclass(frozen=True)
class SourceReadout:
    pending: int
    erroring: int
    given_up: int
    written: int
    reactivated: int
    written_unseen: int
    gone: int
    gave_up: int


def readout(conn: Any, *, since: datetime, source: str | None = None,
            max_rows: int = READOUT_MAX_ROWS) -> dict[str, SourceReadout]:
    """Log what the drains made of the rows this job queued at or after `since`; read-only."""
    if since < datetime.now(timezone.utc) - timedelta(days=db.COMPLETION_RETENTION_DAYS):
        LOG.warning("READOUT since=%s is older than the %d-day completion ledger; "
                    "written/gone/gave_up undercount", since.isoformat(),
                    db.COMPLETION_RETENTION_DAYS)
    params = {"since": since, "source": source, "verify": db.QUEUE_PRIORITY_VERIFY}
    with conn.cursor() as cur:
        cur.execute(PILOT_SUMMARY_SQL, params)
        summary = cur.fetchall()
        cur.execute(PILOT_REACTIVATED_SQL, {**params, "max_rows": max_rows})
        reactivated = cur.fetchall()
    out: dict[str, SourceReadout] = {}
    for src, *counts in summary:
        r = out[str(src)] = SourceReadout(*(int(n) for n in counts))
        LOG.info(
            "READOUT source=%s pending=%d erroring=%d given_up=%d written=%d "
            "reactivated=%d written_unseen=%d gone=%d gave_up=%d",
            src, r.pending, r.erroring, r.given_up, r.written, r.reactivated,
            r.written_unseen, r.gone, r.gave_up,
        )
        if r.written_unseen:
            LOG.warning("READOUT source=%s written_unseen=%d -- a 'written' completion left "
                        "last_seen_at unmoved; investigate before releasing more",
                        src, r.written_unseen)
    checked = sum(r.pending + r.erroring + r.given_up + r.written + r.gone
                  for r in out.values())
    failing = sum(r.erroring + r.given_up for r in out.values())
    LOG.info("READOUT TOTAL rows=%d reactivated=%d gone=%d erroring_or_given_up=%d (%.1f%%)",
             checked, sum(r.reactivated for r in out.values()),
             sum(r.gone for r in out.values()), failing,
             100.0 * failing / checked if checked else 0.0)
    for src, nid, url, keys_now, keys_before, price_now, price_before in reactivated:
        fewer = keys_now is None or keys_before is None or keys_now < keys_before
        LOG.info("READOUT ROW source=%s native_id=%s keys_now=%s keys_before=%s "
                 "price_now=%s price_before=%s%s url=%s",
                 src, nid, keys_now, keys_before, price_now, price_before,
                 " CHECK" if fewer else "", url)
    if len(reactivated) >= max_rows:
        LOG.info("READOUT ROW list cut at %d rows (fewest-keys delta first)", max_rows)
    return out


def _positive_int(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return n


def _limit(value: str) -> int | None:
    return None if value.strip().lower() == "all" else _positive_int(value)


def _utc_timestamp(value: str) -> datetime:
    try:
        ts = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {value!r}") from exc
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="enqueue. Without it the run only counts.")
    ap.add_argument("--limit", type=_limit, default=DEFAULT_LIMIT,
                    help=f"max rows enqueued PER SOURCE this run, newest flip first, or "
                         f"'all' (default: {DEFAULT_LIMIT}, the pilot)")
    ap.add_argument("--source", default=None,
                    help="one crawler source (default: every source but sreality)")
    ap.add_argument("--readout-since", type=_utc_timestamp, default=None,
                    help="read back the rows queued at or after this ISO timestamp (UTC "
                         "when no offset); read-only, no selection, no enqueue")
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
    if args.apply and args.readout_since is not None:
        print("ERROR: --readout-since only reads; run it without --apply.", file=sys.stderr)
        return 2
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    if args.readout_since is not None:
        with db.connect() as conn:
            readout(conn, since=args.readout_since, source=args.source or None)
        return 0

    # Floored to the minute: the readout's lower bound must not pass the DB's enqueued_at.
    started = datetime.now(timezone.utc).replace(second=0, microsecond=0)
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
    if args.apply and done:
        LOG.info("RENOMINATE READBACK once the drains have run, dispatch with "
                 "readout_since=%s (within %d days)",
                 started.strftime("%Y-%m-%dT%H:%MZ"), db.COMPLETION_RETENTION_DAYS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
