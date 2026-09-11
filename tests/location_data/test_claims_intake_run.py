"""The run loop: refusals, batch discipline, the hash gate, and what gets written.

No database. These assert the invariants that only the RUNNER can break — the lane-input
refusal, the single-statement atomicity of claim + `dirty_locations`, the keyset/watermark
contract of the two batch queries, and the `portal_raw_payloads.contract_version` gate that
bounds the page half by page churn rather than by corpus size.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from location_data import claims_intake
from location_data.claims_intake import (
    _BATCH_FINISH_SQL,
    _BATCH_INSERT_SQL,
    _CLAIM_WRITE_SQL,
    _INVENTORY_TERMINAL_SQL,
    _LISTINGS_FULL_SQL,
    _LISTINGS_INCREMENTAL_SQL,
    _RESUME_SQL,
    _STAMP_MINED_SQL,
    _WATERMARK_SQL,
    LEGACY_COLUMNS,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    IntakeRefused,
    _row_from_record,
    assert_inventory_ready,
    extract_listing,
    stamp_mined_bodies,
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
        return (1, 1)

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


def test_claim_and_dirty_enqueue_are_one_statement():
    """03 §3.2: the `dirty_locations` enqueue happens INSIDE the claim-insert transaction —
    it is the only coupling between intake and resolution."""
    conn = _Conn()
    result = extract_listing(
        listing("sreality", SREALITY_POST_CUTOVER, lat=50.078, lon=14.450),
        entries_for("sreality"))
    with conn.cursor() as cur:
        inserted, enqueued = write_result(cur, result)

    claim_statements = [s for s, _ in conn.executed if "INSERT INTO location_claims" in s]
    assert len(claim_statements) == 1
    one = claim_statements[0]
    assert "INSERT INTO dirty_locations" in one
    assert "'claim_insert'" in one
    assert (inserted, enqueued) == (1, 1)


def test_the_lane_writes_claims_and_nothing_else():
    """Rule 25. `location_claim_observations` (263 M rows / 50 GB),
    `location_claim_absences` and `location_enrichment_state` were written by every lane and
    read by none. No SQL constant in this module may name one of them again — a prose
    reference in a comment explaining WHY they are gone is fine, an INSERT is not.
    W1-a stopped writing them; migration 497 dropped them."""
    dead = ("location_claim_observations", "location_claim_absences",
            "location_enrichment_state")
    statements = [v for name, v in vars(claims_intake).items()
                  if name.endswith("_SQL") and isinstance(v, str)]
    assert statements
    for sql in statements:
        for table in dead:
            assert table not in sql, table


def test_the_claim_write_dedupes_within_the_batch():
    """Two listings can legitimately produce the same fingerprint (the tuple is time-free);
    `ON CONFLICT` cannot arbitrate two rows inside ONE statement, so the batch dedupes."""
    assert "DISTINCT ON (claim_fingerprint)" in _CLAIM_WRITE_SQL


def test_a_licence_refusal_is_counted_not_recorded():
    """A withheld coordinate used to be an absence ROW per listing. It is a counter on the
    result and one log line per reason per batch now — the thing an operator reads."""
    row = listing("sreality", SREALITY_TRUNCATED, lat=50.078, lon=14.450,
                  in_mapy_inventory=True)
    result = extract_listing(row, entries_for("sreality"))
    assert result.claims == []
    assert dict(result.refusals) == {
        "coordinate_withheld:listing_in_mapy_affected_inventory": 1,
        "sreality_payload_shape:absent": 1,
    }
    conn = _Conn()
    with conn.cursor() as cur:
        write_result(cur, result)
    assert [s for s, _ in conn.executed] == []


def test_batch_queries_are_keyset_and_bounded():
    full = " ".join(_LISTINGS_FULL_SQL.split())
    incremental = " ".join(_LISTINGS_INCREMENTAL_SQL.split())
    assert "l.id > %(after_id)s" in full and "ORDER BY l.id LIMIT" in full
    assert "(l.last_seen_at, l.id) > (%(after_ts)s, %(after_id)s)" in incremental
    assert "l.last_seen_at >= %(watermark)s" in incremental
    # Both walk active AND inactive LISTINGS: a delisted listing's payload is still
    # evidence, and nothing is ever deleted (CLAUDE.md rule 3). The only `is_active` in
    # either query is `portal_contracts.is_active`, which picks the portal's live contract.
    for one in (full, incremental):
        assert "l.is_active" not in one
        assert one.count("is_active") == 1 and "pc.is_active" in one
    assert MIN_BATCH_SIZE == 10_000 and MAX_BATCH_SIZE == 30_000


def test_the_batch_queries_select_the_legacy_columns_the_readers_consume():
    """06 §6.1.3's class-B columns are a second substrate beside `raw_json`, so they ride
    on the SAME keyset query — one extra SELECT item, never a per-row lookup. The record
    unpack is positional, so a column added to one query and not to `LEGACY_COLUMNS` (or to
    only one of the two queries) has to fail here rather than mid-run.

    `listings.street_source` is selected even though nothing reads it as a value: it is the
    guard column that decides whether `listings.street` is class B or class D, and a guard
    whose column the scan never fetched is a refusal (`_legacy_column`), not a claim.
    """
    for sql in (_LISTINGS_FULL_SQL, _LISTINGS_INCREMENTAL_SQL):
        one = " ".join(sql.split())
        for column in LEGACY_COLUMNS:
            assert f"l.{column.removeprefix('listings.')}" in one, column

    row, body, unmined, version = _row_from_record(_RECORD)
    assert row.legacy_columns == {
        "listings.locality": None,
        "listings.street": "Svatoplukova",
        "listings.street_source": "parser",
    }
    assert row.lat is None and row.in_mapy_inventory is False
    assert (body.id, body.page_kind, unmined, version) == (91, "detail", True, 5)

    # A record whose legacy tail has drifted from LEGACY_COLUMNS shifts every value one
    # position; `zip(strict=True)` is what turns that into a crash on the first row.
    with pytest.raises(ValueError):
        _row_from_record(_RECORD[:-1])


def test_the_claim_write_carries_the_class_b_confidence_as_a_typed_enum():
    """`claim_confidence` is a `match_confidence` column (migration 382), and the resolver's
    survivorship reads it. Binding it as bare text would fail the INSERT; leaving it out of
    the recordset would silently drop 06 §6.1.1's cap."""
    one = " ".join(_CLAIM_WRITE_SQL.split())
    assert "claim_confidence text" in one
    assert "d.claim_confidence::match_confidence" in one


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
    """The lane reads `listings` and writes only location_* / dirty_locations — plus the
    one mined-at stamp on `portal_raw_payloads`, which is the body store it just read."""
    for sql in (_CLAIM_WRITE_SQL, _BATCH_INSERT_SQL, _STAMP_MINED_SQL):
        lowered = sql.lower()
        for verb in ("insert into", "update ", "delete from"):
            for fragment in lowered.split(verb)[1:]:
                target = fragment.strip().split()[0].strip("(")
                assert target.startswith(
                    ("location_", "dirty_locations", "portal_raw_payloads")), target


# --------------------------------------------- the second substrate and its hash gate

# One scan row, in the order both batch queries select: the listing, its Mapy-inventory
# membership, its LATEST stored detail body (id, unmined?, page_kind, sha, first seen), the
# portal's ACTIVE contract version, then the legacy-column TAIL.
_RECORD = (7, "ceskereality", "3822640", {"id": "3822640"},
           datetime(2026, 8, 13, 6, 0, tzinfo=UTC), None, None, False,
           91, True, "detail", "ab" * 32, datetime(2026, 8, 13, 5, 0, tzinfo=UTC), 5,
           None, "Svatoplukova", "parser")


def test_the_scan_joins_the_latest_stored_detail_body_per_portal_key():
    """`portal_raw_payloads.listing_id` is nullable and nothing has ever populated it
    (`scraper.db.append_payload_if_enabled` passes None), so an inner join on it would match
    ZERO rows over the whole archive — and the lane would not raise: the first batch would
    come back empty and the batch would stamp 'ok'. The join is on `(source,
    source_id_native)`, which both writers populate and which is UNIQUE on `listings` too."""
    for sql in (_LISTINGS_FULL_SQL, _LISTINGS_INCREMENTAL_SQL):
        one = " ".join(sql.split())
        assert "LEFT JOIN LATERAL" in one
        assert "p.source = l.source AND p.source_id_native = l.source_id_native" in one
        assert "p.listing_id" not in one
        # "LATEST" IS `last_observed_at`. The store is content-addressed and
        # append-on-change, so a page that goes A -> B -> A appends no third row: it
        # collides on A's sha and bumps A's `last_observed_at`. Ordering by FIRST
        # observation would leave B permanently "latest" while the portal has served A for
        # weeks — the lane would mine a body the page no longer has. Never `version_seq`
        # either: 403 added that counter with no backfill, so every older body is NULL.
        assert "ORDER BY p.last_observed_at DESC, p.id DESC LIMIT 1" in one
        assert "p.first_observed_at DESC" not in one
        assert "p.page_kind = 'detail'" in one
        # Only OK bodies: idnes' 503 interstitial carries no claim anyone can mine.
        assert "p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299" in one


def test_the_hash_gate_is_is_distinct_from_against_the_portals_own_active_contract():
    """THE bound on the page half. A body is immutable and content-addressed, so it only has
    to be mined once per contract version; `contract_version` (migration 403, never
    populated) is the marker. NULL is DISTINCT FROM every version, so a NEW body is always
    eligible and a contract bump re-mines every latest body over the runs that follow."""
    for sql in (_LISTINGS_FULL_SQL, _LISTINGS_INCREMENTAL_SQL):
        one = " ".join(sql.split())
        assert "(pb.contract_version IS DISTINCT FROM pc.version)" in one
        # Per PORTAL, resolved in SQL: one portal's version can never gate another's body.
        assert "LEFT JOIN portal_contracts pc ON pc.source = l.source AND pc.is_active" in one


def test_a_body_already_at_the_active_version_is_not_a_candidate():
    """The whole point of the gate: no R2 round trip for a body this contract already
    mined. The SQL computes it; `_row_from_record` carries the verdict through."""
    _, body, unmined, _ = _row_from_record(_RECORD)
    assert body is not None and unmined is True
    mined = (*_RECORD[:9], False, *_RECORD[10:])
    _, body, unmined, _ = _row_from_record(mined)
    assert body is not None and unmined is False


def test_a_listing_with_no_stored_body_yields_no_candidate():
    bodiless = (*_RECORD[:8], None, None, None, None, None, 5, *_RECORD[14:])
    row, body, unmined, version = _row_from_record(bodiless)
    assert body is None and unmined is False and version == 5
    assert row.listing_id == 7


def test_mining_a_body_stamps_it_at_the_version_that_mined_it_in_the_same_transaction():
    """A rolled-back batch must un-stamp its bodies too, or the next run would skip claims
    that were never written."""
    conn = _Conn()
    with conn.cursor() as cur:
        stamp_mined_bodies(cur, [{"id": 91, "version": 5}])
    sql, params = conn.executed[-1]
    assert sql.startswith("UPDATE portal_raw_payloads")
    assert "SET contract_version = v.version" in sql
    assert params["rows"].obj == [{"id": 91, "version": 5}]
    assert "portal_raw_payloads" in " ".join(_STAMP_MINED_SQL.split())


def test_an_empty_stamp_list_runs_no_statement():
    conn = _Conn()
    with conn.cursor() as cur:
        stamp_mined_bodies(cur, [])
    assert conn.executed == []


def test_the_registry_is_one_and_matches_the_contract_record_exactly():
    """ONE registry, 24 readers: the 10 that read `listings.raw_json` and the 14 that read
    the stored page body. `ARCHIVE_ONLY_READERS` / `LLM_ONLY_READERS` were name-only mirrors
    of registries this module could not import; there is nothing left to mirror, so a name
    that is not in `READERS` is a deploy error again — one question, one answer."""
    from location_data import contracts, page_readers

    assert len(claims_intake.READERS) == 24
    assert set(claims_intake.READERS) == set(contracts.READER_CONTRACTS)
    payload_readers = {n for n, r in claims_intake.READERS.items()
                       if r.substrate == claims_intake.SUBSTRATE_PAYLOAD}
    page = {n for n, r in claims_intake.READERS.items()
            if r.substrate == claims_intake.SUBSTRATE_ARCHIVED_HTML}
    assert len(payload_readers) == 10 and page == set(page_readers.PAGE_READERS)
    assert not hasattr(claims_intake, "ARCHIVE_ONLY_READERS")
    assert not hasattr(claims_intake, "LLM_ONLY_READERS")
