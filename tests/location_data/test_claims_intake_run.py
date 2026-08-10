"""The run loop: refusals, batch discipline, and the shape of what gets written.

No database. These assert the invariants that only the RUNNER can break — the W1-input
refusal, the single-statement atomicity of claim + observation + `dirty_locations`, and the
keyset/watermark contract of the two batch queries.
"""

from __future__ import annotations

import pytest

from location_data.claims_intake import (
    _ABSENCE_WRITE_SQL,
    _BATCH_FINISH_SQL,
    _BATCH_INSERT_SQL,
    _CLAIM_WRITE_SQL,
    _ENRICHMENT_WRITE_SQL,
    _INVENTORY_TERMINAL_SQL,
    _LISTINGS_FULL_SQL,
    _LISTINGS_INCREMENTAL_SQL,
    _RESUME_SQL,
    _WATERMARK_SQL,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    Absence,
    EnrichmentTask,
    IntakeRefused,
    IntakeResult,
    assert_inventory_ready,
    dedupe_absence_rows,
    extract_listing,
    write_result,
)
from tests.location_data.claim_intake_fixtures import (
    SREALITY_POST_CUTOVER,
    SREALITY_TRUNCATED,
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
        if "FROM mapy_inventory_runs" in self._sql:
            return self._conn.inventory_runs
        return (1, 0, 1)

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, *, missing=(), inventory_rows=1, inventory_runs=None):
        self.missing = set(missing)
        self.inventory_rows = inventory_rows
        # (run_count, max restart_epoch, a completed+resumable run exists, status list)
        self.inventory_runs = (
            inventory_runs if inventory_runs is not None else (3, 0, True, "completed"))
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


def test_a_partially_built_inventory_is_refused_even_though_it_is_not_empty():
    """The inventory job is batched and resumable, so a budget-stopped run leaves a
    populated table describing a PREFIX of `listings`. Every listing past that prefix then
    reads as ABSENT from the inventory — which is precisely the verdict that admits a
    Mapy-derived `carry_forward` coordinate as first-party (06 §6.1.2). `count(*) > 0` is
    not the gate; a terminal, complete, unanchored run in the current epoch is."""
    for runs in (
        (1, 0, False, "running"),                      # still going
        (1, 0, False, "stopped"),                      # hit its budget
        (2, 0, False, "failed,stopped"),               # never finished
        (3, 1, False, "running"),                      # a --restart epoch, mid-flight
    ):
        with pytest.raises(IntakeRefused, match="INCOMPLETE"):
            assert_inventory_ready(_Conn(inventory_rows=2201, inventory_runs=runs))

    # A completed epoch-0 sweep does NOT vouch for an epoch-1 restart: the completeness
    # question is asked inside the CURRENT epoch only (migration 385's own contract).
    with pytest.raises(IntakeRefused, match="restart epoch 1"):
        assert_inventory_ready(
            _Conn(inventory_rows=2201, inventory_runs=(4, 1, False, "stopped")))

    # Terminal AND complete AND unanchored: admitted.
    assert assert_inventory_ready(
        _Conn(inventory_rows=2201, inventory_runs=(2, 1, True, "completed,stopped"))) == 2201


def test_an_unaccounted_inventory_is_refused():
    """Rows in `mapy_affected` with no run that produced them cannot be shown complete."""
    with pytest.raises(IntakeRefused, match="no rows"):
        assert_inventory_ready(_Conn(inventory_rows=2201, inventory_runs=(0, 0, False, None)))


def test_only_a_completed_inventory_run_is_read_from_the_current_epoch():
    """The SQL asks migration 385's question, not a looser one."""
    sql = " ".join(_INVENTORY_TERMINAL_SQL.split())
    assert "r.status = 'completed'" in sql
    assert "AND r.resumable" in sql
    assert "r.restart_epoch = (SELECT max(restart_epoch) FROM mapy_inventory_runs)" in sql


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
    assert ("ON CONFLICT (listing_id, snapshot_key, surface, field, extractor_version) "
            "DO NOTHING") in absence_sql
    # An anti-join cannot arbitrate two identical rows inside ONE statement (a statement's
    # snapshot cannot see its own inserts), so the unique index would raise and take the
    # whole run down. ON CONFLICT is the only form that survives it.
    assert "NOT EXISTS" not in absence_sql
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


def test_one_listing_can_produce_two_absences_with_the_same_unique_key():
    """The regression. A sreality listing that is in `mapy_affected` AND lost its
    `locality` object to the 80 KB truncation emits BOTH the withheld-coordinate absence
    (the licence ladder refused its `listings.geom`) and the truncated-payload absence —
    same listing, same surface, same field, same extractor_version, i.e. one row as far as
    migration 382's unique key is concerned. Before the dedupe they went to the database
    as two rows in one statement and the second raised a unique violation, failing the
    entire intake run on a data shape that occurs by construction."""
    row = listing("sreality", SREALITY_TRUNCATED, lat=50.078, lon=14.450,
                  in_mapy_inventory=True)
    result = extract_listing(row, entries_for("sreality"))

    keys = [(a.listing_id, a.surface, a.field_) for a in result.absences]
    assert len(keys) == 2, keys
    assert keys[0] == keys[1], keys

    conn = _Conn()
    with conn.cursor() as cur:
        write_result(cur, result, batch_id=7)
    absence_call = [p for s, p in conn.executed if "location_claim_absences" in s][0]
    assert len(absence_call["rows"].obj) == 1


def test_dedupe_absence_rows_keeps_the_first_assertion_per_unique_key():
    rows = [
        {"listing_id": 1, "surface": "api_json", "field": "coordinate",
         "extractor_version": "v1", "reason": "not_attempted"},
        {"listing_id": 1, "surface": "api_json", "field": "coordinate",
         "extractor_version": "v1", "reason": "not_stated"},
        # Different surface, different fact — 382 keeps `surface` in the key on purpose.
        {"listing_id": 1, "surface": "archived_html", "field": "coordinate",
         "extractor_version": "v1", "reason": "not_stated"},
        {"listing_id": 2, "surface": "api_json", "field": "coordinate",
         "extractor_version": "v1", "reason": "not_attempted"},
    ]
    kept = dedupe_absence_rows(rows)
    assert [r["reason"] for r in kept] == ["not_attempted", "not_stated", "not_attempted"]
    assert [r["surface"] for r in kept] == ["api_json", "archived_html", "api_json"]


def test_the_watermark_is_per_source_and_only_advances_on_a_successful_batch():
    sql = " ".join(_WATERMARK_SQL.split())
    assert "outcome = 'ok'" in sql
    assert "source IS NOT DISTINCT FROM %(source)s" in sql
    assert "'running'" in " ".join(_BATCH_INSERT_SQL.split())


def test_a_budget_stopped_run_is_invisible_to_the_watermark():
    """The critical one. `outcome='ok'` now means "the scan ran out of rows", and the
    watermark reads nothing else — so a run that scanned 30k of 650k listings and stopped
    leaves the incremental floor exactly where it found it. Stamping it 'ok' moved the
    floor past 620k rows that were never opened, and for the ~270k delisted ones (whose
    `last_seen_at` will never move again) that is permanent."""
    assert "outcome = 'ok'" in " ".join(_WATERMARK_SQL.split())
    for terminal in ("'stopped'", "'failed'", "'running'"):
        assert terminal not in _WATERMARK_SQL


def test_the_batch_row_carries_the_cursor_and_the_mode_that_wrote_it():
    """A full cursor is a bare `listings.id`; an incremental one is `(last_seen_at, id)`.
    Resuming one from the other would skip an arbitrary slice, so `scan_mode` rides on the
    row and the resume lookup filters on it."""
    finish = " ".join(_BATCH_FINISH_SQL.split())
    assert "cursor_after_id = %(cursor_after_id)s" in finish
    assert "cursor_after_ts = %(cursor_after_ts)s" in finish
    insert = " ".join(_BATCH_INSERT_SQL.split())
    assert "scan_mode" in insert and "resumable" in insert

    resume = " ".join(_RESUME_SQL.split())
    assert "scan_mode = %(scan_mode)s" in resume
    # An operator-anchored run's cursor does not certify that everything below it was
    # scanned (migration 385 puts the same guard on `mapy_inventory_runs`).
    assert "AND resumable" in resume
    assert "ORDER BY started_at DESC, id DESC" in resume


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
