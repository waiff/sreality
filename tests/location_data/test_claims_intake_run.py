"""The run loop: refusals, batch discipline, and the shape of what gets written.

No database. These assert the invariants that only the RUNNER can break — the W1-input
refusal, the single-statement atomicity of claim + observation + `dirty_locations`, and the
keyset/watermark contract of the two batch queries.
"""

from __future__ import annotations

import pytest

from location_data.claims_intake import (
    _ABSENCE_WRITE_SQL,
    _BATCH_INSERT_SQL,
    _CLAIM_WRITE_SQL,
    _ENRICHMENT_WRITE_SQL,
    _LISTINGS_FULL_SQL,
    _LISTINGS_INCREMENTAL_SQL,
    _WATERMARK_SQL,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    Absence,
    EnrichmentTask,
    IntakeRefused,
    IntakeResult,
    assert_inventory_ready,
    extract_listing,
    write_result,
)
from tests.location_data.claim_intake_fixtures import (
    SREALITY_POST_CUTOVER,
    entries_for,
    listing,
)


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

    def fetchone(self):
        if "to_regclass" in self._sql:
            name = self._conn.executed[-1][1]["name"]
            return (None if name in self._conn.missing else name,)
        if "count(*) FROM mapy_affected" in self._sql:
            return (self._conn.inventory_rows,)
        return (1, 0, 1)

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, *, missing=(), inventory_rows=1):
        self.missing = set(missing)
        self.inventory_rows = inventory_rows
        self.executed: list[tuple[str, object]] = []

    def cursor(self):
        return _Cursor(self)

    def transaction(self):
        return _Cursor(self)


def test_the_lane_refuses_to_run_without_the_mapy_inventory_table():
    with pytest.raises(IntakeRefused, match="migration 385"):
        assert_inventory_ready(_Conn(missing={"mapy_affected"}))


def test_the_lane_refuses_to_run_on_an_empty_mapy_inventory():
    """An empty inventory would admit every carry_forward coordinate as first-party — the
    inventory is a W1 INPUT, not a W1 output (06 §6.1.2)."""
    with pytest.raises(IntakeRefused, match="empty"):
        assert_inventory_ready(_Conn(inventory_rows=0))
    assert assert_inventory_ready(_Conn(inventory_rows=2201)) == 2201


def test_claim_observation_and_dirty_enqueue_are_one_statement():
    """03 §3.2: the `dirty_locations` enqueue happens INSIDE the claim-insert transaction —
    it is the only coupling between intake and resolution."""
    conn = _Conn()
    result = extract_listing(
        listing("sreality", SREALITY_POST_CUTOVER, lat=50.078, lon=14.450),
        entries_for("sreality"))
    with conn.cursor() as cur:
        inserted, observed, enqueued = write_result(cur, result, batch_id=42)

    claim_statements = [s for s, _ in conn.executed if "INSERT INTO location_claims" in s]
    assert len(claim_statements) == 1
    one = claim_statements[0]
    assert "INSERT INTO location_claim_observations" in one
    assert "INSERT INTO dirty_locations" in one
    assert "'claim_insert'" in one
    assert (inserted, observed, enqueued) == (1, 0, 1)


def test_observations_are_appended_only_on_a_re_sight_and_never_duplicated():
    """`location_claim_observations` is the highest-cardinality table in the design: the
    claim row IS its own first observation, and a re-run must not append a second row for
    an (already recorded) sighting."""
    assert "FROM deduped d JOIN location_claims c" in " ".join(_CLAIM_WRITE_SQL.split())
    assert "WHERE NOT EXISTS ( SELECT 1 FROM location_claim_observations o" in " ".join(
        _CLAIM_WRITE_SQL.split())


def test_the_claim_write_dedupes_within_the_batch():
    """Two listings can legitimately produce the same fingerprint (the tuple is time-free);
    `ON CONFLICT` cannot arbitrate two rows inside ONE statement, so the batch dedupes."""
    assert "DISTINCT ON (claim_fingerprint)" in _CLAIM_WRITE_SQL


def test_absences_and_enrichment_are_idempotent():
    conn = _Conn()
    result = IntakeResult(
        absences=[Absence(1, "legacy_column", "coordinate", "not_attempted",
                          "legacy_column", "mapy_derived_coordinate")],
        enrichment=[EnrichmentTask(1, "portal_structured_field", "sreality_detail_refetch",
                                   "skipped", "ab" * 32)])
    with conn.cursor() as cur:
        write_result(cur, result, batch_id=1)

    absence_sql = [s for s, _ in conn.executed if "location_claim_absences" in s][0]
    assert "WHERE NOT EXISTS" in absence_sql
    assert "a.snapshot_key = -1" in absence_sql
    enrichment_sql = [s for s, _ in conn.executed if "location_enrichment_state" in s][0]
    assert "ON CONFLICT (listing_id, method, lane) DO UPDATE" in enrichment_sql
    # `input_hash` is the cost gate: an unchanged payload must not advance `attempts`.
    assert "input_hash IS DISTINCT FROM EXCLUDED.input_hash" in enrichment_sql


def test_batch_queries_are_keyset_and_bounded():
    full = " ".join(_LISTINGS_FULL_SQL.split())
    incremental = " ".join(_LISTINGS_INCREMENTAL_SQL.split())
    assert "l.id > %(after_id)s" in full and "ORDER BY l.id LIMIT" in full
    assert "(l.last_seen_at, l.id) > (%(after_ts)s, %(after_id)s)" in incremental
    assert "l.last_seen_at >= %(watermark)s" in incremental
    # Both walk active AND inactive rows: a delisted listing's payload is still evidence,
    # and nothing is ever deleted (CLAUDE.md rule 3).
    assert "is_active" not in full and "is_active" not in incremental
    assert MIN_BATCH_SIZE == 10_000 and MAX_BATCH_SIZE == 30_000


def test_the_watermark_is_per_source_and_only_advances_on_a_successful_batch():
    sql = " ".join(_WATERMARK_SQL.split())
    assert "outcome = 'ok'" in sql
    assert "source IS NOT DISTINCT FROM %(source)s" in sql
    assert "'running'" in " ".join(_BATCH_INSERT_SQL.split())


def test_no_write_statement_touches_an_existing_production_table():
    """W1 is additive and shadow-only: it reads `listings` and writes only location_* /
    dirty_locations."""
    for sql in (_CLAIM_WRITE_SQL, _ABSENCE_WRITE_SQL, _ENRICHMENT_WRITE_SQL,
                _BATCH_INSERT_SQL):
        lowered = sql.lower()
        for verb in ("insert into", "update ", "delete from"):
            for fragment in lowered.split(verb)[1:]:
                target = fragment.strip().split()[0].strip("(")
                assert target.startswith(("location_", "dirty_locations")), target
