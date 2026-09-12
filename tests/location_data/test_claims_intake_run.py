"""The run loop: refusals, batch discipline, the hash gate, and what gets written.

No database. These assert the invariants that only the RUNNER can break — the lane-input
refusal, the single-statement atomicity of claim + `dirty_locations`, the keyset/watermark
contract of the two batch queries, and the `portal_raw_payloads.contract_version` gate that
bounds the page half by page churn rather than by corpus size.
"""

from __future__ import annotations

import inspect
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
    _LEGACY_WATERMARK_SQL,
    _SNAPSHOT_SEED_SQL,
    _STAMP_MINED_SQL,
    _UNMINED_BODIES_SQL,
    _UNMINED_BODY_BACKLOG_SQL,
    _UNMINED_WINDOW_SQL,
    _UNMINED_WINDOW_WHERE,
    BODIES_BUDGET_SHARE,
    DEFAULT_MAX_SECONDS,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    IntakeRefused,
    _row_from_record,
    assert_inventory_ready,
    extract_listing,
    main,
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


def test_the_enqueue_bumps_a_row_that_is_already_queued():
    """W2-a2. `DO NOTHING` here was total, silent loss of the re-mine: on 2026-09-12 the nine
    contract bumps re-mined ~60k listings whose rows were already queued from the previous
    sweep, so the enqueue was a no-op, the drain resolved them from the OLD claims and deleted
    the queue row — 384,500 answer rows, 135 with a town, and nothing left to re-enqueue them.
    The statement the lane actually executes must carry the bump, not just the constant."""
    conn = _Conn()
    result = extract_listing(
        listing("sreality", SREALITY_POST_CUTOVER, lat=50.078, lon=14.450),
        entries_for("sreality"))
    with conn.cursor() as cur:
        write_result(cur, result)

    one = next(s for s, _ in conn.executed if "INSERT INTO dirty_locations" in s)
    enqueue = " ".join(one.split()).lower().split("insert into dirty_locations")[1]
    assert "on conflict (listing_id) do nothing" not in enqueue
    assert "on conflict (listing_id) do update" in enqueue
    for fragment in ("set enqueued_at = now()", "reason = excluded.reason",
                     "attempts = 0", "next_eligible_at = now()"):
        assert fragment in enqueue, fragment


def test_the_lane_writes_claims_and_nothing_else():
    """Rule 25. `location_claim_observations` (263 M rows / 50 GB),
    `location_claim_absences` and `location_enrichment_state` were written by every lane and
    read by none. No SQL constant in this module may name one of them again — a prose
    reference in a comment explaining WHY they are gone is fine, an INSERT is not.
    W1-a stopped writing them; migration 498 dropped them."""
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
    # W1-a2: the incremental keyset walks the SNAPSHOT LOG, not `listings`.
    assert "FROM listing_snapshots s" in incremental
    assert "WHERE s.id > %(after_id)s" in incremental
    assert "ORDER BY s.id LIMIT %(batch_size)s" in incremental
    # The deleted watermark. `last_seen_at` moves for every active listing every few hours
    # (the index walks touch it), so selecting on it re-opened ~180 000 listings an hour.
    assert "last_seen_at >=" not in incremental
    assert "%(watermark)s" not in incremental and "%(after_ts)s" not in incremental
    # Both walk active AND inactive LISTINGS: a delisted listing's payload is still
    # evidence, and nothing is ever deleted (CLAUDE.md rule 3). The only `is_active` in
    # either query is `portal_contracts.is_active`, which picks the portal's live contract.
    for one in (full, incremental):
        assert "l.is_active" not in one
        assert one.count("is_active") == 1 and "pc.is_active" in one
    assert MIN_BATCH_SIZE == 10_000 and MAX_BATCH_SIZE == 30_000


def test_the_incremental_scan_dedupes_a_listing_that_changed_twice_in_one_window():
    """The window is a slice of the snapshot log; a listing with five snapshots in it is
    ONE row. The readers read `listings.raw_json` — the CURRENT payload — so extracting it
    once per snapshot would produce five identical fingerprints and four wasted reads."""
    one = " ".join(_LISTINGS_INCREMENTAL_SQL.split())
    assert "SELECT listing_id, max(id) AS snapshot_cursor FROM win GROUP BY listing_id" in one
    assert "JOIN listings l ON l.id = c.listing_id" in one


def test_the_source_filter_is_inside_the_snapshot_window():
    """Outside it, a source-scoped run whose window held no row for that portal would
    return zero listings — indistinguishable from "the log is exhausted" — and stamp `ok`
    with its cursor stuck. Inside it, every window row belongs to a returned listing, so
    `max(snapshot_cursor)` over the result IS the window's own high-water mark."""
    one = " ".join(_LISTINGS_INCREMENTAL_SQL.split())
    window = one.split("), changed AS")[0]
    assert "JOIN listings f ON f.id = s.listing_id AND (%(source)s::text IS NULL OR " \
           "f.source = %(source)s)" in window


def test_the_selections_carry_no_listings_column_the_lane_cannot_read():
    """W1-c deleted the class-B legacy columns from all THREE selections. The lane's
    substrates are `raw_json` and the stored page body, so a `listings` TEXT column in the
    select list would be bytes fetched for every row of a keyset scan that no reader can
    consume — 1 500 of them a batch on the bodies pass, which does not even project
    `raw_json`.

    The record unpack is positional and FIXED-WIDTH, so a column added to one selection and
    not the others (or to none of `_row_from_record`) has to fail here rather than mid-run.
    """
    for sql in (_LISTINGS_FULL_SQL, _LISTINGS_INCREMENTAL_SQL, _UNMINED_BODIES_SQL):
        one = " ".join(sql.split())
        for column in ("l.locality", "l.street", "l.street_source"):
            assert column not in one, column

    scan = _row_from_record(_RECORD)
    assert not hasattr(scan.row, "legacy_columns")
    assert scan.row.lat is None and scan.row.in_mapy_inventory is False
    assert (scan.body.id, scan.body.page_kind) == (91, "detail")
    assert (scan.body_unmined, scan.contract_version) == (True, 5)
    assert scan.snapshot_cursor == 4242

    # A record of the wrong width shifts every value one position; the fixed-width unpack
    # is what turns that into a crash on the first row.
    with pytest.raises(ValueError):
        _row_from_record(_RECORD[:-1])


def test_the_claim_write_carries_the_class_b_confidence_as_a_typed_enum():
    """`claim_confidence` is a `match_confidence` column (migration 382), and the resolver's
    survivorship reads it. Binding it as bare text would fail the INSERT; leaving it out of
    the recordset would silently drop 06 §6.1.1's cap."""
    one = " ".join(_CLAIM_WRITE_SQL.split())
    assert "claim_confidence text" in one
    assert "d.claim_confidence::match_confidence" in one


def test_the_lane_has_no_watermark_left_to_read():
    """W1-a2 deleted the time-based floor outright. No SQL constant in this module may ask
    a `location_claim_batches` timestamp where the scan should start — the cursor is the
    lane's only memory, and a second answer to "where do I begin" is how the two would
    drift."""
    statements = {name: v for name, v in vars(claims_intake).items()
                  if name.endswith("_SQL") and isinstance(v, str)}
    assert "_WATERMARK_SQL" not in statements
    # `coverage_since` survives in exactly one place: the CUTOVER seed, read once by a lane
    # that has no cursor of its own. Nothing writes it, and no run reads it twice.
    for name, sql in statements.items():
        if name == "_LEGACY_WATERMARK_SQL":
            continue
        assert "coverage_since" not in sql, name
    assert not hasattr(claims_intake, "DEFAULT_OVERLAP_HOURS")
    assert "'running'" in " ".join(_BATCH_INSERT_SQL.split())


def test_the_resume_lookup_is_per_source_per_mode_and_needs_a_cursor():
    sql = " ".join(_RESUME_SQL.split())
    assert "source IS NOT DISTINCT FROM %(source)s" in sql
    assert "scan_mode = %(scan_mode)s" in sql
    assert "cursor_after_id IS NOT NULL" in sql
    # A completed incremental run is resumed from too: its cursor is a position in an
    # append-only log, not a coverage claim, so restarting at 0 would re-walk the log.
    assert "outcome IN ('ok', 'stopped', 'failed')" in sql


def test_the_lane_writes_no_timestamp_cursor_and_that_is_the_epoch_marker():
    """Before W1-a2 an incremental cursor was `(last_seen_at, id)`; it is a bare
    `listing_snapshots.id` now. Reading an old LISTING id back as a SNAPSHOT id would skip
    every snapshot below it, so `cursor_after_ts IS NULL` is what tells the two apart — and
    it only works because nothing in this lane populates the column any more."""
    assert "cursor_after_ts" not in " ".join(_BATCH_FINISH_SQL.split())
    assert "cursor_after_ts" not in " ".join(_BATCH_INSERT_SQL.split())


def test_the_snapshot_window_stands_behind_the_wall_clock():
    """THE RACE. `listing_snapshots.id` is a bigserial: the id is allocated at INSERT and
    becomes visible at COMMIT, and `write_detail_batch` writes N snapshots inside one
    multi-statement transaction, concurrently across the per-portal drains and the realtime
    worker. A row whose id is BELOW an already-advanced cursor can therefore become visible
    after that cursor moved — and `s.id > after_id` never looks back, so that listing's
    content change is skipped PERMANENTLY. The window stands 15 minutes behind the clock;
    the seed takes the same predicate, or a cold start would jump over the in-flight ids
    instead of stopping short of them."""
    lag = "scraped_at < now() - interval '15 minutes'"
    assert f"s.{lag}" in " ".join(_LISTINGS_INCREMENTAL_SQL.split())
    assert lag in " ".join(_SNAPSHOT_SEED_SQL.split())


def test_the_cold_start_seeds_at_the_old_lane_s_own_anchor():
    """Seeding at the HEAD of the log would drop every change between the old lane's last
    position and this deploy — and the old lane's runs were being cancelled at the job
    timeout, so it has no `outcome='ok'` row for days and that window is hours wide. First
    arm: the newest batch row that still carries a `cursor_after_ts` (the pre-W1-a2 keyset's
    timestamp half). Second: the last `ok` watermark. Both minus the 3-hour overlap that
    cursor was always read with. Neither: the head."""
    sql = " ".join(_LEGACY_WATERMARK_SQL.split())
    assert sql.startswith("SELECT coalesce(")
    assert "b.cursor_after_ts IS NOT NULL" in sql
    assert "ORDER BY b.started_at DESC, b.id DESC LIMIT 1" in sql
    assert "max(coalesce(b.coverage_since, b.started_at))" in sql and "b.outcome = 'ok'" in sql
    assert sql.endswith("- interval '3 hours'")
    seed = " ".join(_SNAPSHOT_SEED_SQL.split())
    assert "coalesce(max(id), 0) FROM listing_snapshots" in seed
    assert "(%(watermark)s::timestamptz IS NULL OR scraped_at <= %(watermark)s)" in seed


def test_the_batch_row_carries_the_cursor_and_the_mode_that_wrote_it():
    """A full cursor is a bare `listings.id`; an incremental one is a
    `listing_snapshots.id`. Resuming one from the other would skip an arbitrary slice, so
    `scan_mode` rides on the row and the resume lookup filters on it."""
    finish = " ".join(_BATCH_FINISH_SQL.split())
    assert "cursor_after_id = %(cursor_after_id)s" in finish
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

# One scan row, in the order all THREE selections project: the listing, its Mapy-inventory
# membership, its LATEST stored detail body (id, unmined?, page_kind, sha, first seen), the
# portal's ACTIVE contract version and the snapshot cursor (NULL outside incremental mode),
# which is the LAST column since W1-c deleted the legacy-column tail that used to follow it.
_RECORD = (7, "ceskereality", "3822640", {"id": "3822640"},
           datetime(2026, 8, 13, 6, 0, tzinfo=UTC), None, None, False,
           91, True, "detail", "ab" * 32, datetime(2026, 8, 13, 5, 0, tzinfo=UTC), 5,
           4242)


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
    # The bodies-first pass asks the same question as a PREDICATE rather than as a
    # projected verdict — it selects only what the gate admits. It lives in the WINDOW (and
    # in the backlog readout, which is both statements' predicates rebuilt): the window IS
    # the eligibility gate, and the outer statement only resolves the ids it named.
    for sql in (_UNMINED_WINDOW_SQL, _UNMINED_BODY_BACKLOG_SQL):
        one = " ".join(sql.split())
        assert "AND p.contract_version IS DISTINCT FROM pc.version" in one
    # Per PORTAL, off the PAYLOAD row — `p.source`, not `l.source`: the window is planned
    # without `listings` at all, and the join is the same one on both sides of the fence.
    for sql in (_UNMINED_WINDOW_SQL, _UNMINED_BODIES_SQL, _UNMINED_BODY_BACKLOG_SQL):
        one = " ".join(sql.split())
        assert "JOIN portal_contracts pc ON pc.source = p.source AND pc.is_active" in one


def test_the_bodies_first_pass_walks_the_payload_table_on_an_in_run_keyset():
    """WHY NOT "no cursor, the stamp is the progress": four paths leave a body UNSTAMPED and
    three are deterministic per body, so with no cursor they sit at the head for ever —
    re-fetched every batch, and once `cap` of them accumulate the batch stamps nothing and
    the whole backlog stalls behind them, silently. The keyset walks past: `after_body_id`
    starts at 0 each run (the contract-version gate still decides what is eligible) and
    advances past every id the WINDOW named."""
    window = " ".join(_UNMINED_WINDOW_SQL.split())
    assert window.startswith("SELECT p.id FROM portal_raw_payloads p")
    assert "WHERE p.id > %(after_body_id)s" in window
    # A `pb.` would be a column this FROM clause does not have: the select list is derived
    # from the listing scan's by replacement, so drift has to fail here.
    assert "pb." not in " ".join(_UNMINED_BODIES_SQL.split())


def test_the_window_is_a_limit_subquery_that_never_touches_listings():
    """THE PLANNER PROOF (W1-a4). With the keyset in one statement's WHERE, Postgres planned
    the selection from `listings` — a bitmap scan of every active page-portal row, a payload
    probe and the latest-body subquery PER ROW, then a sort, with `p.id > after` applied as
    a POST-FILTER — so each batch paid the whole corpus (~150 s; run 34689928656 died on the
    600 s statement timeout, 2026-09-12). `ORDER BY p.id LIMIT %(cap)s` over payload columns
    ALONE is an optimizer fence: planned by itself it is an index scan of the payload
    primary key that stops after `cap` rows. A `listings` reference anywhere in it — even in
    a predicate that looks free — puts the join back inside the fence and the plan reverts.
    """
    window = " ".join(_UNMINED_WINDOW_SQL.split())
    assert window.endswith("ORDER BY p.id LIMIT %(cap)s"), (
        "the window must end at its own ORDER BY + LIMIT: that pair is the fence that "
        "pins the walk to portal_raw_payloads_pkey")
    for table in ("listings", "mapy_affected", " l.", "l.is_active"):
        assert table not in window, (
            f"{table!r} inside the window puts the join back inside the fence and the "
            "planner goes back to walking listings once per batch")
    # The outer statement resolves ONLY the ids the window named — no bound of its own, or
    # a window row dropped by the joins would silently shorten the batch.
    bodies = " ".join(_UNMINED_BODIES_SQL.split())
    assert "WHERE p.id = ANY(%(ids)s::bigint[])" in bodies
    assert "LIMIT" not in bodies


def test_a_listing_with_no_stored_body_yields_no_candidate():
    bodiless = (*_RECORD[:8], None, None, None, None, None, 5, None)
    scan = _row_from_record(bodiless)
    assert scan.body is None and scan.body_unmined is False
    assert scan.contract_version == 5 and scan.row.listing_id == 7


def test_the_bodies_first_pass_selects_the_latest_body_of_an_active_listing_only():
    """ACTIVE, unlike the listing scan. A delisted row's PAYLOAD is evidence we already
    hold; paying R2 for its stored page body ahead of ~250 000 live ones is not. The
    portals are a parameter, not a literal: only the sources whose contract declares a page
    entry have bodies worth fetching, and that set lives in the reader registry."""
    # The portal and page-kind filters are on the PAYLOAD row, so the window can ask them
    # without `listings` (the join makes `p.source` and `l.source` the same column anyway).
    # They are the WINDOW's job: the outer statement only resolves the ids it named.
    for sql in (_UNMINED_WINDOW_SQL, _UNMINED_BODY_BACKLOG_SQL):
        one = " ".join(sql.split())
        assert "p.page_kind = 'detail'" in one
        assert "p.source = ANY(%(page_sources)s::text[])" in one
        assert "(%(source)s::text IS NULL OR p.source = %(source)s)" in one
    for sql in (_UNMINED_BODIES_SQL, _UNMINED_BODY_BACKLOG_SQL):
        one = " ".join(sql.split())
        assert "AND l.is_active" in one
        # THE SAME definition of "the latest detail body" the listing scan's lateral
        # applies, asked from the other direction and as an ANTI-JOIN (one probe per
        # candidate id, not one per listing): `last_observed_at`, never `first_observed_at`
        # (a page that goes A -> B -> A appends no third row, it bumps A) and never
        # `version_seq` (403 added it with no backfill).
        assert "NOT EXISTS ( SELECT 1 FROM portal_raw_payloads p2" in one
        assert "(p2.last_observed_at, p2.id) > (p.last_observed_at, p.id)" in one
        assert "p2.page_kind = 'detail'" in one
    selection = " ".join(_UNMINED_BODIES_SQL.split())
    # No `raw_json`: the page substrate is the BODY, and a 1 500-row batch would otherwise
    # drag ~10 MB of payload over the wire to throw it away.
    assert "l.raw_json" not in selection
    assert "NULL::jsonb" in selection


def test_the_backlog_count_is_both_statements_predicates_rebuilt():
    """The summary's "backlog remaining" must be the same question the drain asks, or the
    operator reads a number that never reaches zero. The drain now asks it in two halves —
    the window's eligibility gate, then the active listing and the latest-body rule — so the
    readout is built from the same two pieces rather than written out again."""
    count = " ".join(_UNMINED_BODY_BACKLOG_SQL.split())
    assert count.startswith("SELECT count(*) FROM portal_raw_payloads p")
    gate = " ".join(_UNMINED_WINDOW_WHERE.split())
    assert gate in count and gate in " ".join(_UNMINED_WINDOW_SQL.split())
    # The readout passes `after_body_id = 0`, so it counts the whole backlog rather than
    # the remainder of the run — and carries no `cap`, so it is never a window.
    assert "%(after_body_id)s" in count and "%(cap)s" not in count


def test_the_backlog_readout_runs_after_the_terminal_stamp():
    """It is a `count(*)` under the 600 s ceiling, run when the budget is already spent.
    Ahead of the stamp it could push the job past `timeout-minutes: 55` and lose the cursor
    of a run that had otherwise finished cleanly — the exact failure this wave closes."""
    source = inspect.getsource(claims_intake.run)
    # rindex: the FIRST `_BATCH_FINISH_SQL` is the failure path inside the try.
    assert source.rindex("_BATCH_FINISH_SQL") < source.index("_unmined_body_backlog(")


def test_the_page_half_may_take_at_most_half_the_run_budget():
    """The backlog is bounded by the CORPUS and the payload half by the hour's change, so
    an undivided budget would let ~170 runs of body drain starve the claims the operator is
    actually waiting on."""
    assert BODIES_BUDGET_SHARE == 0.5


def test_a_run_always_has_a_budget_even_when_the_caller_forgets_one():
    """A `workflow_dispatch` without `max_seconds` ran unbounded, hit `timeout-minutes: 55`,
    was CANCELLED, and stamped nothing — so the next run restarted from the same cursor and
    the lane made no progress at all. The default lives in the CLI, not only in the
    workflow, because the workflow is not the only caller."""
    parser = claims_intake.build_parser()
    assert parser.parse_args([]).max_seconds == DEFAULT_MAX_SECONDS == 2400.0
    assert parser.parse_args(["--max-seconds", "600"]).max_seconds == 600.0
    # And the flag that made "incremental" mean "re-read the live corpus" is gone.
    with pytest.raises(SystemExit):
        parser.parse_args(["--overlap-hours", "3"])


def test_the_registry_is_one_and_matches_the_contract_record_exactly():
    """ONE registry, 21 readers: the 7 that read `listings.raw_json` and the 14 that read
    the stored page body. `ARCHIVE_ONLY_READERS` / `LLM_ONLY_READERS` were name-only mirrors
    of registries this module could not import; there is nothing left to mirror, so a name
    that is not in `READERS` is a deploy error again — one question, one answer.

    Three readers went in W1-c with the `legacy_column` surface they were the only users of
    (`legacy_text_column`, `geom_column`, `coords_stamp_quality`)."""
    from location_data import contracts, page_readers

    assert len(claims_intake.READERS) == 21
    assert set(claims_intake.READERS) == set(contracts.READER_CONTRACTS)
    payload_readers = {n for n, r in claims_intake.READERS.items()
                       if r.substrate == claims_intake.SUBSTRATE_PAYLOAD}
    page = {n for n, r in claims_intake.READERS.items()
            if r.substrate == claims_intake.SUBSTRATE_ARCHIVED_HTML}
    assert len(payload_readers) == 7 and page == set(page_readers.PAGE_READERS)
    assert not hasattr(claims_intake, "ARCHIVE_ONLY_READERS")
    assert not hasattr(claims_intake, "LLM_ONLY_READERS")
