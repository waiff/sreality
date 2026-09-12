"""THE claim lane — one hourly pass that mines every substrate we hold for a listing.

WHAT THIS LANE IS (rule 25: one store, one lane, eleven claim types, no flags)
  * A keyset scan of `listings`, hourly, incremental off a `last_seen_at` watermark.
  * Two substrates, ONE registry (`READERS`), one write:
      - `listings.raw_json`, the portal's own payload as we stored it — the payload
        readers below;
      - the STORED PAGE BODY: the latest `portal_raw_payloads` detail row for the
        listing's `(source, source_id_native)`, fetched from R2 and scoped by the
        contract's exclusion zones — the 14 page readers in `location_data.page_readers`.
  * Contract-driven: every claim is stamped with the `portal_contract_entries` row that
    produced it, and the extractor executes exactly those entries whose `locator` names a
    reader from `READERS`. A name in NO registry is a hard refusal (a real deploy error).

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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
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
# (02 §2.1.8). @4 is the one-lane fold: the page body became a substrate of this lane.
INTAKE_VERSION = "claims_intake@4"
LANE = "location_claims_intake"
WAVE = "W1"

MIN_BATCH_SIZE = 10_000
MAX_BATCH_SIZE = 30_000
DEFAULT_BATCH_SIZE = 20_000
# Incremental runs re-read a window behind the last successful batch: a listing written
# while the previous run was mid-flight would otherwise fall between the two watermarks.
# Re-reading is free — values dedupe on the fingerprint, and a body already at the active
# contract version is not fetched a second time.
DEFAULT_OVERLAP_HOURS = 3

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
    rather than flattened into the coordinate (06 §6.2.1). The blurred-label set is data on
    the contract entry (`precision_map.blurred_labels`), so re-calibrating it is a contract
    version bump, not a code change."""
    label = _text(json_pointer(row.raw_json, str(entry.locator["json_pointer"])))
    if label is None:
        return []
    blurred = {str(x) for x in (entry.precision_map.get("blurred_labels") or [])}
    blur = "declared" if label in blurred else "none"
    return [_base(entry, row, value_text=label, declared_precision_label=label,
                  blur_evidence=blur)]


@reader("declared_bool_quality")
def _read_declared_bool_quality(entry: Entry, row: ListingRow) -> list[Claim]:
    """mmreality `accurate` — present on 100% of rows, `false` on 37.2%, and stored
    nowhere today. The boolean is mapped to a LABEL by the contract (`locator.labels`) and
    the blur axis is then decided the same way `declared_quality` decides it: membership
    in the contract's `precision_map.blurred_labels`. Which of the two labels is blurred
    is a portal fact, so it is data on the entry — re-calibrating it is a contract version
    bump, not a code change. Either way the axis is written EXPLICITLY, never defaulted
    (06 §6.6 rule 7)."""
    raw = json_pointer(row.raw_json, str(entry.locator["json_pointer"]))
    if raw is None or not isinstance(raw, bool):
        return []
    labels = entry.locator.get("labels") or {"true": "accurate", "false": "not_accurate"}
    label = str(labels["true" if raw else "false"])
    blurred = {str(x) for x in (entry.precision_map.get("blurred_labels") or [])}
    blur = "declared" if label in blurred else "none"
    return [_base(entry, row, value_text=label, declared_precision_label=label,
                  value_num=1.0 if raw else 0.0, blur_evidence=blur)]


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
    "mapy_affected", "mapy_inventory_runs",
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
         scan_mode, resumable, coverage_since)
    VALUES (%(lane)s, %(source)s, %(extractor_version)s, %(contract_id)s, %(wave)s,
            %(job_run_id)s, 'running', %(note)s, %(scan_mode)s, %(resumable)s,
            coalesce(%(coverage_since)s::timestamptz, now()))
    RETURNING id, coverage_since
"""

_BATCH_FINISH_SQL = """
    UPDATE location_claim_batches
    SET finished_at = now(), outcome = %(outcome)s, row_count = %(row_count)s,
        cursor_after_id = %(cursor_after_id)s, cursor_after_ts = %(cursor_after_ts)s,
        note = concat_ws(' | ', note, %(note)s::text)
    WHERE id = %(batch_id)s
"""

# `outcome = 'ok'` is load-bearing and narrow (migration 387): 'ok' means "the scan ran out
# of rows", never "the scan ran out of budget". A budget-stopped run stamps 'stopped' and is
# INVISIBLE here, so the incremental floor stays where it was and the rows it never opened
# are still in the next run's window.
#
# `coverage_since`, not `started_at`: for a chain of budgeted runs the completing run began
# long after the scan did, and the claim the watermark makes — "everything written before
# this instant has been mined" — is only true back to the FIRST run's start.
_WATERMARK_SQL = """
    SELECT max(coalesce(coverage_since, started_at))
    FROM location_claim_batches
    WHERE lane = %(lane)s AND outcome = 'ok' AND source IS NOT DISTINCT FROM %(source)s
"""

_RESUME_SQL = """
    SELECT outcome, cursor_after_id, cursor_after_ts, coverage_since
    FROM location_claim_batches
    WHERE lane = %(lane)s
      AND source IS NOT DISTINCT FROM %(source)s
      AND scan_mode = %(scan_mode)s
      AND resumable
      AND outcome IN ('ok', 'stopped', 'failed')
    ORDER BY started_at DESC, id DESC
    LIMIT 1
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

_SELECT_COLUMNS = """
    SELECT l.id, l.source, l.source_id_native, l.raw_json, l.last_seen_at,
           ST_Y(l.geom::geometry), ST_X(l.geom::geometry),
           (a.listing_id IS NOT NULL),
           pb.id, (pb.contract_version IS DISTINCT FROM pc.version), pb.page_kind,
           pb.payload_sha256, pb.first_observed_at, pc.version
    FROM listings l
    LEFT JOIN mapy_affected a ON a.listing_id = l.id
"""

# Keyset over the whole table (active AND inactive: a delisted row's payload is exactly the
# evidence the history waves need, and nothing is ever deleted).
_LISTINGS_FULL_SQL = _SELECT_COLUMNS + _BODY_JOIN + """
    WHERE l.id > %(after_id)s
      AND (%(source)s::text IS NULL OR l.source = %(source)s)
    ORDER BY l.id
    LIMIT %(batch_size)s
"""

_LISTINGS_INCREMENTAL_SQL = _SELECT_COLUMNS + _BODY_JOIN + """
    WHERE l.last_seen_at >= %(watermark)s
      AND (l.last_seen_at, l.id) > (%(after_ts)s, %(after_id)s)
      AND (%(source)s::text IS NULL OR l.source = %(source)s)
    ORDER BY l.last_seen_at, l.id
    LIMIT %(batch_size)s
"""

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


def _row_from_record(
    record: tuple[Any, ...],
) -> tuple[ListingRow, page_readers.ArchivedPayload | None, bool, int | None]:
    """One scan row -> (listing, its latest stored detail body or None, unmined?, version).

    A fixed-width unpack: W1-c deleted the class-B legacy-column tail, so the scan projects
    exactly the columns this function names and a drift between the two is a TypeError here
    on the first row rather than every value shifted one position to the left.
    """
    (listing_id, source, native, raw_json, last_seen_at, lat, lon, in_inventory,
     body_id, body_unmined, body_page_kind, body_sha, body_first_observed,
     contract_version) = record
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
    version = int(contract_version) if contract_version is not None else None
    return row, body, bool(body_unmined), version


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
    conn: psycopg.Connection, *, mode: str, source: str | None, watermark: datetime | None,
) -> dict[str, Any] | None:
    """Where this scan should pick up, or None to start at the beginning of its range.

    Only a 'stopped' predecessor is resumed from, and only one written by the SAME
    `scan_mode`: a full cursor is a bare `listings.id` and an incremental one is
    `(last_seen_at, id)`, so crossing them would skip an arbitrary slice."""
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
        # Coverage is claimed back to where the CHAIN started, not this run's own start.
        "coverage_since": coverage_since,
    }


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

    page_capable = {
        s for s in wanted
        if any(e.reader in page_readers.PAGE_READERS
               for e in entries_by_source.get(s, ()))
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

    watermark: datetime | None = None
    if mode == "incremental":
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_WATERMARK_SQL, {"lane": LANE, "source": source})
            row = cur.fetchone()
        watermark = row[0] - timedelta(hours=overlap_hours) if row and row[0] else None
        if watermark is None:
            LOG.info("INTAKE no prior successful batch for source=%s; "
                     "incremental degrades to a full pass", source or "*")
            mode = "full"

    # An operator-anchored run does not certify that everything below its anchor was
    # scanned, so it neither resumes from a stored cursor nor becomes one (the same guard
    # migration 385 puts on `mapy_inventory_runs.resumable`).
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
            LOG.info("INTAKE resuming a budget-stopped %s scan for source=%s from "
                     "after_id=%d after_ts=%s", mode, source or "*", after_id,
                     resumed_from["after_ts"])

    batch_id: int | None = None
    if not dry_run:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_BATCH_INSERT_SQL, {
                "lane": LANE, "source": source, "extractor_version": INTAKE_VERSION,
                "contract_id": contract_id, "wave": WAVE,
                "job_run_id": os.environ.get("GITHUB_RUN_ID"), "note": note,
                "scan_mode": mode, "resumable": not anchored,
                "coverage_since": (resumed_from or {}).get("coverage_since"),
            })
            batch_id = int(cur.fetchone()[0])
    LOG.info("INTAKE start mode=%s source=%s batch=%d inventory_rows=%d batch_id=%s "
             "page_sources=%s store=%s",
             mode, source or "*", batch_size, inventory_rows, batch_id,
             ",".join(sorted(page_capable)) or "-", "yes" if store else "no")

    started = time.monotonic()
    # Resolved once, not per listing: the extractor is called 20 000 times a batch.
    max_value_bytes = env_positive_int(MAX_CLAIM_VALUE_BYTES_ENV,
                                       DEFAULT_MAX_CLAIM_VALUE_BYTES)
    stats: dict[str, Any] = {
        "listings": 0, "claims": 0, "claims_payload": 0, "claims_page": 0,
        "claims_inserted": 0, "enqueued": 0, "refusals": 0,
        "bodies_eligible": 0, "bodies_fetched": 0, "bodies_from_r2": 0,
        "bodies_mined": 0, "body_fetch_seconds": 0.0,
        "stopped_early": False, "reached_end": False, "resumed_from_id": after_id,
    }
    refusals: dict[str, int] = {}
    try:
        while True:
            if limit is not None and stats["listings"] >= limit:
                stats["stopped_early"] = True
                break
            if max_seconds is not None and time.monotonic() - started > max_seconds:
                LOG.info("INTAKE stopping: --max-seconds reached")
                stats["stopped_early"] = True
                break
            size = batch_size if limit is None else min(batch_size, limit - stats["listings"])

            with guarded(conn, statement_timeout) as cur:
                if mode == "incremental":
                    cur.execute(_LISTINGS_INCREMENTAL_SQL, {
                        "watermark": watermark, "after_ts": after_ts, "after_id": after_id,
                        "source": source, "batch_size": size,
                    })
                else:
                    cur.execute(_LISTINGS_FULL_SQL, {
                        "after_id": after_id, "source": source, "batch_size": size})
                records = cur.fetchall()
                if not records:
                    # The ONLY way this scan earns outcome='ok'. Everything else — a
                    # budget, a limit, an exception — leaves rows unopened behind the
                    # cursor, and a watermark that moves past unopened rows never comes
                    # back for them.
                    stats["reached_end"] = True
                    break

                result = IntakeResult()
                candidates: list[tuple[ListingRow, page_readers.ArchivedPayload, int]] = []
                for record in records:
                    row, body, unmined, version = _row_from_record(record)
                    entries = entries_by_source.get(row.source)
                    if not entries:
                        continue
                    result.extend(extract_listing(
                        row, entries, max_value_bytes=max_value_bytes))
                    if (body is not None and unmined and version is not None
                            and store is not None
                            and page_readers.page_entries(entries, body.page_kind)):
                        candidates.append((row, body, version))

                payload_claims = len(result.claims)
                stats["claims_payload"] += payload_claims
                stats["bodies_eligible"] += len(candidates)
                # THE BOUND ON THE PAGE HALF. Body churn is ~50-80/hour fleet-wide, so this
                # only bites after a contract bump makes every latest body eligible at once
                # — where the right answer is to drain it over several runs, not to pull the
                # whole corpus through the bucket in one.
                candidates = candidates[:body_cap]
                stamps: list[dict[str, Any]] = []
                if candidates:
                    fetch_started = time.monotonic()
                    bodies, from_r2 = page_readers.load_bodies(
                        cur, [b.id for _, b, _ in candidates], store=store)
                    stats["body_fetch_seconds"] += time.monotonic() - fetch_started
                    stats["bodies_fetched"] += len(bodies)
                    stats["bodies_from_r2"] += from_r2
                    for row, body, version in candidates:
                        raw = bodies.get(body.id)
                        if raw is None:
                            # The object could not be read this run (see `load_bodies`).
                            # Left UNSTAMPED so the next run asks for it again.
                            continue
                        register = registers.get(row.source)
                        if register is None:
                            continue
                        try:
                            page_result = page_readers.extract_page(
                                replace(body, body=raw), row,
                                entries_by_source[row.source], register=register,
                                max_value_bytes=max_value_bytes)
                        except IntakeRefused as refused:
                            # A CONTENT-triggered refusal about ONE listing — a reader that
                            # returned a coordinate without its position branch, a span the
                            # evidence CHECK would reject, a blur class a migration may not
                            # write. Refusing the whole batch over it is the same wedge a
                            # failed GET would be: the payload claims beside it roll back,
                            # the watermark stays put, and the next run re-reads the same
                            # immutable body and dies identically. Counted, unstamped,
                            # retried at the next contract version.
                            LOG.warning(
                                "PAGE extract refused listing_id=%d source=%s payload_id=%d: %s",
                                row.listing_id, row.source, body.id, refused)
                            result.refuse(f"page_extract_refused:{row.source}")
                            continue
                        result.extend(page_result)
                        if "scope_incomplete" in page_result.refusals:
                            # The scoper fails closed and this body yielded nothing. A
                            # stamp would record it as mined AT this contract version and
                            # hide the miss until the next bump — so leave it unstamped and
                            # let the refusal counter carry it.
                            continue
                        stamps.append({"id": body.id, "version": version})
                    stats["bodies_mined"] += len(stamps)

                after_id = int(records[-1][0])
                if mode == "incremental":
                    after_ts = records[-1][4]
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
            LOG.info("INTAKE progress listings=%d claims=%d payload=%d page=%d inserted=%d "
                     "bodies eligible=%d fetched=%d from_r2=%d mined=%d in %.1fs "
                     "refusals=%d through_id=%d",
                     stats["listings"], stats["claims"], stats["claims_payload"],
                     stats["claims_page"], stats["claims_inserted"],
                     stats["bodies_eligible"], stats["bodies_fetched"],
                     stats["bodies_from_r2"], stats["bodies_mined"],
                     stats["body_fetch_seconds"], stats["refusals"], after_id)
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
                        "cursor_after_id": after_id, "cursor_after_ts": after_ts,
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

    # 'ok' means ONE thing: the scan ran out of rows. A run that ran out of budget
    # instead stamps 'stopped', which `_WATERMARK_SQL` does not see — so the incremental
    # floor stays where it was and everything behind the cursor is still in the next
    # run's window.
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
                "note": f"listings={stats['listings']} stopped_early={stats['stopped_early']} "
                        f"reached_end={stats['reached_end']} through_id={after_id} "
                        f"bodies_mined={stats['bodies_mined']} "
                        f"refusals={stats['refusals']}",
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
    LOG.info("INTAKE done %s", json.dumps(stats, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
