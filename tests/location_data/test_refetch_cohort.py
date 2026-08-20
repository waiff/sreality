"""W4's refetch-cohort consumer: retirement, scheduling, and the two traps that made it.

No database. What these pin is exactly what a DB-less test CAN pin and a reviewer cannot
eyeball: that a row leaves the cohort when its work is done, that a dispatched row's
schedule actually advances, and that the truncation cohort is never mistaken for success.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from location_data import refetch_cohort
from location_data.claims_intake import sreality_payload_shape
from location_data.refetch_cohort import (
    _MARK_DISPATCHED_SQL,
    _MARK_PLACED_SQL,
    _MARK_RETIRED_SQL,
    COHORT_LANE,
    CONCURRENCY_GROUP,
    JOB_NAME,
    MAX_ATTEMPTS,
    REFETCH_PRIORITY,
    CohortRow,
    DueRow,
    classify,
    dispatch,
    reconcile,
)

_POST_CUTOVER = {"gps_lat": 50.08, "gps_lon": 14.42, "entity_type": "address",
                 "inaccuracy_type": "address", "city": "Praha", "citypart": "Vinohrady"}
_LEGACY = {"name": "Praha 2 - Vinohrady", "value": 12345, "accuracy": "street"}


def _row(locality, *, is_active=True, attempts=0, listing_id=1) -> CohortRow:
    return CohortRow(listing_id=listing_id, is_active=is_active, attempts=attempts,
                     locality=locality)


class _Cursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._sql = " ".join(sql.split())
        self._conn.executed.append((self._sql, params))

    def fetchall(self):
        if "FROM location_enrichment_state es" in self._sql and "l.raw_json" in self._sql:
            batch, self._conn.cohort = self._conn.cohort, []
            return batch
        if "FROM location_enrichment_state es" in self._sql:
            due, self._conn.due = self._conn.due, []
            return due
        return []

    @property
    def rowcount(self):
        return 0


class _Conn:
    def __init__(self, *, due=(), cohort=()):
        self.due = list(due)
        self.cohort = list(cohort)
        self.executed: list[tuple[str, object]] = []

    def cursor(self):
        return _Cursor(self)

    def transaction(self):
        return _Cursor(self)

    def execute(self, sql, params=None):
        cur = _Cursor(self)
        cur.execute(sql, params)
        return cur


def _sql_of(conn, needle: str) -> list[tuple[str, object]]:
    return [(s, p) for s, p in conn.executed if needle in s]


# ---------------------------------------------------------------- classification

def test_a_refetched_row_is_placed_and_leaves_the_cohort():
    assert classify(_row(_POST_CUTOVER)) == "placed"


def test_a_still_legacy_row_stays_pending():
    assert classify(_row(_LEGACY)) == "pending"


def test_the_truncation_cohort_is_never_read_as_success():
    """The 80 KB-truncation cohort lost the `locality` object outright, so the projected
    value is SQL NULL. Classifying that as `placed` would retire the rows W4 exists to
    fix, silently, and they would never be refetched again."""
    assert classify(_row(None)) == "pending"


def test_an_object_carrying_neither_key_set_is_not_placed():
    """`sreality_payload_shape` returns `absent` from TWO arms — the not-a-dict guard and
    this fall-through. A three-way SQL mirror drops this bucket on the floor."""
    assert sreality_payload_shape({"locality": {"unexpected": 1}}) == "absent"
    assert classify(_row({"unexpected": 1})) == "pending"


def test_a_delisted_row_is_not_applicable_rather_than_pending():
    """Nothing refetches a delisted listing, so a row that will never resolve must leave
    the cohort — otherwise it is rescanned on every pass forever."""
    assert classify(_row(_LEGACY, is_active=False)) == "not_applicable"


def test_a_delisted_row_that_is_already_post_cutover_still_leaves_the_cohort():
    assert classify(_row(_POST_CUTOVER, is_active=False)) == "not_applicable"


def test_a_row_that_never_flips_is_exhausted_not_retried_forever():
    assert classify(_row(_LEGACY, attempts=MAX_ATTEMPTS - 1)) == "pending"
    assert classify(_row(_LEGACY, attempts=MAX_ATTEMPTS)) == "exhausted"


# ---------------------------------------------------------------- the two traps

def test_dispatch_advances_the_schedule_of_every_row_it_enqueues(monkeypatch):
    """THE TRAP. `_ENRICHMENT_WRITE_SQL`'s DO UPDATE is gated on `input_hash IS DISTINCT
    FROM`, so an unchanged legacy payload no-ops and `next_eligible_at` stays frozen at
    its first-sight value — permanently in the past. If dispatch does not advance it, a
    driver re-claims the entire cohort on every pass instead of once per window."""
    enqueued: list[tuple[str, list]] = []
    monkeypatch.setattr("location_data.refetch_cohort.db.enqueue_detail",
                        lambda conn, source, entries: enqueued.append(
                            (source, list(entries))) or len(list(entries)))

    conn = _Conn(due=[(11, "sreality", "700111", 0), (12, "sreality", "700112", 1)])
    stats = dispatch(conn)

    assert stats["due"] == 2
    marked = _sql_of(conn, "SET attempts = attempts + 1")
    assert len(marked) == 1, "every dispatched row must have its schedule advanced"
    assert marked[0][1]["ids"] == [11, 12]
    assert marked[0][1]["lane"] == COHORT_LANE
    assert "next_eligible_at = now()" in marked[0][0]


def test_dispatch_enqueues_behind_realtime_discovery():
    """06 §6.4 routes the cohort through the existing bounded drain rather than a bespoke
    crawler; a negative priority keeps a 38k-row backlog from starving live ingest."""
    assert REFETCH_PRIORITY < 0


def test_dispatch_enqueues_at_that_priority_with_no_detail_ref(monkeypatch):
    seen: list = []
    monkeypatch.setattr("location_data.refetch_cohort.db.enqueue_detail",
                        lambda conn, source, entries: seen.extend(entries) or len(seen))
    dispatch(_Conn(due=[(11, "sreality", "700111", 0)]))
    assert seen == [("700111", None, None, REFETCH_PRIORITY)]


def test_reconcile_retires_a_placed_row_without_marking_it_given_up():
    """`given_up` carries the "stopped trying" meaning it has in `listing_fetch_failures`.
    A row that SUCCEEDED did not give up, and conflating the two makes the cohort
    unreadable — retirement is `next_eligible_at = NULL`."""
    conn = _Conn(cohort=[(11, True, 0, _POST_CUTOVER)])
    stats = reconcile(conn)

    assert stats["placed"] == 1
    placed = _sql_of(conn, "SET last_outcome = 'placed'")
    assert len(placed) == 1
    assert placed[0][1]["ids"] == [11]
    assert "next_eligible_at = NULL" in placed[0][0]
    assert "given_up" not in placed[0][0]
    assert not _sql_of(conn, "SET given_up = true")


def test_reconcile_retires_delisted_and_exhausted_rows_as_given_up():
    conn = _Conn(cohort=[(11, False, 0, _LEGACY), (12, True, MAX_ATTEMPTS, _LEGACY)])
    stats = reconcile(conn)

    assert stats["not_applicable"] == 1 and stats["exhausted"] == 1
    retired = _sql_of(conn, "SET given_up = true")
    assert {tuple(p["ids"]) for _, p in retired} == {(11,), (12,)}
    assert {p["outcome"] for _, p in retired} == {"not_applicable", "error"}


def test_reconcile_leaves_pending_rows_alone():
    conn = _Conn(cohort=[(11, True, 0, _LEGACY)])
    stats = reconcile(conn)
    assert stats["pending"] == 1
    assert not _sql_of(conn, "SET last_outcome = 'placed'")
    assert not _sql_of(conn, "SET given_up = true")


def test_reconcile_scans_the_whole_cohort_not_only_the_due_rows():
    """A row is retired by facts that change OUTSIDE this table — the payload shape and
    `is_active` — so gating the scan on `next_eligible_at <= now()` would leave finished
    rows in the cohort until their backoff happened to expire."""
    from location_data.refetch_cohort import _COHORT_SCAN_SQL
    assert "next_eligible_at" not in _COHORT_SCAN_SQL
    assert "NOT es.given_up" in _COHORT_SCAN_SQL


def test_dry_run_writes_nothing():
    conn = _Conn(cohort=[(11, True, 0, _POST_CUTOVER)], due=[(11, "sreality", "700111", 0)])
    reconcile(conn, dry_run=True)
    dispatch(conn, dry_run=True)
    assert not _sql_of(conn, "UPDATE location_enrichment_state")


# ---------------------------------------------------------------- SQL contracts

def test_the_due_query_rides_the_les_due_index_predicate():
    """Migration 384 ships `les_due (lane, next_eligible_at) where not given_up` — the
    index built for a consumer that was never written. The claim query must match its
    predicate or the scan falls off it."""
    from location_data.refetch_cohort import _DUE_SQL
    assert "NOT es.given_up" in _DUE_SQL
    assert "es.next_eligible_at <= now()" in _DUE_SQL
    assert "es.next_eligible_at IS NOT NULL" in _DUE_SQL


@pytest.mark.parametrize("sql", [_MARK_PLACED_SQL, _MARK_RETIRED_SQL, _MARK_DISPATCHED_SQL])
def test_every_write_is_lane_scoped(sql: str):
    """`location_enrichment_state` is keyed (listing_id, method, lane) and other lanes will
    land in it (the LLM lane is already named in 384). An unscoped UPDATE would retire
    another wave's work."""
    assert "lane = %(lane)s" in sql


def test_a_placed_row_is_never_also_given_up():
    assert "given_up" not in _MARK_PLACED_SQL
    assert "given_up = true" in _MARK_RETIRED_SQL


# ---------------------------------------------------------------- the dispatch lane

_WORKFLOW = (Path(__file__).resolve().parents[2]
             / ".github" / "workflows" / "location_refetch_cohort.yml")


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def test_the_lane_cannot_start_a_refetch_on_a_schedule():
    """Dispatch-only. A `schedule:` here would start enqueuing refetches against nine
    portals' egress the moment it merged, with no operator in the loop."""
    triggers = _workflow()[True] if True in _workflow() else _workflow()["on"]
    assert set(triggers) == {"workflow_dispatch"}


def test_the_lane_defaults_to_the_phase_that_writes_no_queue_rows():
    """`reconcile` retires finished rows and enqueues nothing. It is the correct first run:
    the cohort has never been cleaned, so its size says nothing about the work left."""
    inputs = _workflow()[True]["workflow_dispatch"]["inputs"] if True in _workflow() \
        else _workflow()["on"]["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["default"] == "reconcile"
    assert set(inputs["mode"]["options"]) == {"reconcile", "dispatch"}


def test_the_lane_stays_out_of_the_oversubscribed_batch_group():
    """`location-batch` serialises heavy corpus-wide DB sweeps and is measurably
    oversubscribed (#1084). This lane's scarce resource is portal egress, which the drain
    it enqueues into already governs — joining would buy queueing, not safety."""
    assert _workflow()["concurrency"]["group"] == CONCURRENCY_GROUP
    assert _workflow()["concurrency"]["group"] != "location-batch"
    assert _workflow()["concurrency"]["cancel-in-progress"] is False


def test_the_lane_holds_both_halves_of_its_lease():
    """A GitHub concurrency group cannot see a manual local invocation; the lease-row CAS
    can. Advisory locks are refused here — the transaction-mode pooler strands one."""
    assert JOB_NAME and CONCURRENCY_GROUP
    source = Path(refetch_cohort.__file__).read_text(encoding="utf-8")
    assert "lease.held(" in source
    assert "pg_advisory" not in source.lower()
