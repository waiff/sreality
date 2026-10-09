"""E941, EXECUTED: the autodedup lane's lease fence reads the CLOCK.

Session A takes the lane's lease and opens a transaction; session B — the worker's shutdown
path, or a `release_lease=` dispatch — releases A's lease and commits; A's fence
(`rt_lease.hold`, `RT_LEASE_HOLD_SQL`) must then find no live row and raise. Against `now()` —
A's transaction START, earlier than B's release — the row would still read live and A would
commit beside the next holder: the fake has one clock and cannot tell the two apart, so this
runs on Postgres. And a release that comes AFTER A's fence waits for A's commit (the row lock).
Runs in CI's migrations job (`TEST_DATABASE_URL`) on a lease name of its own, deleted after.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any, Iterator

import pytest

from autodedup import rt_lease

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)


def _session() -> Any:
    import psycopg

    return psycopg.connect(_DB_URL, autocommit=True,
                           options="-c statement_timeout=20000 -c lock_timeout=5000")


@pytest.fixture()
def sessions(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, Any]]:
    """Two sessions over a lease row of this test's own, so the lane's row is never touched."""
    name = f"e941-fence-test:{uuid.uuid4()}"
    monkeypatch.setattr(rt_lease, "NAME", name)
    a, b = _session(), _session()
    try:
        yield a, b
    finally:
        a.close()
        b.close()
        with _session() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM autodedup.rt_lease WHERE name = %s", (name,))


def test_a_lease_released_mid_transaction_fails_the_fence(sessions: tuple[Any, Any]) -> None:
    a, b = sessions
    assert rt_lease.take(a, "A", 600)
    with a.transaction():
        with a.cursor() as cur:
            cur.execute("SELECT now()")          # A's transaction has started: now() is fixed
        time.sleep(0.05)
        rt_lease.release(b, "A")                 # committed by B while A's transaction is open
        time.sleep(0.05)
        with pytest.raises(rt_lease.LeaseLost):
            rt_lease.hold(a, "A")
    assert rt_lease.current(a)["live"] is False


def test_a_live_lease_passes_the_fence_and_holds_a_later_release(
        sessions: tuple[Any, Any]) -> None:
    a, b = sessions
    assert rt_lease.take(a, "A", 600)
    released = threading.Event()

    def release() -> None:
        rt_lease.release(b, "A")
        released.set()

    with a.transaction():
        rt_lease.hold(a, "A")                    # live: passes, and locks the row until commit
        thread = threading.Thread(target=release)
        thread.start()
        assert not released.wait(0.5), "the release waits for A's commit"
    thread.join(5)
    assert released.is_set()
    assert rt_lease.current(a)["live"] is False
