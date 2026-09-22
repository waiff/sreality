"""Read-your-writes patch of the Browse read model (`browse_list`).

`browse_list` (migration 276) is an UNLOGGED snapshot of `browse_projection`,
rebuilt wholesale every 15 min by pg_cron (`rebuild_browse_list`, migration 277;
the cadence moved 5 -> 15 min in migration 413, which cut the rebuild's duty
cycle from 142% to 79.7%). That cadence fits organic scrape churn but not an
operator-initiated identity change: a merge / unmerge / split must show in
Browse the instant the API returns, not up to a rebuild-interval later (the
"merge did nothing, then fixed itself after ~2 min" report — docs/design/browse-merge-consistency.md). This
patches exactly the touched rows; the periodic rebuild stays the backstop.

Second caller since W6 (docs/design/field-capture/PROGRAM.md, A15): the dirty-set
maintenance drain, for the properties it just recomputed. Same argument, different
writer — a post-publication attribute fill reaches `properties` in ~2 min and then
waited a measured 11.7 min on average (94 rebuilds over 24 h; worst 36.6) for the
wholesale rebuild to carry it into Browse. It is a FAST PATH, not a guarantee: a
rebuild snapshots `browse_projection` at its start and swaps the new table in at its
end, so a patch committed inside that window writes a table that is about to be
dropped and is simply superseded (283 succeeded rebuilds / 72 h, mean 237 s against a
900 s cadence = in flight ~26% of wall-clock).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import psycopg

LOG = logging.getLogger(__name__)


def sync_browse_list(conn: psycopg.Connection, property_ids: Iterable[int]) -> None:
    """Re-materialize these properties' `browse_list` rows to match `properties` now.

    DELETE + re-INSERT FROM browse_projection so an id that no longer matches the
    projection (retired by a merge, or gate-hidden) simply doesn't reappear — one
    call is correct for survivor and retired alike, no special-casing. Best-effort
    by design: `browse_list` is a disposable cache, so a patch failure must never
    abort the caller's write — it takes its OWN transaction (a SAVEPOINT where the
    caller already holds one, a real BEGIN/COMMIT on the autocommit maintenance
    drain) and, on any DB error, rolls back only itself, logs, and lets the write
    commit; the next rebuild reconciles. Idempotent, so a patch superseded by a
    concurrent blue-green swap is a harmless no-op.
    """
    ids = list(dict.fromkeys(int(p) for p in property_ids))
    if not ids:
        return
    try:
        # Own transaction, so a failure here unwinds only the patch, never the
        # merge/link/recompute it follows.
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM browse_list WHERE property_id = ANY(%s)", (ids,))
            # DO NOTHING, not a bare INSERT: two patches of the same id from different
            # connections serialize on the row lock, and under READ COMMITTED the loser's
            # DELETE cannot see the winner's fresh row — so its INSERT would hit
            # browse_list_pk and discard the whole patch (a merge's, for up to a rebuild
            # interval). The winner read the same projection, so its row is the answer.
            cur.execute(
                "INSERT INTO browse_list "
                "SELECT * FROM browse_projection WHERE property_id = ANY(%s) "
                "ON CONFLICT (property_id) DO NOTHING",
                (ids,),
            )
    except psycopg.Error as exc:
        LOG.warning(
            "browse_list sync failed for %s: %s — self-heals on the next rebuild",
            ids, exc,
        )
