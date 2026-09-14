"""W8: the candidate set and the enqueue behind the daily location re-fetch lane.

The finding (measured 2026-09-14): ceskereality and realitymix geocode an ad AFTER
publishing it, so the detail fetch we ran at discovery read an empty location and
nothing ever re-read the page — 56 of 63 ceskereality and 56 of 112 realitymix rows
in the audit page's active "no data" bucket were fetched within two minutes of first
sighting and never again. These tests pin the two halves of the second look: WHICH
rows it nominates, and that queuing them can never jump the queue or leave a
given-up row unreachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from scraper import db


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if "to_regclass" in s:
            self._rows = [(self._conn.view_present,)]
        elif "location_pin_audit_mv" in s:
            self._rows = list(self._conn.candidate_rows)
        else:
            self._rows = []
        self.rowcount = len(self._rows) if "INSERT" not in s else self._conn.affected

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _Conn:
    def __init__(self, *, view_present: bool = True,
                 candidate_rows: list[tuple[Any, ...]] | None = None,
                 affected: int = 0) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.view_present = view_present
        self.candidate_rows = candidate_rows or []
        self.affected = affected

    def cursor(self) -> _Cur:
        return _Cur(self)


_WHEN = datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc)


def _row(source: str, nid: str, url: str | None, total: int) -> tuple[Any, ...]:
    return (source, nid, url, 4_500_000, _WHEN, total)


def test_candidates_are_the_audit_pages_active_no_data_bucket() -> None:
    conn = _Conn(candidate_rows=[
        _row("ceskereality", "3876635", "https://ceskereality.cz/3876635", 63),
    ])
    out = db.location_refetch_candidates(conn, min_age_seconds=21600, cap_per_source=500)

    sql, params = conn.executed[-1]
    # The audit relation IS the list — it is not re-derived here, so the page the
    # operator reads and the rows this queues can never diverge.
    assert "location_pin_audit_mv" in sql
    assert "a.state = 'unresolved'" in sql and "a.quality = 'active_no_claims'" in sql
    # ... but the ROW is taken live from listings: the matview is an hourly snapshot and
    # a re-fetch must not be aimed by a stale URL, nor sent at a since-delisted listing.
    assert "JOIN listings l ON l.id = a.listing_id" in sql and "l.is_active" in sql
    assert params == {"min_age": 21600.0, "cap": 500}
    assert out == [{
        "source": "ceskereality", "native_id": "3876635",
        "detail_ref": "https://ceskereality.cz/3876635", "price_czk": 4_500_000,
        "last_fetch_at": _WHEN, "source_total": 63,
    }]


def test_only_pages_older_than_the_min_age_and_oldest_first_within_a_cap() -> None:
    conn = _Conn(candidate_rows=[_row("bazos", "1", "https://bazos/1", 203)])
    db.location_refetch_candidates(conn, min_age_seconds=600, cap_per_source=2)
    sql, params = conn.executed[-1]
    # A listing fetched minutes ago would be re-read at the same empty moment.
    assert "c.last_fetch_at < now() - make_interval(secs => %(min_age)s" in sql
    # The bound is PER PORTAL and takes the longest-unfetched first, so a portal with
    # thousands of dead ads drains over days instead of flooding one drain pass.
    assert "row_number() OVER (PARTITION BY c.source ORDER BY c.last_fetch_at" in sql
    assert "WHERE rn <= %(cap)s::int" in sql
    assert params["cap"] == 2


def test_a_missing_audit_view_is_none_not_an_empty_set() -> None:
    # A caller must tell "nothing to do" from "this database cannot answer": the lane
    # skips its tick instead of reporting a clean zero for ever.
    conn = _Conn(view_present=False)
    assert db.location_refetch_candidates(
        conn, min_age_seconds=21600, cap_per_source=500) is None
    assert len(conn.executed) == 1, "the candidate query never runs without the view"


def test_sreality_is_fetched_by_id_never_by_its_stored_url() -> None:
    # detail_ref's safety boundary: sreality's source_url 302s into a login chain.
    conn = _Conn(candidate_rows=[_row("sreality", "123", "https://sreality.cz/detail", 1)])
    out = db.location_refetch_candidates(conn, min_age_seconds=21600, cap_per_source=500)
    assert out[0]["detail_ref"] is None


def test_the_enqueue_uses_verify_priority_and_re_arms_a_given_up_row() -> None:
    conn = _Conn(affected=2)
    queued = db.enqueue_location_refetch(
        conn, "realitymix",
        [("11", "https://realitymix/11", 3_000_000), ("12", None, None)],
    )
    assert queued == 2
    sql, params = conn.executed[-1]
    assert params["verify"] == db.QUEUE_PRIORITY_VERIFY == -2
    # LEAST, not GREATEST: a second look shares the presence checks' class and must
    # never promote a queued row above the brand-new listings the drain is fetching.
    assert "priority = LEAST(listing_detail_queue.priority, EXCLUDED.priority)" in sql
    # The rows this lane most wants are the ones the drain gave up on after five
    # failures — invisible to every claim until something re-arms them (rule #5).
    assert "given_up = false" in sql and "attempts = 0" in sql
    # A row a drain has already claimed is never disturbed.
    assert "WHERE listing_detail_queue.claimed_at IS NULL" in sql
    assert params["nids"] == ["11", "12"] and params["refs"] == [
        "https://realitymix/11", None]


def test_enqueueing_nothing_touches_the_database_not_at_all() -> None:
    conn = _Conn()
    assert db.enqueue_location_refetch(conn, "bazos", []) == 0
    assert conn.executed == []
