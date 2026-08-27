"""The starvation invariant, executed against the replayed schema.

tests/test_detail_queue.py proves the SQL SHAPE with a fake connection: which
CTEs exist, what limits are passed. A fake connection structurally cannot prove
the only thing that matters here — that a refresh backlog larger than the batch
no longer stops new listings being claimed. That is a property of how Postgres
composes two `FOR UPDATE SKIP LOCKED` CTEs under a shared limit, so it needs
real SQL against the real table.

The incident this guards (2026-08-17): `ORDER BY priority DESC, enqueued_at`
over one queue, with a refresh class that a walk re-enqueues on every pass. Over
seven days sreality completed 598,797 refresh fetches and ZERO new-listing
fetches; 15,064 listings sat discovered-but-never-ingested. The guard is
deliberately written as "flood refresh, assert acquisition still flows" rather
than as an assertion about ordering, because ordering is the implementation and
throughput-under-load is the requirement.

Gated on TEST_DATABASE_URL like the other live suites, so a normal local
`pytest` skips it. Rows are keyed on a per-test uuid source in a throwaway
container; nothing here touches production.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from scraper import db

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — the live claim fairness runs in the CI DB job",
)


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


@pytest.fixture()
def source(conn: psycopg.Connection) -> Iterator[str]:
    """A queue namespace unique to one test — `source` is part of the queue key."""
    name = f"test-{uuid.uuid4().hex[:12]}"
    yield name
    conn.execute("DELETE FROM listing_detail_queue WHERE source = %s", (name,))


def _seed(conn: psycopg.Connection, source: str, priority: int, count: int) -> None:
    conn.execute(
        """
        INSERT INTO listing_detail_queue (source, native_id, detail_ref, priority)
        SELECT %(source)s, %(source)s || '-' || %(priority)s || '-' || g,
               '/d/' || g, %(priority)s
        FROM generate_series(1, %(count)s) AS g
        """,
        {"source": source, "priority": priority, "count": count},
    )


def _claimed_priorities(conn: psycopg.Connection, source: str) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT priority, count(*) FROM listing_detail_queue
        WHERE source = %s AND claimed_at IS NOT NULL GROUP BY priority
        """,
        (source,),
    ).fetchall()
    return {int(p): int(n) for p, n in rows}


def test_refresh_flood_cannot_starve_new_listings(conn, source):
    """The regression itself: refresh work an order of magnitude over the batch."""
    _seed(conn, source, db.QUEUE_PRIORITY_CHANGED, 2_000)
    _seed(conn, source, db.QUEUE_PRIORITY_NEW, 500)

    claimed = db.claim_detail_batch(conn, source, 200)

    assert len(claimed) == 200
    # (native_id, detail_ref, index_price_czk, discovery_seq, enqueued_at)
    assert all(len(row) == 5 for row in claimed)
    by_priority = _claimed_priorities(conn, source)
    assert by_priority[db.QUEUE_PRIORITY_NEW] == 100
    assert by_priority[db.QUEUE_PRIORITY_CHANGED] == 100


def test_repeated_claims_drain_the_new_backlog_not_just_one_batch(conn, source):
    """One fair batch is not enough — the backlog has to actually clear.

    Under the old ordering this loop claimed 1,000 refresh rows and zero new
    ones, indefinitely, because the walk refills refresh faster than it drains.
    """
    _seed(conn, source, db.QUEUE_PRIORITY_CHANGED, 5_000)
    _seed(conn, source, db.QUEUE_PRIORITY_NEW, 300)

    for _ in range(5):
        db.claim_detail_batch(conn, source, 200)

    assert _claimed_priorities(conn, source)[db.QUEUE_PRIORITY_NEW] == 300


def test_unused_acquisition_reserve_backfills_to_refresh(conn, source):
    """A quiet market must cost refresh nothing — no idle half-batches."""
    _seed(conn, source, db.QUEUE_PRIORITY_CHANGED, 500)
    _seed(conn, source, db.QUEUE_PRIORITY_NEW, 10)

    claimed = db.claim_detail_batch(conn, source, 200)

    assert len(claimed) == 200
    by_priority = _claimed_priorities(conn, source)
    assert by_priority[db.QUEUE_PRIORITY_NEW] == 10
    assert by_priority[db.QUEUE_PRIORITY_CHANGED] == 190


def test_refresh_keeps_its_internal_ranking(conn, source):
    """Failure-retry still outranks price-changed, and the location refetch lane
    (priority -1, migration 384) still sorts below both — the change is about
    acquisition vs refresh, not about reshuffling refresh."""
    _seed(conn, source, db.QUEUE_PRIORITY_CHANGED, 100)
    _seed(conn, source, db.QUEUE_PRIORITY_FAILURE, 60)
    _seed(conn, source, -1, 100)

    db.claim_detail_batch(conn, source, 100, acquisition_reserve=0)

    by_priority = _claimed_priorities(conn, source)
    assert by_priority[db.QUEUE_PRIORITY_FAILURE] == 60
    assert by_priority[db.QUEUE_PRIORITY_CHANGED] == 40
    assert -1 not in by_priority


def test_acquisition_is_claimed_oldest_first(conn, source):
    """Within acquisition the queue is FIFO, so a listing cannot be overtaken
    indefinitely by fresher discoveries."""
    _seed(conn, source, db.QUEUE_PRIORITY_NEW, 50)
    conn.execute(
        """
        UPDATE listing_detail_queue SET enqueued_at = now() - interval '9 days'
        WHERE source = %s AND native_id = %s
        """,
        (source, f"{source}-0-7"),
    )

    claimed = db.claim_detail_batch(conn, source, 2)

    assert f"{source}-0-7" in {nid for nid, _ref, _price, _seq, _enq in claimed}


def test_claimed_rows_are_not_reclaimed_by_a_concurrent_drain(conn, source):
    """SKIP LOCKED across two CTEs must not hand the same row to two drains."""
    _seed(conn, source, db.QUEUE_PRIORITY_NEW, 100)
    _seed(conn, source, db.QUEUE_PRIORITY_CHANGED, 100)

    first = {nid for nid, _r, _p, _s, _e in db.claim_detail_batch(conn, source, 100)}
    second = {nid for nid, _r, _p, _s, _e in db.claim_detail_batch(conn, source, 100)}

    assert len(first) == 100
    assert len(second) == 100
    assert not (first & second)
