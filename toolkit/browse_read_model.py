"""Read-your-writes patch of the Browse read model (`browse_list`).

`browse_list` (migration 276) is an UNLOGGED snapshot of `browse_projection`,
rebuilt wholesale every 5 min by pg_cron (`rebuild_browse_list`, migration 277).
That cadence fits organic scrape churn but not an operator-initiated identity
change: a merge / unmerge / split must show in Browse the instant the API
returns, not up to a rebuild-interval later (the "merge did nothing, then fixed
itself after ~2 min" report — docs/design/browse-merge-consistency.md). This
patches exactly the touched rows; the periodic rebuild stays the backstop.
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
    abort the caller's write — it runs in a SAVEPOINT (nested transaction) and, on
    any DB error, rolls back only itself, logs, and lets the write commit; the next
    rebuild reconciles. Idempotent, so a patch superseded by a concurrent blue-green
    swap is a harmless no-op.
    """
    ids = list(dict.fromkeys(int(p) for p in property_ids))
    if not ids:
        return
    try:
        # Nested transaction == SAVEPOINT (the callers are already in a txn), so a
        # failure here unwinds only the patch, never the merge/link it follows.
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM browse_list WHERE property_id = ANY(%s)", (ids,))
            cur.execute(
                "INSERT INTO browse_list "
                "SELECT * FROM browse_projection WHERE property_id = ANY(%s)",
                (ids,),
            )
    except psycopg.Error as exc:
        LOG.warning(
            "browse_list sync failed for %s: %s — self-heals on the next rebuild",
            ids, exc,
        )
