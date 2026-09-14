"""Delete the `location_claims` rows written under a RETIRED contract version.

W6-a of the location simplification sprint. A claim whose `contract_entry_id` belongs to a
`portal_contracts` row with `is_active = false` is invisible to the resolver and has been
since W1-c: `location_data/resolver/resolve_db._ADMISSIBLE_CONTRACT` admits a claim only
when its entry hangs off the newest contract version present for that listing, or when it is
an operator claim (no entry at all). Measured 2026-09-13 that is ~9.5 M of ~13.2 M rows — the large majority of a 5.8 GB
relation, carried by seven indexes, read by nothing.

NO VERDICT MOVES, SO NOTHING IS ENQUEUED. This is the whole difference between this script
and `location_data/contracts.py --retract`, and it is why both exist:

  * `--retract <portal>@<version>` withdraws a version's evidence BECAUSE IT WAS WRONG. The
    resolver was reading those claims a moment ago, so every listing they touched must be
    re-resolved without them: the retraction's DELETE and its `dirty_locations` enqueue are
    one statement, deliberately inseparable. It also stands the header down. Keep using it
    for that. It is not what this script does.
  * This script deletes rows the resolver ALREADY ignores, after the header went down.
    Enqueuing anything here would push ~800 k listings through the resolve drain to
    recompute answers that cannot change — hours of the lane's time for a guaranteed no-op.

THE BACKUP IS THE WORKFLOW'S FIRST STEP, not this module's business. CLAUDE.md rule 1 asks
for a pg_dump equivalent before a destructive change; `location_claims_retire.yml` `\\copy`s
the doomed rows to a gzipped CSV artifact (90-day retention) and only then runs this. Run
this by hand and you are the one who skipped it.

BOUNDED, PAUSED, RESUMABLE — the three properties that make a 9.5 M-row delete a job rather
than an outage on a live table:

  * **Bounded.** 20,000 rows a statement inside its own transaction, `lock_timeout 5s` and
    `statement_timeout 120s`. One atomic DELETE of the whole set would spend its timeout and
    roll back, forever, achieving nothing — the lesson `_RETRACT_BATCH_SQL` already records.
  * **Paused.** 0.5 s between batches. The delete competes with the hourly intake, the `*/15`
    resolve drain and every reader of `location_claims`; a tight loop would hold the table's
    buffers and its seven indexes continuously. The pause is what keeps this polite.
  * **Resumable.** The batch is an id KEYSET (`id > cursor ORDER BY id LIMIT n`), so a killed
    run resumes by re-deriving the same cursor from the same predicate: every row at or below
    the cursor that matched is already gone. A fresh run starts at 0 and the PK index walks
    straight past the deleted entries. `--max-seconds` stops it between batches, never inside
    one, so the budget can never leave a half-applied statement.

Why a keyset on `id` and not `ctid` (which `--retract` uses): `--retract` deletes an entire
entry's rows and never needs to remember where it was — the predicate shrinks under it, so
"the first N matching rows" is always fresh work. Here the survivors (~30 % of the table) stay
matched-against forever, so an unordered LIMIT would re-scan them from the top on every batch.

ONLY PORTAL CLAIMS UNDER RETIRED HEADERS ARE REACHABLE. The doomed set is resolved ONCE at
startup into a list of `portal_contract_entries.id`, and the batch predicate is
`contract_entry_id = ANY(entry_ids)`. An operator claim carries `contract_entry_id IS NULL`
(`location_data/operator_corrections.py`), and NULL is never `= ANY` of anything, so the
operator's own corrections cannot be reached by this module even by mistake.

AND IT WAITS FOR THE RE-MINE (W11, re-aimed in W15). Since the 2026-09-14 blackout the
resolver reads a listing's NEWEST EVIDENCE, so a retired version's rows are live data until
the page has been re-mined under the active one. The rail measures exactly what this delete
would destroy, over EVERY listing: per portal, the listings carrying a claim under one of
the doomed entries, and how many of those have NO claim under that portal's active contract
— i.e. would be left with no evidence at all. Any non-zero number refuses the run (exit 3)
and names the portal. No `--force`.

    python3 -m scripts.location_claims_retire --dry-run
    python3 -m scripts.location_claims_retire --confirm RETIRE
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import psycopg

from scraper.db import connect

LOG = logging.getLogger("location_claims_retire")

DEFAULT_BATCH_ROWS = 20_000
DEFAULT_PAUSE_SECONDS = 0.5
DEFAULT_MAX_SECONDS = 20_000
PROGRESS_EVERY = 10

# The doomed set, resolved once. `is_active` lives on the contract HEADER, so the entry ids
# of every retired version of every portal come back in one small read (~350 of 440 entries
# on 2026-09-13). Resolving once and holding the list for the run is deliberate: a header
# retired while this is walking joins the set on the NEXT run, never mid-walk, so the keyset
# cursor can never skip rows that became doomed behind it.
_RETIRED_ENTRIES_SQL = """
    SELECT pce.id, pc.source, pc.version
      FROM portal_contract_entries pce
      JOIN portal_contracts pc ON pc.id = pce.contract_id
     WHERE NOT pc.is_active
     ORDER BY pc.source, pc.version, pce.id
"""

# THE RE-MINE RAIL (W11, incident 2026-09-14; re-aimed W15). A retired version's claims are
# NOT dead weight while the portal's pages have not been re-mined under the ACTIVE version:
# since W11 the resolver reads a listing's NEWEST EVIDENCE (the highest contract version
# present for that listing that is <= the active one), so an old version's rows are what
# keeps a half-re-mined listing on the map. Deleting them mid-re-mine is exactly the blackout
# W11 fixed, spelled as a DELETE instead of a SELECT — and this one would not self-heal.
#
# IT MEASURES THE DAMAGE, NOT A COHORT. W11 asked the question of `l.is_active` listings,
# which was both too wide (a live listing with no doomed claim is not at risk) and too narrow
# (W15 retires the served set: every listing is in the lane, and a delisted one that loses its
# only evidence is a listing the audit page must then explain for ever). So the driving set is
# the DOOMED CLAIMS themselves — the listings this delete would touch — and the number that
# blocks is how many of them would be left with no claim under their portal's ACTIVE contract.
# Any non-zero count refuses the run and names the portal. There is deliberately no `--force`:
# the answer is to wait for the intake lanes, and a flag would exist only to skip that wait.
_REMINE_GAP_SQL = """
    WITH doomed AS (
        SELECT DISTINCT c.listing_id
          FROM location_claims c
         WHERE c.contract_entry_id = ANY(%(entry_ids)s)
    )
    SELECT l.source, count(*) AS touched,
           count(*) FILTER (WHERE NOT EXISTS (
               SELECT 1 FROM location_claims c
                 JOIN portal_contract_entries pce ON pce.id = c.contract_entry_id
                 JOIN portal_contracts pc ON pc.id = pce.contract_id
                WHERE c.listing_id = l.id AND pc.is_active AND pc.source = l.source
                  AND c.licence_class IN ('portal', 'operator'))) AS awaiting_remine
      FROM doomed d
      JOIN listings l ON l.id = d.listing_id
     GROUP BY l.source
     ORDER BY l.source
"""

# THE COUNT IS A DRY-RUN LUXURY, not a precondition. It walks the whole partial index
# (`location_claims_contract_entry`), which is why the real run never asks for it: the
# batches report what they actually deleted, and a number measured before a 40-minute delete
# would be stale before it finished anyway.
_COUNT_SQL = """
    SELECT count(*) FROM location_claims WHERE contract_entry_id = ANY(%(entry_ids)s)
"""

# ONE STATEMENT PER BATCH: the keyset pick, the delete and the new cursor, so a batch cannot
# half-commit and the cursor cannot advance past rows that survived. `ORDER BY id LIMIT` on
# the primary key is what makes this an early-stopping scan (~70 % of the rows match, so the
# planner reads ~28 k index entries to fill a 20 k batch) instead of a sort of the whole set.
_BATCH_SQL = """
    WITH victims AS (
        SELECT c.id
          FROM location_claims c
         WHERE c.id > %(after_id)s
           AND c.contract_entry_id = ANY(%(entry_ids)s)
         ORDER BY c.id
         LIMIT %(batch_size)s
    ), deleted AS (
        DELETE FROM location_claims c
         USING victims v
         WHERE c.id = v.id
        RETURNING c.id
    )
    SELECT count(*), max(id) FROM deleted
"""

# Per batch, never per run: no single statement may hang a pooler backend, but the loop above
# may run for as long as the corpus needs.
_BATCH_TIMEOUTS_SQL = "SET LOCAL lock_timeout = '5s'"
_BATCH_STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '120s'"


def retired_entry_ids(conn: psycopg.Connection) -> list[tuple[int, str, int]]:
    """Every `portal_contract_entries.id` under a retired header, with its portal+version."""
    with conn.cursor() as cur:
        cur.execute(_RETIRED_ENTRIES_SQL)
        return [(int(r[0]), str(r[1]), int(r[2])) for r in cur.fetchall()]


def remine_gaps(conn: psycopg.Connection, entry_ids: list[int],
                *, timeout: str = "600s") -> list[tuple[str, int, int]]:
    """(portal, listings this delete touches, those with no ACTIVE-contract claim left).

    `SET LOCAL` inside an explicit transaction, for `count_doomed`'s reason: the rail walks
    the doomed set once and must not die on the role's 120 s default."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{timeout}'")
            cur.execute(_REMINE_GAP_SQL, {"entry_ids": entry_ids})
            return [(str(r[0]), int(r[1]), int(r[2])) for r in cur.fetchall()]


def count_doomed(conn: psycopg.Connection, entry_ids: list[int], *,
                 timeout: str = "600s") -> int | None:
    """Exact count of the doomed rows; None when the index walk outruns `timeout`.

    `SET LOCAL` inside an explicit transaction, never a session `SET`: the transaction-mode
    pooler rebinds the backend between transactions, so a session setting made in its own
    implicit transaction need not be there for the statement it was meant to bound.
    """
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = '{timeout}'")
                cur.execute(_COUNT_SQL, {"entry_ids": entry_ids})
                return int(cur.fetchone()[0])
    except psycopg.Error as exc:
        LOG.warning("RETIRE count gave up (%s); the plan stands without it", exc)
        return None


def delete_batches(
    conn: psycopg.Connection,
    *,
    entry_ids: list[int],
    batch_size: int = DEFAULT_BATCH_ROWS,
    pause: float = DEFAULT_PAUSE_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
) -> tuple[int, int, bool]:
    """Delete in id-keyset batches until the set is empty or the budget is gone.

    Returns (rows deleted, batches run, whether the set was emptied).
    """
    started = time.monotonic()
    after_id = 0
    deleted = batches = 0
    while True:
        if time.monotonic() - started >= max_seconds:
            LOG.warning("RETIRE budget spent after %d batches (%d rows); re-run to continue",
                        batches, deleted)
            return deleted, batches, False
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(_BATCH_TIMEOUTS_SQL)
                cur.execute(_BATCH_STATEMENT_TIMEOUT_SQL)
                cur.execute(_BATCH_SQL, {
                    "after_id": after_id, "entry_ids": entry_ids, "batch_size": batch_size,
                })
                batch_deleted, batch_max_id = cur.fetchone()
        batch_deleted = int(batch_deleted)
        # `max(id)` over an empty DELETE is NULL, and that is the only case in which the
        # cursor must NOT move — the walk is over.
        if batch_deleted == 0:
            LOG.info("RETIRE done: %d rows in %d batches, %.0fs",
                     deleted, batches, time.monotonic() - started)
            return deleted, batches, True
        after_id = int(batch_max_id)
        deleted += batch_deleted
        batches += 1
        if batches % PROGRESS_EVERY == 0:
            elapsed = time.monotonic() - started
            LOG.info("RETIRE batch=%d deleted=%d cursor=%d %.0fs (%.0f rows/s)",
                     batches, deleted, after_id, elapsed, deleted / max(elapsed, 1e-6))
        time.sleep(pause)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", default="",
                        help="type RETIRE to arm the delete; anything else is a dry run")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve the doomed set and count it; delete nothing")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_ROWS)
    parser.add_argument("--pause", type=float, default=DEFAULT_PAUSE_SECONDS)
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    armed = args.confirm == "RETIRE" and not args.dry_run
    with connect() as conn:
        entries = retired_entry_ids(conn)
        if not entries:
            LOG.warning("RETIRE no retired contract version has any projected entry; nothing to do")
            return 0
        entry_ids = [e[0] for e in entries]
        versions = sorted({f"{src}@{ver}" for _, src, ver in entries})
        LOG.info("RETIRE plan: %d entries under %d retired versions (%s)",
                 len(entry_ids), len(versions), ", ".join(versions))

        blocked = [g for g in remine_gaps(conn, entry_ids) if g[2] > 0]
        for source, touched, awaiting in blocked:
            LOG.error("RETIRE blocked by %s: %d of the %d listings this delete would touch "
                      "carry no claim under its ACTIVE contract — the retired versions are "
                      "still their only evidence", source, awaiting, touched)
        if armed and blocked:
            LOG.error("RETIRE nothing was deleted; re-run once the intake lanes have "
                      "re-mined those pages")
            return 3

        if not armed:
            n = count_doomed(conn, entry_ids)
            LOG.info("RETIRE dry run: %s claim rows would be deleted, in batches of %d",
                     "unknown (the count outran its timeout)" if n is None else f"{n:,}",
                     args.batch_size)
            LOG.info("RETIRE nothing was deleted (dry run); arm with --confirm RETIRE")
            return 0

        deleted, batches, complete = delete_batches(
            conn, entry_ids=entry_ids, batch_size=args.batch_size,
            pause=args.pause, max_seconds=args.max_seconds,
        )
    LOG.info("RETIRE summary deleted=%d batches=%d complete=%s", deleted, batches, complete)
    return 0 if complete else 2


if __name__ == "__main__":
    sys.exit(main())
