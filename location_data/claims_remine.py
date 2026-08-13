"""W3 history backfill — re-mining location claims from `listing_snapshots.raw_json`.

Design: 06-migration-backfill.md §6.2.2 (the snapshot substrate — asymmetric by source),
§6.4 W3 (the gate this module ships against), §6.6 rules 1-2 (observation timestamps,
snapshot anchoring), §6.6.6 (the schema constructs this module writes against — all
already present, see migrations 380-389), 00-shared-contracts.md §3.3 (the canonical
`snapshot_anchor` vocabulary — this section is the tie-breaker wherever `06` and other
design files disagree). 01-schema.md §4.2 owns `location_claims`; this module restates
no DDL.

WHAT THIS LANE IS
  * Re-derive, not re-invent: it reuses W1's (`location_data.claims_intake`) readers,
    `Entry` / `ListingRow` / `Claim` / `Absence` shapes, licence ladder
    (`coordinate_verdict`), and batch/write SQL wholesale. A snapshot's `raw_json` is a
    verbatim historical copy of the SAME payload shape `listings.raw_json` holds today, so
    the SAME contract-entry readers apply unchanged — the only new things this module owns
    are (a) the scan/keyset over `listing_snapshots` instead of `listings`, (b) which claim
    types are admissible from that substrate per source (below), and (c) the
    snapshot-anchor / observation-time plumbing (`Claim.snapshot_id`,
    `Absence.snapshot_id`, both additive fields on the shared dataclasses).
  * A ONE-TIME backfill over the 1,574,313 `listing_snapshots` rows that exist today, but
    re-runnable and incremental like every other location lane (06 §6.7's cross-wave risk:
    "the corpus is live and growing... every wave must be re-runnable or the backfill
    never converges against ingest") — new snapshots keep landing hourly behind sreality's
    own ingest.
  * Claim-fingerprint dedup is TIME-FREE (01 §4.2.1) and does not depend on `snapshot_id`,
    so re-mining a value W1 (or an earlier W3 run) already wrote is a no-op on
    `location_claims` (ON CONFLICT DO NOTHING) and instead appends exactly one
    `location_claim_observations` row — a genuine re-sighting, not a duplicate. A value
    that changed between two snapshots gets its own new `location_claims` row, anchored at
    the snapshot where it first appears. Querying a listing's claims of one `claim_type`
    ordered by `first_observed_at` IS the precision/coordinate time series W3's gate wants
    visible.

COORDINATE-HISTORY SCOPING (06 §6.2.2, resolved against ground truth)
  06 §6.2.2's per-source re-extract table hedges mmreality's coordinate with "but only
  where those fields participated in the hash" — genuine uncertainty in the design corpus,
  flagged for the implementer to resolve. Ground truth (`scraper/scraped_listing.py`
  `_HASH_FIELDS`, the SAME allowlist all eight non-sreality portals share, confirmed by
  `bezrealitky_parser.py` and `mmreality_parser.py` both constructing `ScrapedListing`)
  settles it: `lat`/`lon`/`street`/`house_number`/`zip` are explicitly EXCLUDED from the
  hash for all eight, so a coordinate-only change never appends a snapshot for ANY of
  them — mmreality and bezrealitky included. Structurally, it is worse than that for the
  six `geom_column`-substrate portals (bazos/idnes/ceskereality/realitymix/maxima/remax):
  `listing_snapshots` carries no `geom`/`lat`/`lon` column at all (migration 001 + 320), and
  06 §6.1.3 itself states their raw_json never carries the coordinate VALUE, only the
  provenance METHOD — so there is no historical value to read for them at any snapshot,
  full stop, independent of the hash question. Only sreality (payload-anchored,
  `locality.gps_lat/gps_lon`) and mmreality/bezrealitky (payload-anchored,
  `property.point{}` / `gps{}`) carry the coordinate VALUE inside `raw_json` at all — but
  per the hash fact above, only sreality's presence in a snapshot is EVIDENCE of a location
  check, so only sreality gets `claim_type='coordinate'` claims here.
  06 §6.4's W3 gate is explicit and singular on this: "The 8 hash-excluded portals are
  stamped `history_completeness='locality_text_only'`" (one value, eight portals — not the
  richer `payload_only` split `location_data.claims_intake.HISTORY_COMPLETENESS` uses for
  W1's CURRENT-state claims, which answers a different question: "is the payload present"
  vs W3's "does a snapshot's existence mean this field was checked"). This module's own
  `W3_HISTORY_COMPLETENESS` mapping is therefore NOT the same dict, on purpose. Every
  non-coordinate claim type the contract yields for mmreality/bezrealitky (obec_code,
  cast_obce_name, precision_declaration, ...) is still mined — the restriction is on
  `claim_type='coordinate'` specifically, enforced by filtering it out of the entries
  handed to `extract_listing()` for the eight, not by a special case in a reader.
  The 06 §6.4 W3 gate's "the licence ladder is applied to historical coordinate stamps
  too: a snapshot whose `coords.source='geocode'` yields no coordinate claim" reads, given
  the above, as the general R2-inventory veto (06 §6.1.2) applying uniformly to every
  coordinate claim this lane CAN produce (sreality's), not a claim that the six
  geom_column portals get any coordinate mining at all — reused unchanged via
  `coordinate_verdict()` / `ListingRow.in_mapy_inventory`, the SAME mechanism W1 uses.

WHAT THIS LANE NEVER WRITES
  * `location_enrichment_state` (the LIVE refetch cohort) — a snapshot cannot be refetched;
    only the CURRENT listing can, and W1's own hourly intake already owns that. Enforced
    two ways: `extract_listing(..., route_legacy_shape_to_refetch=False)` stops the
    sreality-legacy-shape tail from enrolling a historical row, and this module drops
    `IntakeResult.enrichment` outright after extraction (the oversized-value guard inside
    `extract_listing()` is not flag-gated, since a value too large to write is a W1
    concern too — the drop here is the second rail).
  * A `legacy_column` claim of any kind. `listings.locality` / `.street` /
    `.street_source` are current-state-only columns with no historical shadow, so every
    `SnapshotRow` carries a dummy `legacy_columns` mapping (every `LEGACY_COLUMNS` key
    present, every value `None`) — `_read_legacy_text_column` sees every key it looks up
    and reads `None`, so it degrades to "no claim" rather than raising `IntakeRefused` for
    an absent key (`claims_intake._legacy_column`'s scan/contract-mismatch guard).

CLI:
    python -m location_data.claims_remine --mode full --max-seconds 2400
    python -m location_data.claims_remine --mode incremental --source sreality
Required: SUPABASE_DB_URL. Requires migrations 380-389 (same as W1) and an active portal
contract per source — nothing new. No new migration ships with this module (06 §6.6.6:
every construct W3 needs — `snapshot_anchor`, `snapshot_id`, `history_completeness` — is
already on `location_claims` / `location_claim_absences` / `location_claim_observations`;
only the WRITE PATH needed to learn to populate them, which is the additive change this
module's PR makes to `location_data.claims_intake`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import psycopg

from location_data import loader_db
from location_data.claims_intake import (
    DEFAULT_BATCH_SIZE,
    LEGACY_COLUMNS,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    SOURCES,
    Entry,
    IntakeRefused,
    IntakeResult,
    ListingRow,
    _ACTIVE_CONTRACT_SQL,
    _BATCH_FINISH_SQL,
    _BATCH_INSERT_SQL,
    _RESUME_SQL,
    _WATERMARK_SQL,
    assert_inventory_ready,
    extract_listing,
    guarded,
    json_pointer,
    load_entries,
    missing_relations,
    write_result,
)
from location_data.claims_intake import _number as intake_number
from location_data.resolver import lease
from scraper import db

LOG = logging.getLogger("location_data.claims_remine")

# Bumped whenever this lane's extraction SEMANTICS change (which entries are admitted,
# how a claim is re-stamped). Rides in `location_claim_batches.extractor_version` and in
# every absence/enrichment row's `extractor_version` — kept OUT of the per-claim
# fingerprint tuple, which stays `entry.extractor_version` (`contract:{source}@{version}`,
# 06 comment in `claims_intake._base`), so a value re-mined here that equals what W1
# already wrote dedupes onto the SAME claim row rather than forking a parallel identity.
REMINE_VERSION = "claims_remine@1"
LANE = "location_claims_remine"
WAVE = "W3"

# 00 §14 names the existing lanes (`location-resolve`, `location-collision`,
# `location-campaign`, `location-llm`, plus intake's own `location-claims` GH group); this
# is the one this module adds, following the same convention.
JOB_NAME = "location_claims_remine"
CONCURRENCY_GROUP = "location-remine"
DEFAULT_LEASE_TTL_S = 3600

# See "COORDINATE-HISTORY SCOPING" above. sreality is the ONLY portal whose
# `_HASH_FIELDS` allowlist includes a coordinate-adjacent signal (the whole estate blob,
# `repo-scrapers.md` §0.10) — every other source's snapshot existing is not evidence its
# coordinate was checked, and six of the eight have no coordinate VALUE in raw_json at all.
COORDINATE_HISTORY_SOURCES = frozenset({"sreality"})

# 06 §6.4 W3 gate, verbatim: "sreality: a precision/coordinate time series exists" /
# "the 8 hash-excluded portals are stamped `history_completeness='locality_text_only'`".
# Deliberately NOT `claims_intake.HISTORY_COMPLETENESS` — that dict answers "how complete
# is the CURRENT payload" (W1); this answers "does a snapshot's existence mean this field
# was checked" (W3), and the two questions have different answers for mmreality and
# bezrealitky (W1: `payload_only`; W3: `locality_text_only`, per the ground-truth hash
# fact above).
W3_HISTORY_COMPLETENESS: dict[str, str] = {
    source: ("full" if source in COORDINATE_HISTORY_SOURCES else "locality_text_only")
    for source in SOURCES
}

# Every `LEGACY_COLUMNS` key present, every value NULL — see "WHAT THIS LANE NEVER
# WRITES" above. A plain module-level constant: it never varies per row, and rebuilding a
# dict of the same three keys 1.57M times would be pure waste.
_DUMMY_LEGACY_COLUMNS: dict[str, Any] = dict.fromkeys(LEGACY_COLUMNS, None)

STATEMENT_TIMEOUT_ENV = "LOCATION_REMINE_TIMEOUT_S"
DEFAULT_STATEMENT_TIMEOUT_S = 600
_FAILURE_STAMP_TIMEOUT_S = 30
DEFAULT_OVERLAP_HOURS = 3


def _entries_for_remine(entries: list[Entry], source: str) -> list[Entry]:
    """06 §6.2.2's asymmetry, applied at the INPUT (same discipline as the licence ladder
    in `claims_intake`: a filter, not a post-hoc audit). A coordinate-typed entry is simply
    never handed to the reader loop for the other eight sources, so there is no code path
    that could accidentally emit one — no per-reader special case to keep in sync."""
    if source in COORDINATE_HISTORY_SOURCES:
        return entries
    return [e for e in entries if e.claim_type != "coordinate"]


def _payload_lat_lon(raw_json: dict[str, Any], entries: list[Entry]) -> tuple[float | None, float | None]:
    """Peek at a snapshot payload's own coordinate, via whichever entry declares the
    `point_pair` reader — driven by the SAME contract locator `_read_point_pair` uses, so
    this never drifts into a hardcoded per-portal pointer. Exists ONLY to feed
    `extract_listing()`'s existing withheld-coordinate absence heuristic (it keys off
    `row.lat`/`row.lon`, which `claims_intake` derives from CURRENT `listings.geom` — a
    column `listing_snapshots` does not carry, so a `SnapshotRow` has to source the same
    signal from the payload it actually holds). Returns (None, None) for every
    `geom_column`-substrate source, correctly: they have no coordinate in raw_json at all
    (06 §6.1.3), so there is nothing to withhold an absence FOR."""
    for entry in entries:
        if entry.reader == "point_pair":
            lat = intake_number(json_pointer(raw_json, str(entry.locator["lat_pointer"])))
            lon = intake_number(json_pointer(raw_json, str(entry.locator["lon_pointer"])))
            if lat is not None and lon is not None:
                return lat, lon
    return None, None


def remine_snapshot(
    snapshot_id: int,
    row: ListingRow,
    entries: list[Entry],
    *,
    max_value_bytes: int | None = None,
) -> IntakeResult:
    """Everything this lane knows about one snapshot. Pure — no DB, no clock, no network.

    Delegates to `claims_intake.extract_listing()` unchanged (reuse, not a fork) and then
    re-stamps its output with the snapshot anchor: `snapshot_anchor='snapshot'` +
    `snapshot_id` (01 §4.2's `loc_claim_anchor` CHECK requires the pair), and this lane's
    OWN `history_completeness` (see the module docstring — deliberately not what
    `claims_intake._base` would have stamped from its own dict). `Claim`/`Absence` are
    frozen dataclasses, so re-stamping is `dataclasses.replace`, never a mutation.
    """
    scoped_entries = _entries_for_remine(entries, row.source)
    result = extract_listing(
        row, scoped_entries, max_value_bytes=max_value_bytes,
        route_legacy_shape_to_refetch=False)
    completeness = W3_HISTORY_COMPLETENESS[row.source]
    result.claims = [
        replace(c, snapshot_id=snapshot_id, snapshot_anchor="snapshot",
                history_completeness=completeness)
        for c in result.claims
    ]
    result.absences = [replace(a, snapshot_id=snapshot_id) for a in result.absences]
    # Never a live refetch target — see the module docstring's "WHAT THIS LANE NEVER
    # WRITES". The oversized-value guard inside extract_listing() is not flag-gated (it is
    # a W1 concern too), so this is the second rail against it leaking into W3's output.
    result.enrichment = []
    return result


# ------------------------------------------------------------------ SQL

# No local relation-existence check: `missing_relations()` (reused from `claims_intake`)
# already establishes migrations 380-389 are applied, and migrations apply strictly in
# order, so `listing_snapshots` (migration 001) is necessarily present whenever that check
# passes — a second check here would only ever be dead code.

# Keyset over the WHOLE snapshot table — active and inactive listings alike, exactly the
# same discipline as claims_intake's full scan (a delisted row's history is exactly the
# evidence this wave exists to recover). The inner join to `listings` silently skips any
# `listing_snapshots` row whose `listing_id` predates migration 320's backfill (never NULL
# for a row scraped since); querying that count is deliberately NOT a preflight check here
# — an unindexed `COUNT(*) WHERE listing_id IS NULL` over 1.57M rows is exactly the
# full-table-aggregate failure mode 06 §6.7 warns W3 to size batches under.
_SNAPSHOTS_FULL_SQL = """
    SELECT s.id, s.listing_id, l.source, l.source_id_native, s.raw_json, s.scraped_at,
           (a.listing_id IS NOT NULL) AS in_mapy_inventory
    FROM listing_snapshots s
    JOIN listings l ON l.id = s.listing_id
    LEFT JOIN mapy_affected a ON a.listing_id = s.listing_id
    WHERE s.id > %(after_id)s
      AND (%(source)s::text IS NULL OR l.source = %(source)s)
    ORDER BY s.id
    LIMIT %(batch_size)s
"""

_SNAPSHOTS_INCREMENTAL_SQL = """
    SELECT s.id, s.listing_id, l.source, l.source_id_native, s.raw_json, s.scraped_at,
           (a.listing_id IS NOT NULL) AS in_mapy_inventory
    FROM listing_snapshots s
    JOIN listings l ON l.id = s.listing_id
    LEFT JOIN mapy_affected a ON a.listing_id = s.listing_id
    WHERE s.scraped_at >= %(watermark)s
      AND (s.scraped_at, s.id) > (%(after_ts)s, %(after_id)s)
      AND (%(source)s::text IS NULL OR l.source = %(source)s)
    ORDER BY s.scraped_at, s.id
    LIMIT %(batch_size)s
"""


def _row_from_snapshot_record(record: tuple[Any, ...]) -> tuple[ListingRow, int, datetime]:
    (snapshot_id, listing_id, source, native, raw_json, scraped_at, in_inventory) = record
    raw = raw_json if isinstance(raw_json, dict) else {}
    return (
        ListingRow(
            listing_id=int(listing_id),
            source=source,
            source_id_native=str(native) if native is not None else str(listing_id),
            raw_json=raw,
            # lat/lon filled in by the caller once the row's entries are known (the
            # `point_pair` locator lives on the contract, not on this record) —
            # `_payload_lat_lon` needs `entries`, which this function does not have.
            lat=None,
            lon=None,
            observed_at=scraped_at,  # 06 §6.6 Rule 1: the SNAPSHOT's own timestamp, never now().
            in_mapy_inventory=bool(in_inventory),
            legacy_columns=_DUMMY_LEGACY_COLUMNS,
        ),
        int(snapshot_id),
        scraped_at,
    )


def _resume_point(
    conn: psycopg.Connection, *, mode: str, source: str | None, watermark: datetime | None,
) -> dict[str, Any] | None:
    """`claims_intake._resume_point`'s logic, parametrized on THIS lane's `LANE` — that
    function hardcodes its own module-level `LANE` constant, so it cannot be reused as-is
    across two lanes sharing one `location_claim_batches` table."""
    with conn.cursor() as cur:
        cur.execute(_RESUME_SQL, {"lane": LANE, "source": source, "scan_mode": mode})
        row = cur.fetchone()
    if not row:
        return None
    outcome, after_id, after_ts, coverage_since = row
    if outcome != "stopped" or after_id is None:
        return None
    if mode == "incremental":
        if after_ts is None:
            return None
        if watermark is not None and after_ts < watermark:
            return None
    return {
        "after_id": int(after_id),
        "after_ts": after_ts if mode == "incremental" else None,
        "coverage_since": coverage_since,
    }


# ------------------------------------------------------------------ the run

def run(
    conn: psycopg.Connection,
    *,
    mode: str,
    source: str | None,
    batch_size: int,
    max_seconds: float | None,
    limit: int | None,
    start_after_id: int,
    overlap_hours: int,
    statement_timeout: int,
    dry_run: bool,
    note: str | None,
) -> dict[str, Any]:
    missing = missing_relations(conn)
    if missing:
        raise IntakeRefused(
            f"location schema not applied; missing {', '.join(missing)} "
            f"(migrations 380-389)")
    inventory_rows = assert_inventory_ready(conn)

    entries_by_source = load_entries(conn)
    wanted = [source] if source else list(SOURCES)
    unloaded = [s for s in wanted if not entries_by_source.get(s)]
    if unloaded:
        raise IntakeRefused(
            f"no ACTIVE portal contract for {', '.join(unloaded)}: git is the store of "
            f"record and the DB tables are its projection — run "
            f"`python -m location_data.contracts --load` (02 §2.1.8)")

    contract_id: int | None = None
    if source:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_ACTIVE_CONTRACT_SQL, {"source": source})
            row = cur.fetchone()
            contract_id = int(row[0]) if row else None

    watermark: datetime | None = None
    if mode == "incremental":
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_WATERMARK_SQL, {"lane": LANE, "source": source})
            row = cur.fetchone()
        watermark = row[0] - timedelta(hours=overlap_hours) if row and row[0] else None
        if watermark is None:
            LOG.info("REMINE no prior successful batch for source=%s; "
                     "incremental degrades to a full pass", source or "*")
            mode = "full"

    anchored = start_after_id > 0
    after_id = start_after_id
    after_ts = watermark
    resumed_from: dict[str, Any] | None = None
    if not anchored:
        resumed_from = _resume_point(conn, mode=mode, source=source, watermark=watermark)
        if resumed_from is not None:
            after_id = int(resumed_from["after_id"])
            if mode == "incremental" and resumed_from["after_ts"] is not None:
                after_ts = resumed_from["after_ts"]
            LOG.info("REMINE resuming a budget-stopped %s scan for source=%s from "
                     "after_id=%d after_ts=%s", mode, source or "*", after_id,
                     resumed_from["after_ts"])

    batch_id: int | None = None
    if not dry_run:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_BATCH_INSERT_SQL, {
                "lane": LANE, "source": source, "extractor_version": REMINE_VERSION,
                "contract_id": contract_id, "wave": WAVE,
                "job_run_id": os.environ.get("GITHUB_RUN_ID"), "note": note,
                "scan_mode": mode, "resumable": not anchored,
                "coverage_since": (resumed_from or {}).get("coverage_since"),
            })
            batch_id = int(cur.fetchone()[0])
    LOG.info("REMINE start mode=%s source=%s batch=%d inventory_rows=%d batch_id=%s",
             mode, source or "*", batch_size, inventory_rows, batch_id)

    started = time.monotonic()
    stats = {
        "snapshots": 0, "claims": 0, "claims_inserted": 0, "observations": 0,
        "enqueued": 0, "absences": 0, "oversized_values": 0,
        "stopped_early": False, "reached_end": False, "resumed_from_id": after_id,
    }
    try:
        while True:
            if limit is not None and stats["snapshots"] >= limit:
                stats["stopped_early"] = True
                break
            if max_seconds is not None and time.monotonic() - started > max_seconds:
                LOG.info("REMINE stopping: --max-seconds reached")
                stats["stopped_early"] = True
                break
            size = batch_size if limit is None else min(batch_size, limit - stats["snapshots"])

            with guarded(conn, statement_timeout) as cur:
                if mode == "incremental":
                    cur.execute(_SNAPSHOTS_INCREMENTAL_SQL, {
                        "watermark": watermark, "after_ts": after_ts, "after_id": after_id,
                        "source": source, "batch_size": size,
                    })
                else:
                    cur.execute(_SNAPSHOTS_FULL_SQL, {
                        "after_id": after_id, "source": source, "batch_size": size})
                records = cur.fetchall()
                if not records:
                    stats["reached_end"] = True
                    break

                result = IntakeResult()
                for record in records:
                    row, snapshot_id, scraped_at = _row_from_snapshot_record(record)
                    entries = entries_by_source.get(row.source)
                    if not entries:
                        continue
                    lat, lon = _payload_lat_lon(row.raw_json, entries)
                    row = replace(row, lat=lat, lon=lon)
                    result.extend(remine_snapshot(snapshot_id, row, entries))

                after_id = int(records[-1][0])
                if mode == "incremental":
                    after_ts = records[-1][5]
                stats["snapshots"] += len(records)
                stats["claims"] += len(result.claims)
                stats["absences"] += len(result.absences)
                stats["oversized_values"] += result.oversized
                if not dry_run and batch_id is not None:
                    inserted, observed, enqueued = write_result(
                        cur, result, batch_id=batch_id, extractor_version=REMINE_VERSION)
                    stats["claims_inserted"] += inserted
                    stats["observations"] += observed
                    stats["enqueued"] += enqueued
            LOG.info("REMINE progress snapshots=%d claims=%d inserted=%d observed=%d "
                     "absences=%d oversized=%d through_id=%d",
                     stats["snapshots"], stats["claims"], stats["claims_inserted"],
                     stats["observations"], stats["absences"], stats["oversized_values"],
                     after_id)
    except Exception as exc:
        if batch_id is not None:
            try:
                with guarded(conn, _FAILURE_STAMP_TIMEOUT_S) as cur:
                    cur.execute(_BATCH_FINISH_SQL, {
                        "batch_id": batch_id, "outcome": "failed",
                        "row_count": stats["claims_inserted"],
                        "cursor_after_id": after_id, "cursor_after_ts": after_ts,
                        "note": f"{type(exc).__name__}: {exc}"[:500],
                    })
            except Exception:  # noqa: BLE001 - never mask the exception being reported
                LOG.exception("REMINE could not stamp batch %s as failed", batch_id)
        raise

    outcome = "ok" if stats["reached_end"] else "stopped"
    stats["outcome"] = outcome
    if batch_id is not None:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_BATCH_FINISH_SQL, {
                "batch_id": batch_id,
                "outcome": outcome,
                "row_count": stats["claims_inserted"],
                "cursor_after_id": after_id,
                "cursor_after_ts": after_ts if mode == "incremental" else None,
                "note": f"snapshots={stats['snapshots']} stopped_early={stats['stopped_early']} "
                        f"reached_end={stats['reached_end']} through_id={after_id} "
                        f"oversized_values={stats['oversized_values']}",
            })
    stats["batch_id"] = batch_id
    stats["mode"] = mode
    stats["cursor_after_id"] = after_id
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--source", choices=SOURCES, default=None)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--start-after-id", type=int, default=0)
    parser.add_argument("--overlap-hours", type=int, default=DEFAULT_OVERLAP_HOURS)
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S))
    parser.add_argument("--lease-ttl-seconds", type=int, default=DEFAULT_LEASE_TTL_S)
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract and report; write nothing.")
    parser.add_argument("--note", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    batch_size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, args.batch_size))

    with db.connect() as conn:
        # Lease-row CAS, never an advisory lock (the transaction-mode pooler strands a
        # lock acquired on one backend and released on another — same rationale as
        # `location_data.resolver.lease`, restated in that module's own docstring). This
        # is a SECOND, ORTHOGONAL guard to the GH Actions `location-batch` /
        # `location-remine` concurrency groups: it also catches a manual local invocation
        # racing the scheduled workflow, which a GH-only concurrency group cannot.
        with lease.held(
            conn, JOB_NAME, cadence="1 hour", concurrency_group=CONCURRENCY_GROUP,
            ttl_seconds=args.lease_ttl_seconds,
        ) as acquired:
            if not acquired:
                LOG.info("REMINE skipped: another run holds the %s lease", JOB_NAME)
                return 0
            try:
                stats = run(
                    conn, mode=args.mode, source=args.source, batch_size=batch_size,
                    max_seconds=args.max_seconds, limit=args.limit,
                    start_after_id=args.start_after_id, overlap_hours=args.overlap_hours,
                    statement_timeout=args.statement_timeout, dry_run=args.dry_run,
                    note=args.note)
            except IntakeRefused as exc:
                print(f"REFUSED: {exc}", file=sys.stderr)
                return 2
    LOG.info("REMINE done %s", json.dumps(stats, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
