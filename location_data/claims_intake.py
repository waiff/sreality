"""THE claim lane — one hourly pass that mines every substrate we hold for a listing.

WHAT THIS LANE IS (rule 25: one store, one lane, eleven claim types, no flags)
  * TWO HALVES, both bounded by the run budget, in this order every hour:
      - BODIES FIRST: the unmined latest detail bodies of active page-portal listings,
        drained straight out of `portal_raw_payloads` in 1 500-row batches on an in-run
        keyset over `p.id`, until the backlog is empty or half the budget is gone.
      - THEN THE CHANGED LISTINGS: a keyset scan of `listing_snapshots.id`, the
        append-on-content-change log (rule 2), joined back to `listings`, standing 15
        minutes behind the clock. What a run opens is an hour's CHANGE, not every
        listing the index walks re-sighted.
  * Two substrates, ONE registry (`READERS`), one write:
      - `listings.raw_json`, the portal's own payload as we stored it — the payload
        readers below;
      - the STORED PAGE BODY: the latest `portal_raw_payloads` detail row for the
        listing's `(source, source_id_native)`, fetched from R2 and scoped by the
        contract's exclusion zones — the 14 page readers in `location_data.page_readers`.
  * Contract-driven: every claim is stamped with the `portal_contract_entries` row that
    produced it, and the extractor executes exactly those entries whose `locator` names a
    reader from `READERS`. A name in NO registry is a hard refusal (a real deploy error).

WHY CHANGE-DRIVEN (W1-a2, measured on run 34658123746)
  The first production run selected on `listings.last_seen_at >= watermark`. Every active
  listing is re-sighted within hours by the index walks, so an "incremental" hour opened
  ~180 000 listings (9 batches x 20 000 in 51 min) and re-mined payloads whose claims
  already existed — `ON CONFLICT DO NOTHING` all the way down. A `listing_snapshots` row
  is appended exactly when a listing's CONTENT changes, and every write path into
  `listings` appends one (a brand-new row included: `scraper.db.upsert_listing` and
  `_BATCH_SNAPSHOT_SQL` both compare against a NULL latest hash), so the snapshot log is
  the exact set of payloads whose claims could have moved. The cursor is therefore a
  `listing_snapshots.id`, kept where the batch row already keeps its keyset position.

THE HASH GATE (why the hourly budget is bounded by page CHURN, not by corpus size)
  `portal_raw_payloads` is append-on-change: a body is one row, immutable, content-
  addressed. So a body only has to be mined ONCE per contract version, and
  `portal_raw_payloads.contract_version` is the marker that says it was: the scan joins the
  portal's ACTIVE contract and mines only where
  `contract_version IS DISTINCT FROM portal_contracts.version`. A new body arrives NULL
  there and is mined on the next run; a contract bump re-mines every latest body over the
  runs that follow; a body already at the active version is never fetched. No new table —
  the column existed (migration 403) and nothing ever populated it.

R2 IS OPTIONAL TO THE LANE, NOT TO THE PAGE HALF
  Bodies live in the bucket (99.7 % are spilled). If R2 is not configured the page half is
  skipped with ONE warning per run and the payload half runs exactly as before — the hourly
  lane must never go dark for all nine portals because a credential rotated.

THE LICENCE LADDER RUNS FIRST (§6.1.2, and it is a filter, not an audit)
  * `geocode` / bazos `street` / `locality` / absent provenance  -> class E, NO coordinate
    claim, ever. The payload substrate can only ever emit `licence_class = 'portal'`; the
    page substrate adds `'odbl'` for realitymix's Nominatim-fallback pin and nothing else.
  * `carry_forward` is provenance-laundering: admitted only when the listing is ABSENT
    from `mapy_affected` (migration 385 — the C7.2 R2 inventory, a lane INPUT).
  * If `mapy_affected` is missing or empty the lane REFUSES to run.

WHAT IT WRITES, AND ONLY THAT
  `location_claims` (append-only, deduped on `claim_fingerprint`), `dirty_locations`
  (the resolver's queue, inside the same transaction), `location_claim_batches` (this
  lane's run ledger and cursor), and the `contract_version` stamp on the bodies it mined.
  Refusals — a withheld coordinate, an oversized value, a subject miss — are COUNTED and
  logged once per reason per batch. `location_claim_observations`,
  `location_claim_absences` and `location_enrichment_state` were written by every lane and
  read by none; W1-a stopped writing them and migration 498 dropped them.

CLI:
    python -m location_data.claims_intake --mode incremental
    python -m location_data.claims_intake --mode full --source sreality --max-seconds 3000
Required: SUPABASE_DB_URL. Additionally requires migrations 380-387 + 403 and a projected,
active portal contract per source (`python -m location_data.contracts --load`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from location_data import loader_db, page_readers, payloads
# The names re-exported here are the ones something OUTSIDE this module imports from it —
# the contract validator's mirror gates (`TRANSFORMS`/`GUARDS`), the licence-ladder tables the
# portal tests score against, and the value objects. Anything the lane neither uses nor
# publishes is imported where it is needed instead; a re-export nobody reads is surface.
from location_data.claims_common import (  # noqa: F401 - the lane's public vocabulary
    ARCHIVED_COORDINATE_RULES,
    COORDINATE_RULES,
    DEFAULT_MAX_CLAIM_VALUE_BYTES,
    EMITTABLE_LICENCE_CLASSES,
    GUARD_CZ_BBOX,
    GUARDS,
    MAPY_COORDS_SOURCES,
    MIRROR_UNSAFE_CHARS,
    MAX_CLAIM_VALUE_BYTES_ENV,
    SOURCES,
    SUBSTRATE_ARCHIVED_HTML,
    SUBSTRATE_PAYLOAD,
    TRANSFORMS,
    Claim,
    Entry,
    IntakeRefused,
    IntakeResult,
    ListingRow,
    _base,
    _number,
    _text,
    apply_transforms,
    claim_value_bytes,
    coordinate_verdict,
    env_positive_int,
    envelope_wkt,
    guard_admits,
    json_pointer,
    mirror_is_faithful,
    point_wkt,
    sreality_payload_shape,
    value_norm_mirror,
)
from scraper import db, street

LOG = logging.getLogger("location_data.claims_intake")

# Bumped whenever the extraction SEMANTICS change. It rides in every claim's batch row;
# the per-claim `extractor_version` is the contract's own `contract:<portal>@<version>`
# (02 §2.1.8). @4 was the one-lane fold (the page body became a substrate of this lane);
# @5 is W1-a2 — the selection became change-driven and the page half got its own pass.
INTAKE_VERSION = "claims_intake@5"
LANE = "location_claims_intake"
WAVE = "W1"

MIN_BATCH_SIZE = 10_000
MAX_BATCH_SIZE = 30_000
DEFAULT_BATCH_SIZE = 20_000

# EVERY RUN HAS A BUDGET, and the default lives here rather than only in the workflow.
# A `workflow_dispatch` without one used to run unbounded, hit `timeout-minutes: 55`, be
# CANCELLED, and stamp NOTHING — so the next run restarted from the same cursor and the
# lane made no progress at all (run 34658123746). A budget is what makes a stop
# resumable; the job timeout is the backstop, not the mechanism.
DEFAULT_MAX_SECONDS = 2400.0

# The page half runs FIRST and may spend at most this share of the budget, so the payload
# half is never starved by a backlog drain (the first wave is ~250 000 unmined bodies at
# 1 500 a batch — ~170 runs, and every one of those runs still owes the operator an hour
# of change-driven payload claims).
BODIES_BUDGET_SHARE = 0.5

# Per-batch statement ceiling (seconds), env-overridable so a lane can be widened without
# a deploy. `_FAILURE_STAMP_TIMEOUT_S` is deliberately much shorter: a one-row UPDATE on
# the failure path must fail fast rather than become a second wedge on top of the first.
STATEMENT_TIMEOUT_ENV = "LOCATION_INTAKE_TIMEOUT_S"
DEFAULT_STATEMENT_TIMEOUT_S = 600
_FAILURE_STAMP_TIMEOUT_S = 30

# THE SCAN BATCH IS NOT THE WRITE SIZE.
#
# Every write in this module passes ONE jsonb array as ONE parameter to
# `jsonb_to_recordset`, and Postgres caps the total size of a jsonb array's elements at
# 256 MB: `total size of jsonb array elements exceeds the maximum of 268435455 bytes`
# (ProgramLimitExceeded). A 20 000-listing batch crossed it in production (Actions run
# 31482522487, the hourly incremental) — the incremental scan orders by `last_seen_at`, not
# `id`, which concentrated the geometry-heavy sreality rows into one batch where the earlier
# id-ordered full pass had diluted them across nine portals. The cap is a property of the
# WRITE, not of the scan, so shrinking the batch would only move the cliff: the arrays are
# flushed in chunks bounded by BOTH a row count and a cumulative serialized-byte budget,
# whichever trips first, all inside the same batch transaction as before.
WRITE_CHUNK_ROWS_ENV = "LOCATION_INTAKE_CHUNK_ROWS"
WRITE_CHUNK_BYTES_ENV = "LOCATION_INTAKE_CHUNK_BYTES"
DEFAULT_WRITE_CHUNK_ROWS = 5_000
DEFAULT_WRITE_CHUNK_BYTES = 32 * 1024 * 1024  # ~8x under the hard limit, per statement.

# ONE BODY FETCH PER BODY IS THE WHOLE COST OF THE PAGE HALF, and the fleet appends
# ~8k bodies a day (~50-80 an hour), so a run's fan-out is bounded by churn. This ceiling
# is the second rail, PER BATCH: a contract bump makes every latest body eligible at once,
# and without it one batch would try to pull the whole corpus through the bucket.
#
# 1500, not the scan batch: a batch is ONE transaction and every decompressed body in it is
# live in memory at once (41-245 KB each), so the bound is the runner's memory and the time
# the transaction sits idle across the fan-out, not the row count. 1500 is ~150 MB and ~20x
# an hour of churn — a bumped contract drains over a handful of batches instead of one
# multi-gigabyte transaction holding thousands of round trips open against the
# transaction-mode pooler. That is the lesson the deleted archive lane's own bounds carried
# (it refused to share W1's 10,000-row floor for exactly this reason).
BODY_FETCH_CAP_ENV = "LOCATION_INTAKE_BODY_CAP"
DEFAULT_BODY_FETCH_CAP = 1_500


# ------------------------------------------------------------------ THE registry

@dataclass(frozen=True, slots=True)
class Reader:
    """One row of THE reader registry: the substrate it takes, and the function.

    ONE registry, not three. The lane used to carry `READERS` (raw_json) plus two
    name-only mirrors — `ARCHIVE_ONLY_READERS` and `LLM_ONLY_READERS` — because the
    other two lanes could not be imported here without a cycle. The cycle is gone
    (`claims_common` holds the shared vocabulary), the LLM lane is gone, and a name that
    lives in a mirror rather than in the registry is a name nothing executes.
    """
    name: str
    substrate: str
    fn: Any


PayloadReaderFn = Callable[[Entry, ListingRow], list[Claim]]
ReaderFn = PayloadReaderFn
READERS: dict[str, Reader] = {}


def reader(name: str) -> Callable[[PayloadReaderFn], PayloadReaderFn]:
    """Register a `listings.raw_json` reader. The page readers fold in below."""
    def register(fn: PayloadReaderFn) -> PayloadReaderFn:
        READERS[name] = Reader(name, SUBSTRATE_PAYLOAD, fn)
        return fn
    return register


def payload_entries(entries: list[Entry]) -> list[Entry]:
    """The entries this lane executes against `listings.raw_json`."""
    return [e for e in entries
            if e.reader and READERS.get(e.reader) is not None
            and READERS[e.reader].substrate == SUBSTRATE_PAYLOAD]

@reader("scalar")
def _read_scalar(entry: Entry, row: ListingRow) -> list[Claim]:
    value = _text(json_pointer(row.raw_json, str(entry.locator["json_pointer"])))
    value = apply_transforms(value, entry.transform)
    if value is None:
        return []
    number = _number(value) if entry.locator.get("value_kind") == "num" else None
    return [_base(entry, row, value_text=value, value_num=number)]


@reader("conflict_signal")
def _read_conflict_signal(entry: Entry, row: ListingRow) -> list[Claim]:
    """A value the contract BANS as a subject source but keeps as contradiction evidence.

    remax `raw_json.address` is mis-sourced from the "Nemovitosti v okolí" carousel and
    reached `listings.street` on 2 rows [02 §2.2.6, live-B §0.3]. 03 §3.2 rule 4: store it
    with `subject_scoped = false` — inadmissible to survivorship, admissible to the
    contradiction ledger. Discarding it instead leaves the reconciler nothing to compare on
    the corpus's best-evidenced contamination class (2,144 of 4,918 street-bearing rows).
    """
    value = _text(json_pointer(row.raw_json, str(entry.locator["json_pointer"])))
    if value is None:
        return []
    return [_base(entry, row, value_text=value, subject_scoped=False,
                  legacy_source_column=str(entry.locator.get("legacy_source_column")
                                            or "raw_json.address"))]


@reader("namespaced_id")
def _read_namespaced_id(entry: Entry, row: ListingRow) -> list[Claim]:
    """`portal_admin_id` / `portal_street_id` values stay namespaced in the value
    (02 §2.1.6, 01 §12) so a portal id can never become a cross-portal query dimension."""
    value = _text(json_pointer(row.raw_json, str(entry.locator["json_pointer"])))
    value = apply_transforms(value, entry.transform)
    if value is None:
        return []
    namespace = str(entry.locator["namespace"])
    return [_base(entry, row, value_text=f"{namespace}={value}", value_num=_number(value),
                  value_jsonb={"namespace": namespace, "id": value})]


@reader("point_pair")
def _read_point_pair(entry: Entry, row: ListingRow) -> list[Claim]:
    """A first-party coordinate published inside the portal's own payload."""
    if row.source == "sreality" and sreality_payload_shape(row.raw_json) != "post_cutover":
        return []
    lat = _number(json_pointer(row.raw_json, str(entry.locator["lat_pointer"])))
    lon = _number(json_pointer(row.raw_json, str(entry.locator["lon_pointer"])))
    if lat is None or lon is None:
        return []
    verdict = coordinate_verdict(row.source, None, in_mapy_inventory=row.in_mapy_inventory)
    if not verdict.admitted:
        return []
    if not guard_admits(entry, GUARD_CZ_BBOX, (lat, lon)):
        return []
    return [_base(entry, row, value_geom_wkt=point_wkt(lat, lon),
                  licence_class=verdict.licence_class or "portal")]


@reader("declared_quality")
def _read_declared_quality(entry: Entry, row: ListingRow) -> list[Claim]:
    """A portal's own precision label -> `precision_declaration`, with the blur axis typed
    rather than flattened into the coordinate (06 §6.2.1). The reader states the LABEL;
    `_base` derives the blur axis from `precision_cap.blurred_labels` (W1-c R5), so
    re-calibrating it is a contract version bump, not a code change."""
    label = _text(json_pointer(row.raw_json, str(entry.locator["json_pointer"])))
    if label is None:
        return []
    return [_base(entry, row, value_text=label, declared_precision_label=label)]


@reader("declared_bool_quality")
def _read_declared_bool_quality(entry: Entry, row: ListingRow) -> list[Claim]:
    """mmreality `accurate` — present on 100% of rows, `false` on 37.2%, and stored
    nowhere today. The boolean is mapped to a LABEL by the contract (`locator.labels`);
    `_base` then derives the blur axis from that label's membership in the contract's
    `precision_cap.blurred_labels` (W1-c R5). Which of the two labels is blurred is a portal
    fact, so it is data on the entry — re-calibrating it is a contract version bump, not a
    code change, and the axis is written EXPLICITLY, never defaulted (06 §6.6 rule 7)."""
    raw = json_pointer(row.raw_json, str(entry.locator["json_pointer"]))
    if raw is None or not isinstance(raw, bool):
        return []
    labels = entry.locator.get("labels") or {"true": "accurate", "false": "not_accurate"}
    label = str(labels["true" if raw else "false"])
    return [_base(entry, row, value_text=label, declared_precision_label=label,
                  value_num=1.0 if raw else 0.0)]


@reader("bbox_envelope")
def _read_bbox_envelope(entry: Entry, row: ListingRow) -> list[Claim]:
    """sreality `locality.geometry.bounding_box` — "the bounding box, not the label, is the
    real precision measure": sample 520268 is `inaccuracy_type:"street"` with a bbox
    spanning ~15 km [db-raw §3.1]. Stored as the uncertainty geometry plus its verbatim
    envelope; the resolver derives `uncertainty_radius_m` from it (03 §3.8.3)."""
    node = json_pointer(row.raw_json, str(entry.locator["json_pointer"]))
    if not isinstance(node, dict):
        return []
    lat_min = _number(node.get("leftBottomLatitude"))
    lon_min = _number(node.get("leftBottomLongitude"))
    lat_max = _number(node.get("rightTopLatitude"))
    lon_max = _number(node.get("rightTopLongitude"))
    if None in (lat_min, lon_min, lat_max, lon_max):
        return []
    assert lat_min is not None and lon_min is not None
    assert lat_max is not None and lon_max is not None
    if lat_max < lat_min or lon_max < lon_min:
        return []
    if not guard_admits(entry, GUARD_CZ_BBOX, (lat_min, lon_min), (lat_max, lon_max)):
        return []
    return [_base(entry, row,
                  value_shape_wkt=envelope_wkt(lat_min, lon_min, lat_max, lon_max),
                  value_jsonb={"bounding_box": node,
                               "geometry_type": json_pointer(row.raw_json,
                                                             "/locality/geometry/geometry_type")})]


# ------------------------------------------------------------------ the value-size cap


# The 14 page readers fold into THE registry here, after the payload ones above have
# registered.
# Folded rather than mirrored by name: `location_data.page_readers` imports
# `claims_common`, never this module, so there is no cycle left to work around.
for _page_reader_name, _page_reader_fn in page_readers.PAGE_READERS.items():
    READERS[_page_reader_name] = Reader(
        _page_reader_name, SUBSTRATE_ARCHIVED_HTML, _page_reader_fn)
del _page_reader_name, _page_reader_fn


# ------------------------------------------------------------------ extraction

def _refuse_oversized(
    row: ListingRow, claims: list[Claim], *, max_value_bytes: int,
) -> tuple[list[Claim], list[str]]:
    """Partition off claims whose value exceeds the cap.

    The rail that survives a pathological single row: no chunk budget can split ONE array
    element, so a claim whose value alone dwarfs the budget would still be handed to
    Postgres verbatim. A value this large is not a location claim — it is a portal geometry
    blob that landed in `raw_json` — so it is refused at extraction time, counted, and
    logged."""
    kept: list[Claim] = []
    refusals: list[str] = []
    for claim in claims:
        size = claim_value_bytes(claim)
        if size <= max_value_bytes:
            kept.append(claim)
            continue
        LOG.warning("INTAKE oversized value refused listing_id=%d source=%s claim_type=%s "
                    "extractor_id=%s bytes=%d cap=%d",
                    row.listing_id, row.source, claim.claim_type, claim.extractor_id,
                    size, max_value_bytes)
        refusals.append(f"oversized_value:{claim.claim_type}")
    return kept, refusals


def extract_listing(
    row: ListingRow, entries: list[Entry], *, max_value_bytes: int | None = None,
) -> IntakeResult:
    """Everything the PAYLOAD substrate knows about one listing. Pure — no DB, no clock,
    no network. Page entries are this lane's too; they read a different substrate and are
    executed by `extract_page` once the body is in hand."""
    if max_value_bytes is None:
        max_value_bytes = env_positive_int(MAX_CLAIM_VALUE_BYTES_ENV,
                                           DEFAULT_MAX_CLAIM_VALUE_BYTES)
    result = IntakeResult()
    coordinate_entry: Entry | None = None

    for entry in payload_entries(entries):
        spec = READERS[str(entry.reader)]
        if entry.claim_type == "coordinate":
            coordinate_entry = entry
        for claim in spec.fn(entry, row):
            if claim.licence_class not in EMITTABLE_LICENCE_CLASSES:
                raise IntakeRefused(
                    f"{entry.entry_id} produced licence_class='{claim.licence_class}'; "
                    f"the payload substrate may only emit "
                    f"{sorted(EMITTABLE_LICENCE_CLASSES)} (06 §6.6 rule 6)")
            result.claims.append(claim)

    result.claims, oversized = _refuse_oversized(
        row, result.claims, max_value_bytes=max_value_bytes)
    for reason in oversized:
        result.refuse(reason)

    withheld = (row.lat is not None and row.lon is not None
                and not any(c.claim_type == "coordinate" for c in result.claims))
    if coordinate_entry is not None and withheld:
        verdict = coordinate_verdict(
            row.source, _text(json_pointer(row.raw_json, "/coords/source")),
            in_mapy_inventory=row.in_mapy_inventory)
        if not verdict.admitted:
            # A coordinate the ladder refused must not read as "the portal published
            # none" — it is counted under its own reason so the class-E cohort stays
            # visible in the run log.
            result.refuse(f"coordinate_withheld:{verdict.reason}")

    if row.source == "sreality":
        shape = sreality_payload_shape(row.raw_json)
        if shape != "post_cutover":
            # 06 §6.2.1 caveat: a legacy-shape row can never yield
            # zip/housenumber/entity_type/inaccuracy_type and a truncated one lost the
            # locality object outright.
            result.refuse(f"sreality_payload_shape:{shape}")
    return result


def extract_page(
    payload: page_readers.ArchivedPayload, row: ListingRow, entries: list[Entry], *,
    register: page_readers.ScopeRegister, max_value_bytes: int | None = None,
) -> IntakeResult:
    """Everything the PAGE substrate knows about one stored body. Re-exported so the lane
    has one extraction surface; the readers themselves live in `page_readers`."""
    return page_readers.extract_page(
        payload, row, entries, register=register, max_value_bytes=max_value_bytes)


# ------------------------------------------------------------------ SQL

_REGCLASS_SQL = "SELECT to_regclass(%(name)s)"
_MAPY_COUNT_SQL = "SELECT count(*) FROM mapy_affected"

# The inventory is only a lane INPUT once it is TERMINAL AND COMPLETE. `count(*) > 0` is
# the wrong gate: the inventory job is batched and resumable, so a run that stopped at
# its budget leaves a perfectly non-empty table describing a PREFIX of `listings` — and
# every listing past that prefix would then be read as "absent from the inventory", which
# is exactly the verdict that admits a carry_forward coordinate as first-party.
_INVENTORY_TERMINAL_SQL = """
    SELECT
      (SELECT count(*) FROM mapy_inventory_runs),
      (SELECT coalesce(max(restart_epoch), 0) FROM mapy_inventory_runs),
      EXISTS (
        SELECT 1 FROM mapy_inventory_runs r
        WHERE r.restart_epoch = (SELECT max(restart_epoch) FROM mapy_inventory_runs)
          AND r.status = 'completed'
          AND r.resumable),
      (SELECT string_agg(DISTINCT r.status, ',' ORDER BY r.status)
       FROM mapy_inventory_runs r
       WHERE r.restart_epoch = (SELECT max(restart_epoch) FROM mapy_inventory_runs))
"""

_RELATIONS = (
    "location_claims", "location_claim_batches", "dirty_locations",
    "portal_contracts", "portal_contract_entries", "portal_raw_payloads",
    "listing_snapshots", "mapy_affected", "mapy_inventory_runs",
)

_TIMEOUT_GUARD_SQL = """
    SELECT set_config('statement_timeout', %(statement_timeout)s, true),
           set_config('lock_timeout', %(lock_timeout)s, true)
"""

_ENTRIES_SQL = """
    SELECT pce.id, pc.source, pc.id, pc.version, pce.entry_id, pce.surface::text,
           pce.page_kind::text, pce.locator, pce.claim_type::text,
           pce.extraction_method::text, pce.subject_scope, pce.transform,
           pce.precision_map, pce.default_blur_evidence::text,
           pce.default_licence_class::text, pce.guards
    FROM portal_contract_entries pce
    JOIN portal_contracts pc ON pc.id = pce.contract_id
    WHERE pc.is_active
    ORDER BY pc.source, pce.entry_id
"""

_ACTIVE_CONTRACT_SQL = """
    SELECT id, version FROM portal_contracts WHERE source = %(source)s AND is_active
"""

_BATCH_INSERT_SQL = """
    INSERT INTO location_claim_batches
        (lane, source, extractor_version, contract_id, wave, job_run_id, outcome, note,
         scan_mode, resumable)
    VALUES (%(lane)s, %(source)s, %(extractor_version)s, %(contract_id)s, %(wave)s,
            %(job_run_id)s, 'running', %(note)s, %(scan_mode)s, %(resumable)s)
    RETURNING id
"""

# `cursor_after_ts` is NOT written any more — by either mode. It was the timestamp half of
# the deleted `(last_seen_at, id)` incremental keyset, and leaving it NULL on every row this
# lane writes is what lets `_RESUME_SQL` tell a W1-a2 snapshot cursor from a pre-W1-a2
# listing-id one (see `_resume_point`). The column stays; nothing populates it.
_BATCH_FINISH_SQL = """
    UPDATE location_claim_batches
    SET finished_at = now(), outcome = %(outcome)s, row_count = %(row_count)s,
        cursor_after_id = %(cursor_after_id)s,
        note = concat_ws(' | ', note, %(note)s::text)
    WHERE id = %(batch_id)s
"""

# THE CURSOR IS THE LANE'S ONLY MEMORY NOW. There is no watermark: `_WATERMARK_SQL`
# (`max(coverage_since) WHERE outcome='ok'`, minus `--overlap-hours`) selected on
# `listings.last_seen_at`, which the index walks move for every active listing every few
# hours — so "incremental" meant "re-mine the whole live corpus". Deleted with its time
# arm, its overlap and `coverage_since`, which nothing else reads.
#
# `outcome` is still load-bearing and narrow (migration 387): 'ok' means "the scan ran out
# of rows", never "the scan ran out of budget". It no longer gates the resume, because the
# incremental cursor must survive a completed run — a scan that reached the end of the
# snapshot log resumes from exactly where it ended, not from zero.
_RESUME_SQL = """
    SELECT outcome, cursor_after_id, cursor_after_ts
    FROM location_claim_batches
    WHERE lane = %(lane)s
      AND source IS NOT DISTINCT FROM %(source)s
      AND scan_mode = %(scan_mode)s
      AND resumable
      AND cursor_after_id IS NOT NULL
      AND outcome IN ('ok', 'stopped', 'failed')
    ORDER BY started_at DESC, id DESC
    LIMIT 1
"""

# THE CUTOVER SEED, read ONCE by a lane that has no incremental cursor of its own.
#
# Seeding at the head of the log would drop every change between the old lane's last
# position and this deploy — and the old lane's runs were being CANCELLED at the job
# timeout, so it has no `outcome='ok'` row for days and that window is hours wide. Seed at
# the old lane's own resume point instead: the newest batch row that still carries a
# `cursor_after_ts` (the pre-W1-a2 `(last_seen_at, id)` keyset's timestamp half — on
# 2026-09-11 that was 17:16Z), minus the 3-hour overlap that cursor was always read with.
# Second arm: the last `ok` watermark, same overlap. Neither: the head, and the lag below
# is then the only floor. The few hours between the anchor and now are re-walked once,
# which is cheap and lossless; the exhaustive pass is still `--mode full`.
_LEGACY_WATERMARK_SQL = """
    SELECT coalesce(
      (SELECT b.cursor_after_ts FROM location_claim_batches b
        WHERE b.lane = %(lane)s AND b.source IS NOT DISTINCT FROM %(source)s
          AND b.cursor_after_ts IS NOT NULL
        ORDER BY b.started_at DESC, b.id DESC LIMIT 1),
      (SELECT max(coalesce(b.coverage_since, b.started_at)) FROM location_claim_batches b
        WHERE b.lane = %(lane)s AND b.source IS NOT DISTINCT FROM %(source)s
          AND b.outcome = 'ok')
    ) - interval '3 hours'
"""

# THE LAG, and it is a correctness rail, not a politeness one. `listing_snapshots.id` is a
# bigserial: the id is allocated at INSERT and becomes VISIBLE at COMMIT. `write_detail_batch`
# writes N snapshots inside one multi-statement transaction, concurrently across the
# per-portal drains and the realtime worker — so a row carrying an id BELOW a cursor this
# lane has already advanced past can appear after that cursor moved, and `s.id > after_id`
# never looks back. Standing 15 minutes behind the wall clock keeps the window below every
# transaction that could still be in flight. The seed takes the same predicate, or a cold
# start would jump straight over the in-flight ids instead of stopping short of them.
_SNAPSHOT_SEED_SQL = """
    SELECT coalesce(max(id), 0) FROM listing_snapshots
    WHERE scraped_at < now() - interval '15 minutes'
      AND (%(watermark)s::timestamptz IS NULL OR scraped_at <= %(watermark)s)
"""

# THE SECOND SUBSTRATE, JOINED ONTO THE FIRST.
#
# `portal_raw_payloads.listing_id` is nullable and nothing has ever populated it
# (`scraper.db.append_payload_if_enabled` passes None), so the join is on the portal's own
# key — `(source, source_id_native)`, the store's uniqueness key, UNIQUE on `listings` too
# (`listings_source_native_uidx`, migration 091), so it stays 1:1.
#
# `page_kind = 'detail'` because that is the only kind stored: index bodies are never
# archived. Only OK bodies are mined, matching `payloads._PRUNE_SQL`'s own ranking (403
# cites idnes' 503 interstitial).
#
# "LATEST BODY" IS `last_observed_at`, NOT `first_observed_at`. The store is
# content-addressed and append-on-change, so a page that goes A -> B -> A does not append a
# third row: it collides on A's `payload_sha256` and bumps A's `last_observed_at`. Ordering
# by FIRST observation would then leave B permanently "latest" while the portal has been
# serving A for weeks, and the lane would mine a body the page no longer has. Not
# `version_seq` either — 403 added that counter with no backfill, so every older body is
# NULL there and a comparison against NULL ranks the older row as the latest.
# `prp_native (source, source_id_native, page_kind, first_observed_at desc)` still drives
# the equality lookup; the ordering is a sort over the handful of rows the version cap
# (2) allows per key, not a scan.
#
# `body_unmined` IS THE HASH GATE, computed in SQL against the portal's own ACTIVE contract
# so a per-portal version can never be applied to the wrong portal's body. NULL (a body
# nothing has mined) is DISTINCT FROM any version, so a new body is always eligible.
#
# The scan projects NO body bytes. One batch's applicable ids go to `page_readers.load_bodies`,
# which is where the R2 round trips happen — materialising ~14 GB of archive to discover
# most of it has nothing to mine is the exact cost this ordering avoids.
# NOT `*_SQL`: these two are FRAGMENTS, not statements. `tests/sql_corpus.discover` treats
# every module-level `*_SQL` constant as a statement and PREPAREs it against the replayed
# schema, where a bare select list referencing `pb` reads as "missing FROM-clause entry".
# The two composed queries below carry the `_SQL` suffix and are what the sweep checks.
_BODY_JOIN = """
    LEFT JOIN portal_contracts pc ON pc.source = l.source AND pc.is_active
    LEFT JOIN LATERAL (
        SELECT p.id, p.contract_version, p.page_kind::text AS page_kind,
               encode(p.payload_sha256, 'hex') AS payload_sha256, p.first_observed_at
        FROM portal_raw_payloads p
        WHERE p.source = l.source
          AND p.source_id_native = l.source_id_native
          AND p.page_kind = 'detail'
          AND (p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299)
        ORDER BY p.last_observed_at DESC, p.id DESC
        LIMIT 1
    ) pb ON TRUE
"""

# The snapshot cursor is a COLUMN of the scan and the LAST one: `_row_from_record` unpacks
# the record positionally, so the three selections and that unpack are one contract and a
# column appended past the end would be swallowed silently. Full mode and the bodies pass
# have no snapshot keyset and select NULL there. (W1-c deleted the class-B legacy tail that
# used to sit after it — the lane reads `raw_json` and the stored body, and a `listings`
# TEXT column is neither.)
_SELECT_COLUMNS = """
    SELECT l.id, l.source, l.source_id_native, l.raw_json, l.last_seen_at,
           ST_Y(l.geom::geometry), ST_X(l.geom::geometry),
           (a.listing_id IS NOT NULL),
           pb.id, (pb.contract_version IS DISTINCT FROM pc.version), pb.page_kind,
           pb.payload_sha256, pb.first_observed_at, pc.version,
"""

_FROM_LISTINGS = """
    FROM listings l
    LEFT JOIN mapy_affected a ON a.listing_id = l.id
"""

# Keyset over the whole table (active AND inactive: a delisted row's payload is exactly the
# evidence the history waves need, and nothing is ever deleted). Full mode is the
# contract-bump path: it re-walks every listing by id and carries no snapshot cursor.
_LISTINGS_FULL_SQL = (
    _SELECT_COLUMNS + " NULL::bigint\n"
    + _FROM_LISTINGS + _BODY_JOIN + """
    WHERE l.id > %(after_id)s
      AND (%(source)s::text IS NULL OR l.source = %(source)s)
    ORDER BY l.id
    LIMIT %(batch_size)s
""")

# THE CHANGE-DRIVEN SELECTION (W1-a2; the measurement is in the module docstring).
#
# THE WINDOW STANDS 15 MINUTES BEHIND THE CLOCK — see `_SNAPSHOT_SEED_SQL` for why: a
# bigserial id is allocated at INSERT and visible at COMMIT, so a concurrent
# `write_detail_batch` transaction can land a row BELOW a cursor that has already moved, and
# a keyset never looks back. The lag costs one run of latency and closes the hole.
#
# The WINDOW is a keyset slice of the snapshot LOG, not of `listings`: `s.id > cursor ORDER
# BY s.id LIMIT n` is one walk of the primary key. It is deduped to one row per listing
# afterwards, so a listing that changed five times in the window is extracted once — the
# readers read `listings.raw_json`, the CURRENT payload, so re-reading it per snapshot
# would produce five identical fingerprints.
#
# THE SOURCE FILTER LIVES INSIDE THE WINDOW, not outside it. Outside, a source-scoped run
# whose window held no row for that portal would return zero listings — indistinguishable
# from "the log is exhausted" — and the run would stamp `ok` with its cursor stuck. Inside,
# every window row belongs to a listing the scan returns, so `max(snapshot_cursor)` over
# the returned rows IS the window's own high-water mark.
_LISTINGS_INCREMENTAL_SQL = ("""
    WITH win AS (
        SELECT s.id, s.listing_id
        FROM listing_snapshots s
        JOIN listings f ON f.id = s.listing_id
         AND (%(source)s::text IS NULL OR f.source = %(source)s)
        WHERE s.id > %(after_id)s
          AND s.scraped_at < now() - interval '15 minutes'
        ORDER BY s.id
        LIMIT %(batch_size)s
    ), changed AS (
        SELECT listing_id, max(id) AS snapshot_cursor FROM win GROUP BY listing_id
    )
"""
    + _SELECT_COLUMNS + " c.snapshot_cursor\n" + """
    FROM changed c
    JOIN listings l ON l.id = c.listing_id
    LEFT JOIN mapy_affected a ON a.listing_id = l.id
"""
    + _BODY_JOIN + """
    ORDER BY l.id
""")

# THE BODIES-FIRST BACKLOG (W1-a2), the page half's own pass.
#
# The listing scan mines the body of a listing it happens to visit; that is change-shaped,
# and the page backlog is not. ~250 000 latest detail bodies of active listings sat unmined
# after the first wave (`contract_version IS NULL` everywhere), and at 1 500 per 20 000-row
# listing batch they would have drained over ~170 runs of pure side effect.
#
# DRIVEN FROM `portal_raw_payloads`, WITH AN IN-RUN KEYSET ON `p.id`. The first cut drove
# off `listings` with no cursor at all, on the theory that the mined-at stamp is the
# progress — and it is, for a body that CAN be stamped. Four paths leave one unstamped (the
# bucket could not serve it, the portal has no scope register, `extract_page` refused on its
# content, the scoper failed closed) and three of those are deterministic per body, so the
# unstampable ones sit at the head of `ORDER BY p.id` forever: re-fetched every batch, and
# once `cap` of them accumulate a batch stamps nothing at all, the no-progress rail ends the
# pass, and the next run selects the identical rows. The whole backlog stalls behind a
# handful of bad objects, silently. The keyset walks PAST them instead: `after_body_id`
# starts at 0 each run and advances to the batch's `max(p.id)` after its transaction closes,
# so poison costs one re-fetch per RUN, not one per batch, and the pass ends when a batch
# comes back short of `cap` (the end of the keyset) rather than when it stamps nothing.
#
# The `p.id = (SELECT ... ORDER BY last_observed_at DESC, p2.id DESC LIMIT 1)` self-probe is
# "the LATEST detail body of this key", the same definition `_BODY_JOIN`'s lateral applies
# from the other direction — `last_observed_at`, never `first_observed_at` (a page that goes
# A -> B -> A appends no third row, it bumps A) and never `version_seq` (403 added it with no
# backfill, so every older body is NULL there).
#
# ACTIVE listings only, unlike the listing scan. A delisted row's PAYLOAD is evidence we
# already hold; its stored page body is a fetch we would pay R2 for to mine a page nobody
# will ever see again, ahead of ~250 000 live ones.

# The SAME record shape as the listing scan — ONE `_row_from_record` for all three
# selections — projected off the payload row itself rather than through a lateral, and
# without `raw_json`: no page reader reads it (the substrate is the body), and 1 500 rows of
# it is ~10 MB dragged over the wire to be thrown away. A `pb.` left in the result would be
# a column this FROM clause does not have, so the test asserts none survives.
_UNMINED_BODIES_SELECT = (
    _SELECT_COLUMNS
    .replace("l.raw_json", "NULL::jsonb")
    .replace("pb.payload_sha256", "encode(p.payload_sha256, 'hex')")
    .replace("pb.page_kind", "p.page_kind::text")
    .replace("pb.id", "p.id")
    .replace("pb.contract_version", "p.contract_version")
    .replace("pb.first_observed_at", "p.first_observed_at")
    # No snapshot keyset here: this pass walks payload ids.
    + " NULL::bigint\n"
)

_UNMINED_BODIES_FROM = """
    FROM portal_raw_payloads p
    JOIN listings l ON l.source = p.source AND l.source_id_native = p.source_id_native
    LEFT JOIN mapy_affected a ON a.listing_id = l.id
    JOIN portal_contracts pc ON pc.source = l.source AND pc.is_active
"""

_UNMINED_BODIES_WHERE = """
    WHERE p.id > %(after_body_id)s
      AND p.page_kind = 'detail'
      AND (p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299)
      AND p.contract_version IS DISTINCT FROM pc.version
      AND l.is_active
      AND l.source = ANY(%(page_sources)s::text[])
      AND (%(source)s::text IS NULL OR l.source = %(source)s)
      AND p.id = (
        SELECT p2.id FROM portal_raw_payloads p2
        WHERE p2.source = p.source
          AND p2.source_id_native = p.source_id_native
          AND p2.page_kind = 'detail'
          AND (p2.http_status IS NULL OR p2.http_status BETWEEN 200 AND 299)
        ORDER BY p2.last_observed_at DESC, p2.id DESC
        LIMIT 1)
"""

_UNMINED_BODIES_SQL = (
    _UNMINED_BODIES_SELECT + _UNMINED_BODIES_FROM + _UNMINED_BODIES_WHERE + """
    ORDER BY p.id
    LIMIT %(cap)s
""")

# One count per RUN (never per batch), so the summary can say how much of the backlog is
# left rather than only how much this run took off it. Same predicate, read from id 0.
_UNMINED_BODY_BACKLOG_SQL = (
    "SELECT count(*)" + _UNMINED_BODIES_FROM + _UNMINED_BODIES_WHERE)


# The mined-at stamp, in the SAME transaction as the claims it produced: a batch that rolls
# back un-stamps its bodies too, so the next run mines them again rather than skipping
# claims that were never written.
_STAMP_MINED_SQL = """
    UPDATE portal_raw_payloads p
    SET contract_version = v.version
    FROM jsonb_to_recordset(%(rows)s::jsonb) AS v(id bigint, version integer)
    WHERE p.id = v.id
"""

# One statement, so the claim insert and the dirty_locations enqueue are atomic together
# (03 §3.2: the enqueue happens INSIDE the claim-insert transaction; it is the only
# coupling between intake and resolution).
#
# claim_fingerprint is computed in SQL, deliberately, and by a NAMED FUNCTION rather than
# an expression pasted here, because `value_norm` is written by `location_value_norm()`
# (migration 382) = `lower(unaccent(...))`, and PostgreSQL's `unaccent` dictionary is NOT
# Python's NFKD combining-mark strip (it additionally expands ß→ss, ø→o, đ→d, ł→l …). A
# Python mirror would drift on exactly the foreign-address cohort this program exists to
# detect, and a drifted fingerprint does not conflict — it inserts.
#
# The tuple is 01 §4.2.1's, in its order, and is TIME-FREE.
#
# IT IS ALSO WIDER THAN THE TABLE (W1-b, migration 498). Nine of its inputs — page_kind,
# extractor_id, extractor_version, value_norm, distance_m, travel_mode, target_text,
# declared_confidence, legacy_source_column — are no longer STORED, but the readers still
# compute them and they still enter the hash. That is what keeps 5 M existing fingerprints
# valid: narrowing the tuple would re-dialect every one of them, and a re-dialected
# fingerprint does not conflict, it inserts.
_CLAIM_FINGERPRINT_SQL = """
    location_claim_fingerprint(
        t.listing_id, t.source, t.source_id_native,
        t.claim_type, t.surface, t.page_kind, t.extraction_method,
        t.extractor_id, t.extractor_version, t.contract_entry_id,
        t.value_norm, t.value_text,
        t.value_num, t.geom, t.shape,
        t.value_jsonb, t.distance_m, t.travel_mode, t.target_text,
        t.declared_precision_label, t.declared_confidence, t.declared_radius_m,
        t.legacy_source_column)
"""

_CLAIM_WRITE_SQL = f"""
    WITH input AS (
        SELECT * FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
            listing_id bigint, source text, source_id_native text,
            snapshot_id bigint, snapshot_anchor text, first_observed_at timestamptz,
            claim_type text, surface text, page_kind text, extraction_method text,
            extractor_id text, extractor_version text, contract_entry_id bigint,
            value_text text, value_num numeric, value_geom_wkt text, value_shape_wkt text,
            value_jsonb jsonb, distance_m integer, travel_mode text, target_text text,
            declared_precision_label text, declared_confidence text,
            declared_radius_m numeric, claim_confidence text,
            blur_evidence text, licence_class text,
            legacy_source_column text, legacy_write_path_unknown boolean,
            history_completeness text, subject_scoped boolean,
            payload_id bigint, payload_sha256 text, evidence_quote text,
            span_start integer, span_end integer, payload_scope_version text,
            model text, prompt_version text)
    ), typed AS (
        SELECT i.*,
               location_value_norm(i.value_text) AS value_norm,
               CASE WHEN i.value_geom_wkt IS NULL THEN NULL
                    ELSE ST_GeomFromText(i.value_geom_wkt, 4326) END AS geom,
               CASE WHEN i.value_shape_wkt IS NULL THEN NULL
                    ELSE ST_GeomFromText(i.value_shape_wkt, 4326) END AS shape
        FROM input i
    ), fingerprinted AS (
        SELECT t.*, {_CLAIM_FINGERPRINT_SQL} AS claim_fingerprint FROM typed t
    ), deduped AS (
        SELECT DISTINCT ON (claim_fingerprint) * FROM fingerprinted ORDER BY claim_fingerprint
    ), ins AS (
        INSERT INTO location_claims (
            listing_id, source, first_observed_at, claim_type, surface,
            extraction_method, contract_entry_id, value_text, value_num, value_geom,
            value_jsonb, declared_precision_label, declared_radius_m, claim_confidence,
            blur_evidence, licence_class, subject_scoped, claim_fingerprint)
        SELECT d.listing_id, d.source, d.first_observed_at,
               d.claim_type::location_claim_type,
               d.surface::location_claim_surface,
               d.extraction_method::location_extraction_method,
               d.contract_entry_id, d.value_text, d.value_num, d.geom, d.value_jsonb,
               d.declared_precision_label, d.declared_radius_m,
               d.claim_confidence::match_confidence,
               d.blur_evidence::blur_evidence, d.licence_class::licence_class,
               d.subject_scoped, d.claim_fingerprint
        FROM deduped d
        ON CONFLICT (claim_fingerprint) DO NOTHING
        RETURNING id, listing_id
    ), enqueued AS (
        INSERT INTO dirty_locations (listing_id, reason)
        SELECT DISTINCT listing_id, 'claim_insert' FROM ins
        ON CONFLICT (listing_id) DO NOTHING
        RETURNING listing_id
    )
    SELECT (SELECT count(*) FROM ins), (SELECT count(*) FROM enqueued)
"""


# ------------------------------------------------------------------ db plumbing

@contextmanager
def guarded(
    conn: psycopg.Connection, statement_timeout_s: int, lock_timeout_s: int = 5,
) -> Iterator[psycopg.Cursor]:
    """One transaction with transaction-LOCAL timeouts. `db.connect()` is autocommit and
    points at the transaction-mode pooler, where a session-level SET can land on a
    different backend than the statement it was meant to guard."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(_TIMEOUT_GUARD_SQL, {
                "statement_timeout": f"{statement_timeout_s}s",
                "lock_timeout": f"{lock_timeout_s}s",
            })
            yield cur


def missing_relations(conn: psycopg.Connection) -> list[str]:
    missing: list[str] = []
    with conn.cursor() as cur:
        for name in _RELATIONS:
            cur.execute(_REGCLASS_SQL, {"name": name})
            if cur.fetchone()[0] is None:
                missing.append(name)
    return missing


def assert_inventory_ready(conn: psycopg.Connection) -> int:
    """06 §6.1.2: the C7.2 R2 inventory is a lane INPUT. Without it, `carry_forward` cannot
    be classified and the licence gate cannot be met, so the lane refuses to run.

    "Without it" means TERMINAL AND COMPLETE, not merely non-empty: absence from a PREFIX
    is indistinguishable from absence from the inventory, and that is exactly the verdict
    that admits a Mapy-derived `carry_forward` coordinate as first-party."""
    with conn.cursor() as cur:
        cur.execute(_REGCLASS_SQL, {"name": "mapy_affected"})
        if cur.fetchone()[0] is None:
            raise IntakeRefused(
                "mapy_affected does not exist: migration 385 is not applied. The Mapy "
                "affected-set inventory is a lane INPUT (06 §6.1.2) — run "
                "`python -m scripts.location_mapy_inventory` first.")
        cur.execute(_MAPY_COUNT_SQL)
        count = int(cur.fetchone()[0])
        cur.execute(_INVENTORY_TERMINAL_SQL)
        run_count, epoch, complete, statuses = cur.fetchone()
    if count == 0:
        raise IntakeRefused(
            "mapy_affected is empty: the Mapy affected-set inventory has not been "
            "materialised. Every carry_forward coordinate would be admitted as "
            "first-party and the licence gate would fail (06 §6.1.2). Run "
            "`python -m scripts.location_mapy_inventory` to completion first.")
    if not int(run_count or 0):
        raise IntakeRefused(
            "mapy_inventory_runs has no rows: mapy_affected holds data no run "
            "accounted for, so its completeness cannot be established. The inventory "
            "is a lane INPUT (06 §6.1.2) — run "
            "`python -m scripts.location_mapy_inventory` to completion first.")
    if not complete:
        raise IntakeRefused(
            f"the Mapy affected-set inventory is INCOMPLETE: restart epoch {int(epoch)} "
            f"has no resumable run with status='completed' (saw: {statuses or 'none'}). "
            f"A partial inventory is worse than none — every listing past the scan's "
            f"high-water mark reads as ABSENT from it, which is exactly the verdict that "
            f"admits a Mapy-derived carry_forward coordinate as first-party (06 §6.1.2). "
            f"Run `python -m scripts.location_mapy_inventory` to completion first.")
    return count


def load_entries(conn: psycopg.Connection) -> dict[str, list[Entry]]:
    by_source: dict[str, list[Entry]] = {}
    with conn.cursor() as cur:
        cur.execute(_ENTRIES_SQL)
        for row in cur.fetchall():
            entry = Entry(
                id=int(row[0]), source=row[1], contract_id=int(row[2]),
                contract_version=int(row[3]), entry_id=row[4], surface=row[5],
                page_kind=row[6], locator=row[7] or {}, claim_type=row[8],
                extraction_method=row[9], subject_scope=row[10] or {},
                transform=tuple(row[11] or ()), precision_map=row[12] or {},
                default_blur_evidence=row[13], default_licence_class=row[14],
                guards=tuple(row[15] or ()))
            by_source.setdefault(entry.source, []).append(entry)
    return by_source


@dataclass(frozen=True, slots=True)
class ScanRow:
    """One row of either selection: the listing, its latest stored detail body, and where
    the snapshot keyset had reached when this row was selected (None in full mode and in
    the bodies pass, neither of which walks the snapshot log)."""
    row: ListingRow
    body: page_readers.ArchivedPayload | None
    body_unmined: bool
    contract_version: int | None
    snapshot_cursor: int | None


def _row_from_record(record: tuple[Any, ...]) -> ScanRow:
    """One scan row -> the listing, its latest stored detail body or None, and the cursor.

    A FIXED-WIDTH unpack: W1-c deleted the class-B legacy-column tail, so the three
    selections project exactly the columns this function names and a drift between them is a
    TypeError here on the first row rather than every value shifted one position to the
    left. The snapshot cursor is the last column for the same reason it used to sit before
    the tail — a column appended after it would be swallowed silently.
    """
    (listing_id, source, native, raw_json, last_seen_at, lat, lon, in_inventory,
     body_id, body_unmined, body_page_kind, body_sha, body_first_observed,
     contract_version, snapshot_cursor) = record
    row = ListingRow(
        listing_id=int(listing_id),
        source=source,
        source_id_native=str(native) if native is not None else str(listing_id),
        raw_json=raw_json if isinstance(raw_json, dict) else {},
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
        # 06 §6.6 rule 1: a claim mined from `listings.raw_json` keeps the payload's own
        # observation time — the listing's last sighting — never the migration date.
        observed_at=last_seen_at,
        in_mapy_inventory=bool(in_inventory))
    body: page_readers.ArchivedPayload | None = None
    if body_id is not None:
        body = page_readers.ArchivedPayload(
            id=int(body_id), source=source,
            source_id_native=row.source_id_native,
            page_kind=body_page_kind,
            payload_sha256=str(body_sha),
            # 06 §6.6 Rule 1 + Rule 2: the BODY's own first observation, never now() and
            # never `last_observed_at` (which an unchanged refetch moves).
            first_observed_at=body_first_observed)
    return ScanRow(
        row=row, body=body, body_unmined=bool(body_unmined),
        contract_version=int(contract_version) if contract_version is not None else None,
        snapshot_cursor=int(snapshot_cursor) if snapshot_cursor is not None else None)


def chunk_rows(
    rows: list[dict[str, Any]], *, max_rows: int, max_bytes: int,
) -> Iterator[list[dict[str, Any]]]:
    """Split one jsonb array into statement-sized arrays, WITHOUT splitting a listing.

    Two bounds, whichever trips first — a row count (cheap, predictable) and a cumulative
    serialized-byte budget (the one that actually matters, because a batch's row count says
    nothing about its bytes).

    The listing boundary is what makes chunking SEMANTICS-PRESERVING, not merely smaller.
    `claim_fingerprint`'s tuple (01 §4.2.1) begins with `listing_id, source,
    source_id_native`, so two fingerprint-equal claims are necessarily the same listing's.
    Keeping a listing's rows in one array therefore keeps every fingerprint-equal set inside
    ONE statement, where `DISTINCT ON (claim_fingerprint)` still arbitrates it.

    A group that exceeds `max_bytes` on its own is still emitted (a budget cannot split an
    array element) — that case is what `_refuse_oversized` exists to keep out of reach.
    """
    chunk: list[dict[str, Any]] = []
    size = 0
    for index, row in enumerate(rows):
        row_bytes = len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
        starts_group = index == 0 or row["listing_id"] != rows[index - 1]["listing_id"]
        if chunk and starts_group and (len(chunk) >= max_rows or size + row_bytes > max_bytes):
            yield chunk
            chunk, size = [], 0
        chunk.append(row)
        size += row_bytes
    if chunk:
        yield chunk


def write_result(cur: psycopg.Cursor, result: IntakeResult) -> tuple[int, int]:
    """Write one scan batch's claims. Returns (inserted, enqueued).

    The caller's transaction is unchanged — every chunk is flushed inside it, so the batch
    is still all-or-nothing and a failure still rolls the whole batch back. Claims are the
    only rows this lane writes; a refusal is a counter, not a row.
    """
    max_rows = env_positive_int(WRITE_CHUNK_ROWS_ENV, DEFAULT_WRITE_CHUNK_ROWS)
    max_bytes = env_positive_int(WRITE_CHUNK_BYTES_ENV, DEFAULT_WRITE_CHUNK_BYTES)
    inserted = enqueued = 0
    claim_rows = [c.to_row() for c in result.claims]
    claim_rows.sort(key=lambda r: r["listing_id"])
    for chunk in chunk_rows(claim_rows, max_rows=max_rows, max_bytes=max_bytes):
        cur.execute(_CLAIM_WRITE_SQL, {"rows": Jsonb(chunk)})
        chunk_inserted, chunk_enqueued = (int(x) for x in cur.fetchone())
        inserted += chunk_inserted
        enqueued += chunk_enqueued
    return inserted, enqueued


def stamp_mined_bodies(cur: psycopg.Cursor, stamps: list[dict[str, Any]]) -> None:
    """Mark the bodies this batch mined as mined AT the contract version that mined them.

    Inside the batch transaction on purpose: a rolled-back batch un-stamps its bodies, so
    the next run mines them again rather than skipping claims that were never written.
    """
    if stamps:
        cur.execute(_STAMP_MINED_SQL, {"rows": Jsonb(stamps)})


def _resume_point(
    conn: psycopg.Connection, *, mode: str, source: str | None,
) -> int | None:
    """Where this scan picks up, or None to start at the beginning of its range.

    THE TWO MODES ANSWER DIFFERENTLY, because their cursors claim different things.

    FULL keeps migration 387's rule: only a budget-'stopped' predecessor is resumed from.
    'ok' there means the whole table was walked, and the next full pass is the contract-bump
    re-walk — it must start at id 0.

    INCREMENTAL resumes from ANY terminal outcome, because its cursor is a POSITION IN AN
    APPEND-ONLY LOG, not a claim about coverage. A scan that reached the end of
    `listing_snapshots` has to carry on from that end (restarting at 0 would re-walk the
    whole log), and a 'failed' one is safe to resume because the cursor only ever advances
    past a batch whose transaction closed.

    THE EPOCH GUARD IS `cursor_after_ts IS NULL`. Before W1-a2 an incremental cursor was a
    `(last_seen_at, id)` keyset and always carried a timestamp; it is a bare
    `listing_snapshots.id` now and this lane writes no timestamp cursor at all. Reading an
    old LISTING id back as a SNAPSHOT id would silently skip every snapshot below it, so a
    row that still carries a timestamp is not ours to resume from.
    """
    with conn.cursor() as cur:
        cur.execute(_RESUME_SQL, {"lane": LANE, "source": source, "scan_mode": mode})
        row = cur.fetchone()
    if not row:
        return None
    outcome, after_id, after_ts = row
    if after_id is None:
        return None
    if mode == "full":
        return int(after_id) if outcome == "stopped" else None
    return None if after_ts is not None else int(after_id)


def _snapshot_seed(
    conn: psycopg.Connection, statement_timeout: int, *, source: str | None,
) -> tuple[int, Any]:
    """The cutover cursor and the anchor it was derived from. See `_SNAPSHOT_SEED_SQL`.

    Two statements, two guarded blocks: they are separate reads, and each one is bounded on
    its own so neither can hang the run before its first batch row exists."""
    with guarded(conn, statement_timeout) as cur:
        cur.execute(_LEGACY_WATERMARK_SQL, {"lane": LANE, "source": source})
        row = cur.fetchone()
        watermark = row[0] if row else None
    with guarded(conn, statement_timeout) as cur:
        cur.execute(_SNAPSHOT_SEED_SQL, {"watermark": watermark})
        row = cur.fetchone()
    return (int(row[0]) if row and row[0] is not None else 0), watermark


@dataclass
class _Budget:
    """The run's wall clock, shared by both halves.

    `room_for` is the whole point: a batch must not START unless it is expected to FINISH
    inside the budget, and the only honest estimate of the next batch's duration is the
    last one's. The alternative is what production did — check the clock between batches,
    start an 18-minute batch with 3 minutes left, overrun `timeout-minutes: 55`, get
    CANCELLED, and stamp nothing resumable at all.
    """
    max_seconds: float | None
    # `lambda:`, not the bound `time.monotonic`: a default_factory is resolved when the
    # CLASS is defined, so it would freeze one clock here and read another in `spent()`.
    started: float = field(default_factory=lambda: time.monotonic())

    def spent(self) -> float:
        return time.monotonic() - self.started

    def left(self, share: float = 1.0) -> float | None:
        if self.max_seconds is None:
            return None
        return self.max_seconds * share - self.spent()

    def room_for(self, estimate: float, share: float = 1.0) -> bool:
        left = self.left(share)
        return left is None or left > estimate


# ------------------------------------------------------------------ the run


def unknown_readers(
    entries_by_source: dict[str, list[Entry]], wanted: list[str],
) -> list[str]:
    """`entry_id:reader` for every entry naming a reader THE registry does not implement.

    One registry, so one question. While there were three lanes this had to know about two
    name-only mirrors, and leaving them out of the preflight took the hourly intake down
    for all nine portals on 2026-09-06 (the W2-6..W2-12 activation): the runtime loop
    skipped the other lanes' readers correctly and never ran, because the preflight refused
    first. There is nothing left to mirror.
    """
    return sorted(
        f"{e.entry_id}:{e.reader}"
        for s in wanted for e in entries_by_source.get(s, ())
        if e.reader and e.reader not in READERS)


def _open_body_store(page_capable: bool) -> page_readers.BodyStore | None:
    """The R2 client, or None with ONE warning. Never a refusal.

    The page half is the half that needs a bucket; the payload half is the hourly ingest
    for all nine portals. A rotated credential must cost us the first, never the second.
    """
    try:
        store = payloads.open_store()
    except Exception as exc:  # noqa: BLE001 - a bad env must not take the payload half down
        LOG.warning("INTAKE could not open the R2 body store (%s); the page-body half of "
                    "this run is SKIPPED and the payload half runs unchanged", exc)
        return None
    if store is None and page_capable:
        LOG.warning("INTAKE R2 is not configured (R2_* env vars unset); the page-body half "
                    "of this run is SKIPPED and the payload half runs unchanged")
    return store


BodyCandidate = tuple[ListingRow, page_readers.ArchivedPayload, int]


def _body_candidate(
    scan: ScanRow, entries: list[Entry], *, store: page_readers.BodyStore | None,
) -> BodyCandidate | None:
    """Is this row's stored body worth a round trip? ONE predicate, both halves.

    The bodies pass asks the same question in SQL and would get the same answer; asking it
    here too costs nothing and keeps `page_entries` (the body's page KIND must be one the
    contract declares entries for) from being a rule only one half applies."""
    if (scan.body is None or not scan.body_unmined or scan.contract_version is None
            or store is None
            or not page_readers.page_entries(entries, scan.body.page_kind)):
        return None
    return (scan.row, scan.body, scan.contract_version)


def mine_bodies(
    cur: psycopg.Cursor, candidates: list[BodyCandidate], *,
    entries_by_source: dict[str, list[Entry]],
    registers: dict[str, page_readers.ScopeRegister],
    store: page_readers.BodyStore | None, result: IntakeResult,
    stats: dict[str, Any], max_value_bytes: int,
) -> list[dict[str, Any]]:
    """Fetch, extract and account for one set of stored bodies; returns the stamps to write.

    ONE implementation for both halves — the bodies-first drain and the listing scan's
    opportunistic mining are the same operation over a different selection.

    THE THREE WAYS ONE LISTING'S PAGE CAN FAIL each cost that listing's page entries and
    nothing else: a body the bucket could not serve, an `IntakeRefused` triggered by the
    body's CONTENT, and a scoper that failed closed. All three leave the body UNSTAMPED so
    the next run asks again. Letting any of them out would take down the payload claims
    computed in the same transaction and hand the next run the same body to die on.
    """
    if not candidates:
        return []
    fetch_started = time.monotonic()
    bodies, from_r2 = page_readers.load_bodies(
        cur, [body.id for _, body, _ in candidates], store=store)
    stats["body_fetch_seconds"] += time.monotonic() - fetch_started
    stats["bodies_fetched"] += len(bodies)
    stats["bodies_from_r2"] += from_r2
    stamps: list[dict[str, Any]] = []
    for row, body, version in candidates:
        raw = bodies.get(body.id)
        if raw is None:
            continue
        register = registers.get(row.source)
        if register is None:
            continue
        try:
            page_result = page_readers.extract_page(
                replace(body, body=raw), row, entries_by_source[row.source],
                register=register, max_value_bytes=max_value_bytes)
        except IntakeRefused as refused:
            LOG.warning("PAGE extract refused listing_id=%d source=%s payload_id=%d: %s",
                        row.listing_id, row.source, body.id, refused)
            result.refuse(f"page_extract_refused:{row.source}")
            continue
        result.extend(page_result)
        if "scope_incomplete" in page_result.refusals:
            # A stamp would record the body as mined AT this contract version and hide the
            # miss until the next bump.
            continue
        stamps.append({"id": body.id, "version": version})
    stats["bodies_mined"] += len(stamps)
    return stamps


def _unmined_body_backlog(
    conn: psycopg.Connection, *, source: str | None, page_sources: set[str],
    statement_timeout: int,
) -> int | None:
    """How many latest bodies are still unmined, for the run summary. Once per RUN.

    Best-effort by design: this is a readout, and a run that mined 1 500 bodies has already
    earned its outcome — failing it over a slow `count(*)` would be the reporting tail
    wagging the lane."""
    sources = sorted(page_sources if source is None else page_sources & {source})
    if not sources:
        return None
    try:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_UNMINED_BODY_BACKLOG_SQL,
                        {"source": source, "page_sources": sources,
                         "after_body_id": 0})
            row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception as exc:  # noqa: BLE001 - a readout must not fail a finished run
        LOG.warning("INTAKE could not count the unmined-body backlog (%s)", exc)
        return None


def drain_unmined_bodies(
    conn: psycopg.Connection, *, source: str | None, page_sources: set[str],
    entries_by_source: dict[str, list[Entry]],
    registers: dict[str, page_readers.ScopeRegister],
    store: page_readers.BodyStore | None, cap: int, statement_timeout: int,
    budget: _Budget, batch_id: int | None, dry_run: bool, max_value_bytes: int,
    stats: dict[str, Any], refusals: dict[str, int],
) -> None:
    """The page half's OWN pass, ahead of the listing scan (W1-a2).

    Bodies arrive on the portals' cadence, not on the listings' — and after the first wave
    ~250 000 latest bodies of active listings were unmined at once. Riding them on the
    listing scan capped the drain at `DEFAULT_BODY_FETCH_CAP` per 20 000-row batch: ~170
    runs of walking listings to reach bodies the query could have named directly.

    THE KEYSET IS IN-RUN and it is what keeps an unstampable body from stalling the whole
    backlog (see `_UNMINED_BODIES_SQL`): `after_body_id` starts at 0 every run, so the
    contract-version gate still decides WHAT is eligible, and advances past every row a
    batch selected — stamped or not — so the pass walks on. The other two rails: half the
    run budget at most (the payload half is never starved), and a batch that comes back
    short of `cap` is the end of the keyset.
    """
    if store is None or not page_sources:
        return
    started = time.monotonic()
    last_seconds = 0.0
    after_body_id = 0
    sources = sorted(page_sources if source is None else page_sources & {source})
    if not sources:
        return
    while True:
        if not budget.room_for(last_seconds, BODIES_BUDGET_SHARE):
            LOG.info("INTAKE bodies-first stopping: %.0fs of the half budget left, the "
                     "last batch took %.0fs", budget.left(BODIES_BUDGET_SHARE) or 0.0,
                     last_seconds)
            break
        batch_started = time.monotonic()
        result = IntakeResult()
        stamps: list[dict[str, Any]] = []
        selected = 0
        batch_cursor = after_body_id
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_UNMINED_BODIES_SQL, {
                "source": source, "page_sources": sources, "cap": cap,
                "after_body_id": after_body_id})
            records = cur.fetchall()
            selected = len(records)
            if not records:
                stats["bodies_pass_complete"] = True
                LOG.info("INTAKE bodies-first: no unmined bodies left above id=%d",
                         after_body_id)
                break
            candidates: list[BodyCandidate] = []
            for record in records:
                scan = _row_from_record(record)
                # The keyset advances past EVERY selected row, before any filter: a body
                # this batch cannot mine must cost one re-fetch per run, not one per batch.
                if scan.body is not None:
                    batch_cursor = max(batch_cursor, scan.body.id)
                entries = entries_by_source.get(scan.row.source)
                if not entries:
                    continue
                candidate = _body_candidate(scan, entries, store=store)
                if candidate is not None:
                    candidates.append(candidate)
            stats["bodies_eligible"] += len(candidates)
            stamps = mine_bodies(
                cur, candidates, entries_by_source=entries_by_source,
                registers=registers, store=store, result=result, stats=stats,
                max_value_bytes=max_value_bytes)
            stats["claims"] += len(result.claims)
            stats["claims_page"] += len(result.claims)
            for reason, count in result.refusals.items():
                refusals[reason] = refusals.get(reason, 0) + count
                stats["refusals"] += count
            if not dry_run and batch_id is not None:
                inserted, enqueued = write_result(cur, result)
                stamp_mined_bodies(cur, stamps)
                stats["claims_inserted"] += inserted
                stats["enqueued"] += enqueued
        # Advanced only here, after the transaction closed — the same rule the payload
        # half's cursor follows.
        after_body_id = batch_cursor
        last_seconds = time.monotonic() - batch_started
        stats["bodies_batches"] += 1
        LOG.info("INTAKE bodies-first batch selected=%d mined=%d claims=%d inserted=%d "
                 "through_id=%d in %.1fs", selected, len(stamps), len(result.claims),
                 stats["claims_inserted"], after_body_id, last_seconds)
        if dry_run:
            # A dry run is a shape check, not a drain: it writes no claims, so spending the
            # bucket on the rest of the backlog would buy nothing.
            LOG.info("INTAKE bodies-first: --dry-run takes one batch, not the backlog")
            break
        if selected < cap:
            stats["bodies_pass_complete"] = True
            LOG.info("INTAKE bodies-first: the keyset reached its end at id=%d",
                     after_body_id)
            break
    stats["bodies_seconds"] = time.monotonic() - started


def run(
    conn: psycopg.Connection,
    *,
    mode: str,
    source: str | None,
    batch_size: int,
    max_seconds: float | None,
    limit: int | None,
    start_after_id: int,
    statement_timeout: int,
    dry_run: bool,
    note: str | None,
    store: page_readers.BodyStore | None = None,
) -> dict[str, Any]:
    missing = missing_relations(conn)
    if missing:
        raise IntakeRefused(
            f"location schema not applied; missing {', '.join(missing)} "
            f"(migrations 380-387, 403)")
    inventory_rows = assert_inventory_ready(conn)

    entries_by_source = load_entries(conn)
    wanted = [source] if source else list(SOURCES)
    unloaded = [s for s in wanted if not entries_by_source.get(s)]
    if unloaded:
        raise IntakeRefused(
            f"no ACTIVE portal contract for {', '.join(unloaded)}: git is the store of "
            f"record and the DB tables are its projection — run "
            f"`python -m location_data.contracts --load` (02 §2.1.8)")

    # Fail fast, before a batch row exists: an unknown reader on one entry would otherwise
    # abort mid-run and leave the batch `failed` for a config problem.
    unknown = unknown_readers(entries_by_source, wanted)
    if unknown:
        raise IntakeRefused(
            f"active contract declares readers no lane implements: "
            f"{', '.join(unknown)}")

    # `page_entries(..., 'detail')`, not merely "names a page reader": `detail` is the only
    # page kind the archive stores, so a portal whose page entries are declared for another
    # kind has no body this lane can mine. Without the distinction the bodies-first pass
    # would select that portal's rows every batch, stamp none of them, and stop on its own
    # no-progress rail with the rest of the backlog untouched.
    page_capable = {
        s for s in wanted
        if page_readers.page_entries(entries_by_source.get(s, []), "detail")
    }
    registers = page_readers.load_registers(conn) if page_capable else {}
    if store is None:
        store = _open_body_store(bool(page_capable))
    body_cap = env_positive_int(BODY_FETCH_CAP_ENV, DEFAULT_BODY_FETCH_CAP)

    # The preflight reads are bounded too. They are small by construction, which is exactly
    # why an unbounded one is dangerous: under the IO pressure of a concurrent registry load
    # a "small" read still waits for its pages, and a run that hangs before its first batch
    # row exists leaves nothing at all to diagnose from.
    contract_id: int | None = None
    if source:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_ACTIVE_CONTRACT_SQL, {"source": source})
            row = cur.fetchone()
            contract_id = int(row[0]) if row else None

    # An operator-anchored run does not certify that everything below its anchor was
    # scanned, so it neither resumes from a stored cursor nor becomes one (the same guard
    # migration 385 puts on `mapy_inventory_runs.resumable`). `--start-after-id` means a
    # `listings.id` in full mode and a `listing_snapshots.id` in incremental mode — the
    # keyset each one walks.
    anchored = start_after_id > 0
    after_id = start_after_id
    if not anchored:
        resumed = _resume_point(conn, mode=mode, source=source)
        if resumed is not None:
            after_id = resumed
            LOG.info("INTAKE resuming the %s scan for source=%s from after_id=%d",
                     mode, source or "*", after_id)
        elif mode == "incremental":
            after_id, anchor = _snapshot_seed(conn, statement_timeout, source=source)
            LOG.info("INTAKE no incremental cursor for source=%s; seeding at snapshot "
                     "id=%d from the pre-W1-a2 anchor %s (the exhaustive pass is "
                     "--mode full)", source or "*", after_id, anchor or "none (log head)")

    batch_id: int | None = None
    if not dry_run:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_BATCH_INSERT_SQL, {
                "lane": LANE, "source": source, "extractor_version": INTAKE_VERSION,
                "contract_id": contract_id, "wave": WAVE,
                "job_run_id": os.environ.get("GITHUB_RUN_ID"), "note": note,
                "scan_mode": mode, "resumable": not anchored,
            })
            batch_id = int(cur.fetchone()[0])
    LOG.info("INTAKE start mode=%s source=%s batch=%d inventory_rows=%d batch_id=%s "
             "page_sources=%s store=%s budget=%s",
             mode, source or "*", batch_size, inventory_rows, batch_id,
             ",".join(sorted(page_capable)) or "-", "yes" if store else "no",
             f"{max_seconds:.0f}s" if max_seconds is not None else "none")

    budget = _Budget(max_seconds)
    # Resolved once, not per listing: the extractor is called 20 000 times a batch.
    max_value_bytes = env_positive_int(MAX_CLAIM_VALUE_BYTES_ENV,
                                       DEFAULT_MAX_CLAIM_VALUE_BYTES)
    stats: dict[str, Any] = {
        "listings": 0, "claims": 0, "claims_payload": 0, "claims_page": 0,
        "claims_inserted": 0, "enqueued": 0, "refusals": 0,
        "bodies_eligible": 0, "bodies_fetched": 0, "bodies_from_r2": 0,
        "bodies_mined": 0, "body_fetch_seconds": 0.0, "bodies_batches": 0,
        "bodies_seconds": 0.0, "bodies_pass_complete": False,
        "bodies_backlog_remaining": None, "payload_seconds": 0.0,
        "stopped_early": False, "reached_end": False, "resumed_from_id": after_id,
    }
    refusals: dict[str, int] = {}
    try:
        # BODIES FIRST. The backlog is bounded by the corpus, the payload half by the hour's
        # change, and only the first of those can be behind by 250 000 rows.
        drain_unmined_bodies(
            conn, source=source, page_sources=page_capable,
            entries_by_source=entries_by_source, registers=registers, store=store,
            cap=body_cap, statement_timeout=statement_timeout, budget=budget,
            batch_id=batch_id, dry_run=dry_run, max_value_bytes=max_value_bytes,
            stats=stats, refusals=refusals)

        payload_started = time.monotonic()
        last_seconds = 0.0
        while True:
            if limit is not None and stats["listings"] >= limit:
                stats["stopped_early"] = True
                break
            # NOT "is there time left" but "is there time for ANOTHER BATCH". The estimate
            # is the last batch's measured duration; the first batch is free to start.
            if not budget.room_for(last_seconds):
                LOG.info("INTAKE payload half stopping: %.0fs of budget left, the last "
                         "batch took %.0fs", budget.left() or 0.0, last_seconds)
                stats["stopped_early"] = True
                break
            size = batch_size if limit is None else min(batch_size, limit - stats["listings"])
            batch_started = time.monotonic()

            with guarded(conn, statement_timeout) as cur:
                statement = (_LISTINGS_INCREMENTAL_SQL if mode == "incremental"
                             else _LISTINGS_FULL_SQL)
                cur.execute(statement, {
                    "after_id": after_id, "source": source, "batch_size": size})
                records = cur.fetchall()
                if not records:
                    # The ONLY way this scan earns outcome='ok'. Everything else — a
                    # budget, a limit, an exception — leaves rows unopened behind the
                    # cursor, and a cursor that moves past unopened rows never comes back
                    # for them.
                    stats["reached_end"] = True
                    break

                result = IntakeResult()
                candidates: list[BodyCandidate] = []
                batch_cursor = after_id
                for record in records:
                    scan = _row_from_record(record)
                    # The cursor is read from EVERY record, before any filter: a listing
                    # whose portal has no entries still consumed its slice of the keyset,
                    # and a cursor that stalls on it re-reads the same window forever.
                    batch_cursor = max(
                        batch_cursor,
                        scan.snapshot_cursor if mode == "incremental"
                        else scan.row.listing_id)
                    entries = entries_by_source.get(scan.row.source)
                    if not entries:
                        continue
                    result.extend(extract_listing(
                        scan.row, entries, max_value_bytes=max_value_bytes))
                    candidate = _body_candidate(scan, entries, store=store)
                    if candidate is not None:
                        candidates.append(candidate)

                payload_claims = len(result.claims)
                stats["claims_payload"] += payload_claims
                # THE BOUND ON THE OPPORTUNISTIC MINING. A changed listing gets both halves
                # in one pass, but a contract bump makes every visited body eligible at
                # once — and that backlog belongs to the bodies-first pass, which is
                # ordered, resumable and budgeted, not to the tail of a listing batch.
                candidates = candidates[:body_cap]
                stats["bodies_eligible"] += len(candidates)
                stamps = mine_bodies(
                    cur, candidates, entries_by_source=entries_by_source,
                    registers=registers, store=store, result=result, stats=stats,
                    max_value_bytes=max_value_bytes)

                stats["listings"] += len(records)
                stats["claims"] += len(result.claims)
                stats["claims_page"] += len(result.claims) - payload_claims
                for reason, count in result.refusals.items():
                    refusals[reason] = refusals.get(reason, 0) + count
                    stats["refusals"] += count
                if not dry_run and batch_id is not None:
                    inserted, enqueued = write_result(cur, result)
                    stamp_mined_bodies(cur, stamps)
                    stats["claims_inserted"] += inserted
                    stats["enqueued"] += enqueued
            # ADVANCED ONLY HERE, after the transaction closed. The cursor that gets
            # stamped on the batch row — including by the failure path below — is the last
            # one whose claims are actually committed, which is what makes a 'failed'
            # incremental run safe to resume from.
            after_id = batch_cursor
            last_seconds = time.monotonic() - batch_started
            LOG.info("INTAKE progress listings=%d claims=%d payload=%d page=%d inserted=%d "
                     "bodies eligible=%d fetched=%d from_r2=%d mined=%d in %.1fs "
                     "refusals=%d through_id=%d batch=%.1fs",
                     stats["listings"], stats["claims"], stats["claims_payload"],
                     stats["claims_page"], stats["claims_inserted"],
                     stats["bodies_eligible"], stats["bodies_fetched"],
                     stats["bodies_from_r2"], stats["bodies_mined"],
                     stats["body_fetch_seconds"], stats["refusals"], after_id,
                     last_seconds)
        stats["payload_seconds"] = time.monotonic() - payload_started
    except Exception as exc:
        if batch_id is not None:
            # Guarded like every other write, and for a sharper reason: this is the
            # FAILURE path. Whatever broke the run may be the same pressure that would
            # hang this stamp, and a bookkeeping write that hangs replaces the exception
            # you need with a wedge. A short ceiling fails fast and re-raises the cause.
            try:
                with guarded(conn, _FAILURE_STAMP_TIMEOUT_S) as cur:
                    cur.execute(_BATCH_FINISH_SQL, {
                        "batch_id": batch_id, "outcome": "failed",
                        "row_count": stats["claims_inserted"],
                        "cursor_after_id": after_id,
                        "note": f"{type(exc).__name__}: {exc}"[:500],
                    })
            except Exception:  # noqa: BLE001 - never mask the exception being reported
                LOG.exception("INTAKE could not stamp batch %s as failed", batch_id)
        raise

    # A refusal is a LINE PER REASON WITH A COUNT, which is what the operator reads. It is
    # not a row: `location_claim_absences` held one per refused entry per listing, was
    # written by every lane and read by none, and is gone (migration 498).
    for reason in sorted(refusals):
        LOG.info("INTAKE refused reason=%s count=%d", reason, refusals[reason])
    stats["refusal_reasons"] = dict(sorted(refusals.items()))

    # 'ok' means ONE thing: the scan ran out of rows. A run that ran out of budget instead
    # stamps 'stopped'. Both keep their cursor: in incremental mode the cursor is a
    # position in an append-only log, so the next run picks it up either way.
    outcome = "ok" if stats["reached_end"] else "stopped"
    stats["outcome"] = outcome
    if batch_id is not None:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_BATCH_FINISH_SQL, {
                "batch_id": batch_id,
                "outcome": outcome,
                "row_count": stats["claims_inserted"],
                "cursor_after_id": after_id,
                "note": f"listings={stats['listings']} stopped_early={stats['stopped_early']} "
                        f"reached_end={stats['reached_end']} through_id={after_id} "
                        f"bodies_mined={stats['bodies_mined']} "
                        f"refusals={stats['refusals']}",
            })
    stats["batch_id"] = batch_id
    stats["mode"] = mode
    stats["cursor_after_id"] = after_id

    # THE READOUT COMES AFTER THE TERMINAL STAMP, deliberately. It is a `count(*)` under the
    # 600 s statement ceiling, run at the moment the budget is already spent — ahead of the
    # stamp it could push the job past `timeout-minutes: 55` and lose the cursor of a run
    # that had otherwise finished cleanly, which is the exact failure this wave exists to
    # close. Nothing below this line is allowed to decide whether the run is resumable.
    if page_capable and store is not None:
        stats["bodies_backlog_remaining"] = _unmined_body_backlog(
            conn, source=source, page_sources=page_capable,
            statement_timeout=statement_timeout)

    # ONE LINE THE OPERATOR CAN READ A RUN OFF. The per-batch progress lines say what the
    # run was doing; this says what it achieved and what is left.
    LOG.info("INTAKE summary mode=%s source=%s outcome=%s listings=%d payload_claims=%d "
             "claims_inserted=%d bodies_mined=%d backlog_remaining=%s refusals=%s "
             "bodies=%.0fs payload=%.0fs cursor=%d",
             mode, source or "*", outcome, stats["listings"], stats["claims_payload"],
             stats["claims_inserted"], stats["bodies_mined"],
             stats["bodies_backlog_remaining"]
             if stats["bodies_backlog_remaining"] is not None else "?",
             ",".join(f"{r}={c}" for r, c in stats["refusal_reasons"].items()) or "none",
             stats["bodies_seconds"], stats["payload_seconds"], after_id)
    return stats


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface, separate from `main()` so the defaults are testable without a DB."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--source", choices=SOURCES, default=None)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=None)
    # DEFAULTED, not optional. An unbudgeted run cannot stop cleanly: it walks until the
    # job timeout kills it, stamps nothing, and the next run repeats it (run 34658123746).
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    parser.add_argument("--start-after-id", type=int, default=0,
                        help="a listings.id in full mode, a listing_snapshots.id in "
                             "incremental mode")
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S))
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract and report; write nothing.")
    parser.add_argument("--note", default=None)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    batch_size = max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, args.batch_size))

    with db.connect() as conn:
        try:
            stats = run(
                conn, mode=args.mode, source=args.source, batch_size=batch_size,
                max_seconds=args.max_seconds, limit=args.limit,
                start_after_id=args.start_after_id,
                statement_timeout=args.statement_timeout, dry_run=args.dry_run,
                note=args.note)
        except IntakeRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    LOG.info("INTAKE done %s", json.dumps(stats, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
