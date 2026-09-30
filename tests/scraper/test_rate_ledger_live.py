"""The shared rate ledger's lease, executed against the replayed schema.

tests/scraper/test_rate_ledger.py drives LedgerRateLimiter through a fake that
EMULATES _LEASE_SQL, so it cannot prove the property the 2026-09-29 fix rests on:
a lease the caller will not wait for is refused WITHOUT moving the shared
frontier, so a refused caller pushes nobody back. That is how Postgres runs a
data-modifying CTE beside a FOR UPDATE one, so it needs real SQL. A statement that
errors instead would not fail loudly either: the limiter falls back to local
pacing, silently ending the cross-runtime budget.

Gated on TEST_DATABASE_URL like the other live suites, so a normal local `pytest`
skips it. Rows are keyed on a per-test uuid source; nothing touches production.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from scraper import db
from scraper.rate_ledger import _ENSURE_SQL, _LEASE_SQL

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — the live ledger lease runs in the CI DB job",
)


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


@pytest.fixture()
def source(conn: psycopg.Connection) -> Iterator[str]:
    name = f"test-{uuid.uuid4().hex[:12]}"
    conn.execute(_ENSURE_SQL, {"source": name, "interval_ms": 1000})
    yield name
    conn.execute("DELETE FROM portal_rate_state WHERE source = %s", (name,))
    conn.execute("DELETE FROM listing_detail_queue WHERE source = %s", (name,))


def _lease(conn: psycopg.Connection, source: str, *, max_wait_s: float) -> Any:
    return conn.execute(_LEASE_SQL, {
        "source": source, "interval_ms": 1000, "n": 5, "decay": 0.9,
        "max_wait_s": max_wait_s,
    }).fetchone()


def _row(conn: psycopg.Connection, source: str) -> Any:
    return conn.execute(
        "SELECT next_slot_at, penalty_factor FROM portal_rate_state WHERE source = %s",
        (source,),
    ).fetchone()


def test_a_lease_inside_the_bound_is_granted_and_advances_the_frontier(conn, source):
    delay_s, slot_s, granted = _lease(conn, source, max_wait_s=120.0)
    assert granted is True
    assert delay_s == pytest.approx(0.0, abs=1.0)
    assert slot_s == pytest.approx(1.0)
    ahead = conn.execute(
        "SELECT extract(epoch FROM next_slot_at - now())::float8 "
        "FROM portal_rate_state WHERE source = %s", (source,),
    ).fetchone()[0]
    assert ahead == pytest.approx(5.0, abs=1.0)


def test_a_lease_beyond_the_bound_is_refused_and_moves_nothing(conn, source):
    conn.execute(
        "UPDATE portal_rate_state SET next_slot_at = now() + interval '1 hour', "
        "penalty_factor = 8 WHERE source = %s", (source,),
    )
    before = _row(conn, source)
    delay_s, _slot_s, granted = _lease(conn, source, max_wait_s=120.0)
    assert granted is False
    assert delay_s == pytest.approx(3600.0, abs=5.0)
    assert _row(conn, source) == before


def test_a_zero_bound_still_takes_a_slot_that_is_free_now(conn, source):
    # A drain past its deadline may still take a slot that costs no wait.
    conn.execute(
        "UPDATE portal_rate_state SET next_slot_at = now() - interval '1 minute' "
        "WHERE source = %s", (source,),
    )
    _delay_s, _slot_s, granted = _lease(conn, source, max_wait_s=0.0)
    assert granted is True


def test_released_claims_keep_their_attempts(conn, source):
    conn.execute(
        """
        INSERT INTO listing_detail_queue
            (source, native_id, detail_ref, priority, attempts, claimed_at)
        SELECT %(source)s, %(source)s || '-' || g, '/d/' || g, 0, 2, now()
        FROM generate_series(1, 3) AS g
        """,
        {"source": source},
    )
    released = db.release_claims(conn, source, [f"{source}-1", f"{source}-2"])
    assert released == 2
    rows = conn.execute(
        "SELECT native_id, claimed_at IS NULL, attempts, given_up "
        "FROM listing_detail_queue WHERE source = %s ORDER BY native_id", (source,),
    ).fetchall()
    assert rows == [
        (f"{source}-1", True, 2, False),
        (f"{source}-2", True, 2, False),
        (f"{source}-3", False, 2, False),
    ]
