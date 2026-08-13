"""W1 claim intake — deterministic extraction of location claims from `listings.raw_json`.

Design: 06-migration-backfill.md §6.2.1 (the raw_json substrate — cheapest, richest,
first; all nine sources), §6.1.2 (the coordinate-provenance ladder, a filter on the INPUT
applied before anything is written), §6.6 rules 1/3/6/7 (observation timestamps, legacy
anchoring, no claim for a non-storable signal, blur written never defaulted),
03-resolution-pipeline.md §3.2 (the S0 admission contract and the dirty_locations
side effect), 02-portal-contracts.md §2.2 (what each portal publishes).

WHAT THIS LANE IS
  * Pure deterministic extraction. No model, no network, no re-fetch.
  * The substrate is `listings.raw_json` PLUS the class-B legacy columns of 06 §6.1.3 —
    `listings.geom` (ladder-gated) and `listings.locality` (the only surviving copy of the
    locality string wherever the slim-dict payload carries the key with a NULL value).
    §6.1.3 classes some of those columns per WRITER rather than per column, so a contract
    entry may guard its read on a provenance stamp (`locator.require_column_equals`):
    `listings.street` is class B where `street_source='parser'` and class D — quarantine,
    never a claim — where it is `'resolver'` or NULL.
  * Contract-driven: every claim is stamped with the `portal_contract_entries` row that
    produced it, and the extractor executes exactly those entries whose `locator` names a
    `reader` from the registry below. Entries declared for W2 surfaces (html_selector,
    map_config, url_slug, og_meta, jsonld, description) carry no reader and are inert here.
    What a given reader may be declared on — surfaces, extraction methods, the locator
    keys it indexes, whether it consults `transform` / `guards` at all — is
    `contracts.READER_CONTRACTS`, and the `TRANSFORMS` / `GUARDS` registries below are the
    vocabularies an entry that DOES name a reader may draw on. All of it is enforced when
    the contract is projected, so an entry cannot declare something this lane ignores.
  * NO evidence-bearing method runs in W1. `regex_text` / `llm_text` claims need a span
    into a retrievable document, and the content-addressed body store
    (`portal_raw_payloads`) does not fill until W2a — a span into a latest-wins body is a
    one-shot check (01 §4.2). Those entries ship in the contract and stay unexecuted.

THE LICENCE LADDER RUNS FIRST (§6.1.2, and it is a filter, not an audit)
  * `geocode` / bazos `street` / `locality` / absent provenance  -> class E, NO coordinate
    claim, ever. This lane can only ever emit `licence_class = 'portal'`.
  * `carry_forward` is provenance-laundering: admitted only when the listing is ABSENT
    from `mapy_affected` (migration 385 — the C7.2 R2 inventory, a W1 INPUT).
  * Stronger than that, and deliberately: §6.4's blocking W1 gate is
    `claims JOIN <R2 inventory> USING (listing_id) WHERE claim_type='coordinate'` = 0,
    so a listing present in the inventory gets NO coordinate claim on ANY substrate,
    including the three portals whose coordinate is first-party payload.
  * If `mapy_affected` is missing or empty the lane REFUSES to run.

CLI:
    python -m location_data.claims_intake --mode incremental
    python -m location_data.claims_intake --mode full --source sreality --max-seconds 3000
Required: SUPABASE_DB_URL. Additionally requires migrations 380-387 and a projected,
active portal contract per source (`python -m location_data.contracts --load`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from location_data import loader_db
from scraper import db

LOG = logging.getLogger("location_data.claims_intake")

# Bumped whenever the extraction SEMANTICS change. It rides in every claim's batch row and
# in `location_claim_observations.extractor_version`; the per-claim `extractor_version` is
# the contract's own `contract:<portal>@<version>` (02 §2.1.8).
INTAKE_VERSION = "claims_intake@3"
LANE = "location_claims_intake"
WAVE = "W1"

SOURCES = (
    "sreality", "bezrealitky", "bazos", "idnes", "mmreality", "remax", "ceskereality",
    "realitymix", "maxima",
)

# The class-B legacy columns (06 §6.1.3), in the ORDER the two batch queries select them.
# ONE list, because four things have to agree: the SELECT items of both queries, the
# positional unpack in `_row_from_record`, and the `locator.legacy_source_column` /
# `locator.require_column_equals` keys a contract entry is allowed to name. Adding a column
# is one SELECT item + one line here + a contract entry — never a new reader.
#
# `listings.street_source` is here as a GUARD column: nothing reads it as a value, and it
# is the reason `listings.street` can be read at all (06 §6.1.3 admits that column only for
# `street_source='parser'`). `listings.geom` is NOT here — it has its own reader, because
# the licence ladder gates it.
LEGACY_COLUMNS: tuple[str, ...] = (
    "listings.locality", "listings.street", "listings.street_source",
)

MIN_BATCH_SIZE = 10_000
MAX_BATCH_SIZE = 30_000
DEFAULT_BATCH_SIZE = 20_000
# Incremental runs re-read a window behind the last successful batch: a listing written
# while the previous run was mid-flight would otherwise fall between the two watermarks.
# Re-reading is free — values dedupe on the fingerprint and a re-sight appends at most one
# observation per (claim, observed_at).
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
# id-ordered full pass had diluted them across nine portals. There is nothing exotic about
# the arithmetic: one post-cutover sreality listing yields 21 claims ~= 18.9 KB, so a
# 20 000-listing all-sreality batch is ~378 MB in ONE array. The cap is a property of the
# WRITE, not of the scan, so shrinking the batch would only move the cliff: the arrays are
# flushed in chunks bounded by BOTH a row count and a cumulative serialized-byte budget,
# whichever trips first, all inside the same batch transaction as before.
WRITE_CHUNK_ROWS_ENV = "LOCATION_INTAKE_CHUNK_ROWS"
WRITE_CHUNK_BYTES_ENV = "LOCATION_INTAKE_CHUNK_BYTES"
DEFAULT_WRITE_CHUNK_ROWS = 5_000
DEFAULT_WRITE_CHUNK_BYTES = 32 * 1024 * 1024  # ~8x under the hard limit, per statement.

# The second rail, and the one that survives a pathological single row: no chunk budget can
# split ONE array element, so a claim whose value alone dwarfs the budget would still be
# handed to Postgres verbatim. A value this large is not a location claim — it is a portal
# geometry blob that landed in `raw_json` — so it is refused at extraction time and the
# listing is routed to the refetch cohort instead (see `_refuse_oversized`).
MAX_CLAIM_VALUE_BYTES_ENV = "LOCATION_INTAKE_MAX_VALUE_BYTES"
DEFAULT_MAX_CLAIM_VALUE_BYTES = 2 * 1024 * 1024

# 02 §2.1.9 + 06 §6.6 rule 6: a signal we may not store produces NO claim row. This lane
# has exactly one storable lineage, and the guard is asserted at write time.
EMITTABLE_LICENCE_CLASSES = frozenset({"portal"})

# 06 §6.2.2, applied by the loader as a per-source constant: sreality is the only portal
# whose location changes ever appended a snapshot; mmreality/bezrealitky have a payload
# per snapshot but hash-excluded coordinates; the six slim-dict portals have locality-text
# history only. Without the marker "a chart of locality-string changes reads as a chart of
# coordinate changes".
HISTORY_COMPLETENESS: dict[str, str] = {
    "sreality": "full",
    "mmreality": "payload_only",
    "bezrealitky": "payload_only",
    "bazos": "locality_text_only",
    "idnes": "locality_text_only",
    "ceskereality": "locality_text_only",
    "realitymix": "locality_text_only",
    "remax": "locality_text_only",
    "maxima": "locality_text_only",
}

# 06 §6.1.2 rows 4-5: Mapy output under three different stamps ('street'/'locality' are
# bazos' own in-parser geocoder). Kept identical to scripts/location_mapy_inventory.py's
# arm 1 set minus carry_forward, which has its own inventory-conditional rung.
MAPY_COORDS_SOURCES = frozenset({"geocode", "street", "locality"})


@dataclass(frozen=True, slots=True)
class CoordinateRule:
    """Where a portal's coordinate legitimately comes from, as data (06 §6.2.1 + §6.1.2).

    `substrate`:
      payload      - the portal published the coordinate in the body we still hold; the
                     value is re-derived from `raw_json` and is first-party (class A).
      geom_column  - the value survives ONLY in `listings.geom` (the six slim-dict portals
                     never wrote lat/lon into raw_json), so it is migrated as a legacy
                     column and ONLY when the provenance stamp names a first-party path.
      none         - the portal ships no admissible coordinate at all.
    """
    substrate: str
    first_party_sources: frozenset[str] = frozenset()
    carry_forward_admissible: bool = False


COORDINATE_RULES: dict[str, CoordinateRule] = {
    # Post-cutover `locality.gps_lat/gps_lon`; the retired shape yields no coordinate.
    "sreality": CoordinateRule("payload"),
    # `advert.gps{lat,lng}` — 97.4% unique, the cleanest pin of the fleet [live-A §2.5].
    "bezrealitky": CoordinateRule("payload"),
    # The Vue prop's `point{latitude,longitude}` — first-party [06 §6.2.1].
    "mmreality": CoordinateRule("payload"),
    # Only `link` is first-party on bazos: the CZ-guarded maps anchor inside the ad.
    "bazos": CoordinateRule("geom_column", frozenset({"link"}), carry_forward_admissible=True),
    "idnes": CoordinateRule("geom_column", frozenset({"page"}), carry_forward_admissible=True),
    "ceskereality": CoordinateRule("geom_column", frozenset({"page"}),
                                   carry_forward_admissible=True),
    "realitymix": CoordinateRule("geom_column", frozenset({"page"}),
                                 carry_forward_admissible=True),
    "maxima": CoordinateRule("geom_column", frozenset({"page"}),
                             carry_forward_admissible=True),
    # remax stamps NO `coords` key at all while its geocoder was enabled — every remax
    # coordinate is of unestablished provenance [db-raw §3.4, 06 §6.1.2 last row].
    "remax": CoordinateRule("none"),
}

# Same envelope as `location_constants.cz_bbox` (migration 380) and as
# location_data/krovak.py, which is the module that owns it. The literal below is the
# import fallback only — it exists so this module still imports while PR #1010 (the RÚIAN
# loader, which adds krovak.py) is unmerged, and it is byte-identical to that constant.
_CZ_BBOX_FALLBACK = (48.0, 51.5, 12.0, 19.0)


def cz_bbox() -> tuple[float, float, float, float]:
    """(lat_min, lat_max, lon_min, lon_max) — the ONE canonical CZ envelope."""
    try:
        from location_data.krovak import CZ_LAT_MAX, CZ_LAT_MIN, CZ_LON_MAX, CZ_LON_MIN
    except ImportError:
        return _CZ_BBOX_FALLBACK
    return CZ_LAT_MIN, CZ_LAT_MAX, CZ_LON_MIN, CZ_LON_MAX


def env_positive_int(name: str, default: int) -> int:
    """A positive-integer knob, overridable per environment.

    Re-exported from `loader_db`, which owns the budget helpers every location lane
    shares: a typo or a non-positive value is the default, not a crash — and for these
    knobs 0 would mean "no bound at all", which is exactly the state they exist to stop.
    """
    return loader_db.env_positive_int(name, default)


class IntakeRefused(RuntimeError):
    """A blocking precondition failed; nothing was written."""


# ------------------------------------------------------------------ value objects

@dataclass(frozen=True, slots=True)
class Entry:
    """One `portal_contract_entries` row, as the extractor reads it."""
    id: int
    source: str
    contract_id: int
    contract_version: int
    entry_id: str
    surface: str
    page_kind: str
    locator: dict[str, Any]
    claim_type: str
    extraction_method: str
    subject_scope: dict[str, Any]
    transform: tuple[str, ...]
    precision_map: dict[str, Any]
    default_blur_evidence: str
    default_licence_class: str
    cardinality: str
    guards: tuple[str, ...]

    @property
    def reader(self) -> str | None:
        value = self.locator.get("reader")
        return str(value) if value else None

    @property
    def extractor_version(self) -> str:
        return f"contract:{self.source}@{self.contract_version}"


@dataclass(frozen=True, slots=True)
class ListingRow:
    listing_id: int
    source: str
    source_id_native: str
    raw_json: dict[str, Any]
    lat: float | None
    lon: float | None
    observed_at: datetime
    in_mapy_inventory: bool
    # `LEGACY_COLUMNS` -> value, keyed by the SAME string a contract entry puts in
    # `locator.legacy_source_column` / `locator.require_column_equals`. Always ALL of
    # them: a key that is absent is a scan/contract mismatch and is refused, not read as
    # NULL (`_legacy_column`).
    legacy_columns: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Claim:
    listing_id: int
    source: str
    source_id_native: str
    claim_type: str
    surface: str
    page_kind: str
    extraction_method: str
    extractor_id: str
    extractor_version: str
    contract_entry_id: int
    snapshot_anchor: str
    first_observed_at: datetime
    blur_evidence: str
    licence_class: str
    history_completeness: str
    value_text: str | None = None
    value_num: float | None = None
    value_geom_wkt: str | None = None
    value_shape_wkt: str | None = None
    value_jsonb: Any | None = None
    distance_m: int | None = None
    travel_mode: str | None = None
    target_text: str | None = None
    declared_precision_label: str | None = None
    declared_confidence: str | None = None
    declared_radius_m: float | None = None
    subject_scoped: bool | None = None
    legacy_source_column: str | None = None
    legacy_write_path_unknown: bool = False
    # The EXTRACTOR's confidence in this claim (`match_confidence`), not the portal's
    # declaration — that is `declared_confidence`. NULL on every payload-derived claim;
    # 06 §6.1.1 caps a class-B legacy column at 'medium' and the contract entry says so.
    claim_confidence: str | None = None
    # NULL on every W1 claim (the substrate is latest-wins `listings.raw_json`, which has
    # no snapshot to anchor to). W3 (`location_data.claims_remine`) is the first writer
    # that sets this: a claim mined from `listing_snapshots` carries its row's id here and
    # `snapshot_anchor='snapshot'` (01 §4.2's `loc_claim_anchor` CHECK pairs the two — see
    # 00 §3.3). Present here, not on a W3-only subclass, so `location_claims_intake` and
    # `location_claims_remine` share one `Claim` shape, one `to_row()`, and one writer.
    snapshot_id: int | None = None

    def to_row(self) -> dict[str, Any]:
        row = {
            "listing_id": self.listing_id,
            "source": self.source,
            "source_id_native": self.source_id_native,
            "snapshot_id": self.snapshot_id,
            "snapshot_anchor": self.snapshot_anchor,
            "first_observed_at": self.first_observed_at.isoformat(),
            "claim_type": self.claim_type,
            "surface": self.surface,
            "page_kind": self.page_kind,
            "extraction_method": self.extraction_method,
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
            "contract_entry_id": self.contract_entry_id,
            "value_text": self.value_text,
            "value_num": self.value_num,
            "value_geom_wkt": self.value_geom_wkt,
            "value_shape_wkt": self.value_shape_wkt,
            "value_jsonb": self.value_jsonb,
            "distance_m": self.distance_m,
            "travel_mode": self.travel_mode,
            "target_text": self.target_text,
            "declared_precision_label": self.declared_precision_label,
            "declared_confidence": self.declared_confidence,
            "declared_radius_m": self.declared_radius_m,
            "claim_confidence": self.claim_confidence,
            "blur_evidence": self.blur_evidence,
            "licence_class": self.licence_class,
            "legacy_source_column": self.legacy_source_column,
            "legacy_write_path_unknown": self.legacy_write_path_unknown,
            "history_completeness": self.history_completeness,
            "subject_scoped": self.subject_scoped,
        }
        return row


@dataclass(frozen=True, slots=True)
class Absence:
    """A negative assertion (03 §3.2: "tried and found nothing" must be distinguishable
    from "never tried"). W1 records the two that would otherwise be invisible: a
    coordinate withheld by the licence ladder, and a payload it could not read."""
    listing_id: int
    surface: str
    field_: str
    reason: str
    extraction_method: str
    detail: str
    # NULL for W1 (no snapshot). W3 sets it: migration 382's absence key is
    # `(listing_id, snapshot_key, surface, field, extractor_version)` with
    # `snapshot_key = coalesce(snapshot_id, -1)` PRECISELY so a withheld coordinate at
    # snapshot N and the same withholding at snapshot N+5 are two rows, not one collapsed
    # by the unique index.
    snapshot_id: int | None = None

    def to_row(self, extractor_version: str) -> dict[str, Any]:
        return {
            "listing_id": self.listing_id,
            "snapshot_id": self.snapshot_id,
            "surface": self.surface,
            "field": self.field_,
            "reason": self.reason,
            "extraction_method": self.extraction_method,
            "extractor_version": extractor_version,
        }


@dataclass(frozen=True, slots=True)
class EnrichmentTask:
    """A row routed to a per-method refetch cohort (`location_enrichment_state`)."""
    listing_id: int
    method: str
    lane: str
    outcome: str
    input_hash: str
    error: str | None = None

    def to_row(self, extractor_version: str) -> dict[str, Any]:
        return {
            "listing_id": self.listing_id,
            "method": self.method,
            "lane": self.lane,
            "last_outcome": self.outcome,
            "last_error": self.error,
            "input_hash": self.input_hash,
            "extractor_version": extractor_version,
        }


@dataclass(slots=True)
class IntakeResult:
    claims: list[Claim] = field(default_factory=list)
    absences: list[Absence] = field(default_factory=list)
    enrichment: list[EnrichmentTask] = field(default_factory=list)
    # Claims refused by the value-size cap. Counted, never silently dropped: each one also
    # produced an absence row and a refetch-cohort row (`_refuse_oversized`).
    oversized: int = 0

    def extend(self, other: IntakeResult) -> None:
        self.claims.extend(other.claims)
        self.absences.extend(other.absences)
        self.enrichment.extend(other.enrichment)
        self.oversized += other.oversized


@dataclass(frozen=True, slots=True)
class CoordinateVerdict:
    admitted: bool
    licence_class: str | None
    reason: str


# ------------------------------------------------------------------ the licence ladder

def coordinate_verdict(
    source: str, coords_source: str | None, *, in_mapy_inventory: bool,
) -> CoordinateVerdict:
    """06 §6.1.2, applied to the INPUT. Never returns a non-`portal` licence class:
    a class-E coordinate produces no claim at all (§6.6 rule 6)."""
    rule = COORDINATE_RULES.get(source)
    if rule is None:
        return CoordinateVerdict(False, None, "unknown_source")
    # The W1 blocking gate (§6.4) is `claims JOIN <R2 inventory> WHERE claim_type =
    # 'coordinate'` = 0, so inventory membership vetoes every substrate, not just
    # carry_forward: a listing can enter the inventory through arm 2 (a geocode was
    # attempted) or arm 3 (its geom matches a cached Mapy coordinate) while its payload
    # coordinate looks first-party.
    if in_mapy_inventory:
        return CoordinateVerdict(False, None, "listing_in_mapy_affected_inventory")
    if rule.substrate == "none":
        return CoordinateVerdict(False, None, "no_first_party_coordinate_on_this_portal")
    if rule.substrate == "payload":
        return CoordinateVerdict(True, "portal", "portal_published_payload_coordinate")
    # geom_column: the provenance stamp is the only thing that can license the value.
    if coords_source is None:
        return CoordinateVerdict(False, None, "coordinate_provenance_unestablished")
    if coords_source in MAPY_COORDS_SOURCES:
        return CoordinateVerdict(False, None, "mapy_derived_coordinate")
    if coords_source == "carry_forward":
        if not rule.carry_forward_admissible:
            return CoordinateVerdict(False, None, "carry_forward_not_admissible")
        return CoordinateVerdict(True, "portal", "carry_forward_absent_from_mapy_inventory")
    if coords_source in rule.first_party_sources:
        return CoordinateVerdict(True, "portal", f"first_party_{coords_source}")
    return CoordinateVerdict(False, None, "unrecognised_coordinate_provenance")


# ------------------------------------------------------------------ payload helpers

def json_pointer(payload: Any, pointer: str) -> Any:
    """RFC 6901 subset: `/a/b/0`. Returns None for any miss."""
    if pointer in ("", "/"):
        return payload
    node = payload
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return None
            node = node[token]
        elif isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ transforms

# The ordered normalisers a contract entry may declare (02 §2.1.2), as a REGISTRY rather
# than an if/elif chain: `contracts.IMPLEMENTED_TRANSFORMS` refuses an executable entry
# naming a transform that is not here, and that gate needs a name it can enumerate.
# A transform is `name[:arg]` and sees the value only when it is non-None.
TransformFn = Callable[[str, str], str | None]
TRANSFORMS: dict[str, TransformFn] = {}


def transform(name: str) -> Callable[[TransformFn], TransformFn]:
    def register(fn: TransformFn) -> TransformFn:
        TRANSFORMS[name] = fn
        return fn
    return register


@transform("sentinel_drop")
def _sentinel_drop(value: str, arg: str) -> str | None:
    return None if value == arg else value


@transform("psc_normalise")
def _psc_normalise(value: str, arg: str) -> str | None:
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits if len(digits) == 5 else None


@transform("split_cp_co")
def _split_cp_co(value: str, arg: str) -> str | None:
    """Czech `čp/čo` pairs arrive as "655/31"; the pair is not two alternatives."""
    head, sep, tail = value.partition("/")
    if arg == "cp":
        return head.strip() or None
    if arg == "co":
        return (tail.strip() or None) if sep else None
    return value


@transform("strip_prefix")
def _strip_prefix(value: str, arg: str) -> str | None:
    return value[len(arg):].strip() if value.startswith(arg) else value


def apply_transforms(value: str | None, transforms: tuple[str, ...]) -> str | None:
    """The ordered normalisers a contract entry declares (02 §2.1.2).

    An unknown name is a no-op here rather than a refusal: the projection in the DB can be
    older than this image (a rollback), and a whole batch must not die over a normaliser.
    The gate that stops it reaching a live entry at all is `contracts._check_executable`.
    """
    for spec in transforms:
        if value is None:
            return None
        name, _, arg = spec.partition(":")
        fn = TRANSFORMS.get(name)
        if fn is not None:
            value = fn(value, arg)
    return value.strip() if isinstance(value, str) and value.strip() else value or None


def point_wkt(lat: float, lon: float) -> str:
    return f"POINT({lon!r} {lat!r})"


def envelope_wkt(lat_min: float, lon_min: float, lat_max: float, lon_max: float) -> str:
    corners = (
        (lon_min, lat_min), (lon_max, lat_min), (lon_max, lat_max),
        (lon_min, lat_max), (lon_min, lat_min),
    )
    return "POLYGON((" + ", ".join(f"{x!r} {y!r}" for x, y in corners) + "))"


def in_cz_bbox(lat: float, lon: float) -> bool:
    lat_min, lat_max, lon_min, lon_max = cz_bbox()
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


# ------------------------------------------------------------------ guards

# The reject rules a contract entry may declare (02 §2.1.2). W1 implements exactly one —
# the rest of the vocabulary (`reject_if_in_excluded_zone`, `require_czech_street_morphology`,
# `reject_empty_geometry`, …) needs substrates this lane does not have. A guard the runtime
# does not implement rejects nothing, silently, so `contracts.IMPLEMENTED_GUARDS` mirrors
# this registry and refuses one on an entry that actually executes. Only the three readers
# below that CALL `guard_admits` consult them at all, which the same gate enforces
# (`contracts.READER_CONTRACTS[...].consults_guards`) — being implemented is not enough.
GuardFn = Callable[[float, float], bool]
GUARD_CZ_BBOX = "reject_outside_cz_bbox"
GUARDS: dict[str, GuardFn] = {GUARD_CZ_BBOX: in_cz_bbox}


def guard_admits(entry: Entry, name: str, *points: tuple[float, float]) -> bool:
    """False only when the entry declares guard `name` and a point fails it.

    An unknown name admits, for the same reason `apply_transforms` no-ops one: the
    projection in the DB can be older than this image (a rollback), and a whole batch must
    not die over a guard. The gate that stops one reaching a live entry — implemented or
    not, consulted by this reader or not — is `contracts._check_executable`.
    """
    if name not in entry.guards:
        return True
    predicate = GUARDS.get(name)
    if predicate is None:
        return True
    return all(predicate(lat, lon) for lat, lon in points)


def sreality_payload_shape(raw: dict[str, Any]) -> str:
    """`post_cutover` | `legacy` | `absent`.

    `absent` is the 80 KB-truncation cohort — one sreality row's raw_json was truncated by
    a geometry blob and lost the whole `locality` object (06 §6.2.1 caveat 2). Both
    non-post-cutover shapes route to the refetch cohort rather than emitting "no claim".
    """
    locality = raw.get("locality")
    if not isinstance(locality, dict):
        return "absent"
    if {"gps_lat", "gps_lon", "entity_type", "inaccuracy_type", "city", "citypart"} & set(locality):
        return "post_cutover"
    if {"name", "value", "accuracy"} & set(locality):
        return "legacy"
    return "absent"


def payload_hash(raw: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


# ------------------------------------------------------------------ value_norm mirror

# Characters where PostgreSQL's `unaccent` DICTIONARY and Python's NFKD combining-mark
# strip disagree: the dictionary expands or maps them (ß->ss, æ->ae, ø->o, đ->d, ł->l …)
# while NFKD leaves them intact, so the Python mirror would fold them to a space instead.
# They are rare in Czech and NOT rare in this corpus — the program exists partly to find
# the German, Polish and Nordic addresses hiding in it (remax 442804 is in Poland; bazos
# has an 835-row `Zahraničí` bucket). This set is why `claim_fingerprint` is computed in
# SQL and never in Python: a mirror that drifts on exactly the foreign cohort would make
# the unique index stop deduping, silently, in an append-only table.
MIRROR_UNSAFE_CHARS = frozenset("ßæÆœŒøØđĐłŁðÐþÞħĦŧŦıĸŉ")


def mirror_is_faithful(value: str | None) -> bool:
    """True when the Python mirror provably agrees with `location_value_norm()`."""
    return value is None or not (MIRROR_UNSAFE_CHARS & set(value))


def value_norm_mirror(value: str | None) -> str | None:
    """DIAGNOSTIC ONLY — never on the write path.

    Mirrors migration 382's
    `nullif(btrim(regexp_replace(lower(unaccent(p_value)), '[^a-z0-9]+', ' ', 'g')), '')`
    so a dry run can show what a claim's `value_norm` will become and so the parity test
    has something to compare. The authoritative definition is the SQL function; see
    `_CLAIM_FINGERPRINT_SQL`.
    """
    if value is None:
        return None
    try:
        from location_data.name_index import normalize_name
    except ImportError:  # the RÚIAN loader (PR #1010) is not merged yet
        import re
        import unicodedata

        def normalize_name(raw: str) -> str:
            stripped = "".join(
                ch for ch in unicodedata.normalize("NFKD", raw)
                if not unicodedata.combining(ch))
            return re.sub(r"[^0-9a-z]+", " ", stripped.lower()).strip()

    return normalize_name(value) or None


# ------------------------------------------------------------------ readers

ReaderFn = Callable[[Entry, ListingRow], list[Claim]]
READERS: dict[str, ReaderFn] = {}


def reader(name: str) -> Callable[[ReaderFn], ReaderFn]:
    def register(fn: ReaderFn) -> ReaderFn:
        READERS[name] = fn
        return fn
    return register


def _base(entry: Entry, row: ListingRow, **overrides: Any) -> Claim:
    """Every claim is stamped identically: contract identity, anchor, blur, licence.

    `blur_evidence` and `licence_class` are always passed explicitly — 06 §6.6 rule 7:
    letting the column default fire stamps "no blur observed" onto the rows that carry a
    portal blur flag, and in an append-only table that is unrecoverable.
    """
    anchor = "unanchored_legacy" if entry.surface == "legacy_column" else "unanchored_latest_fetch"
    # 01 §4.2's `loc_claim_legacy` CHECK: a legacy claim must name its column. The contract
    # validator requires the key, so this can only be missing if the projection is stale.
    legacy_column = (str(entry.locator["legacy_source_column"])
                     if entry.extraction_method == "legacy_column" else None)
    fields: dict[str, Any] = {
        "listing_id": row.listing_id,
        "source": row.source,
        "source_id_native": row.source_id_native,
        "claim_type": entry.claim_type,
        "surface": entry.surface,
        "page_kind": entry.page_kind,
        "extraction_method": entry.extraction_method,
        "extractor_id": entry.entry_id,
        "extractor_version": entry.extractor_version,
        "contract_entry_id": entry.id,
        "snapshot_anchor": anchor,
        "first_observed_at": row.observed_at,
        "blur_evidence": entry.default_blur_evidence,
        "licence_class": entry.default_licence_class,
        "history_completeness": HISTORY_COMPLETENESS[row.source],
        "subject_scoped": entry.subject_scope.get("subject_scoped", True),
        "legacy_source_column": legacy_column,
    }
    fields.update(overrides)
    return Claim(**fields)


@reader("scalar")
def _read_scalar(entry: Entry, row: ListingRow) -> list[Claim]:
    value = _text(json_pointer(row.raw_json, str(entry.locator["json_pointer"])))
    value = apply_transforms(value, entry.transform)
    if value is None:
        return []
    number = _number(value) if entry.locator.get("value_kind") == "num" else None
    return [_base(entry, row, value_text=value, value_num=number)]


def _legacy_column(entry: Entry, row: ListingRow, column: str) -> Any:
    """One legacy column's value for this row, refusing a column the scan never selected.

    `.get()` would be wrong here and silently so: a contract naming a column that is not in
    `LEGACY_COLUMNS` would read as NULL on every row forever — no claim, no absence, no
    error, and a coverage gap whose cause is invisible. That is a projection/scan mismatch,
    i.e. a deploy error, so it is refused exactly like an unknown reader is.
    """
    if column not in row.legacy_columns:
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} names legacy column '{column}', which the "
            f"batch query does not select (LEGACY_COLUMNS = "
            f"{', '.join(LEGACY_COLUMNS)})")
    return row.legacy_columns[column]


def legacy_guard_passes(entry: Entry, row: ListingRow) -> bool:
    """`locator.require_column_equals` — a legacy column admitted for ONE writer only.

    06 §6.1.3 does not class `listings.street` as a column, it classes it per writer:
    class B where `street_source='parser'` (portal-derived text, capped at `medium` with
    mandatory gazetteer revalidation), class D where it is `'resolver'` (a RÚIAN
    address-point inference — ~11 of ~21 text-checkable ones are wrong) or NULL (the
    unattributable legacy-write cohort; two live backfill scripts write the column and
    stamp nothing). A class-D value is quarantine and is never read by the resolver, so it
    must never become a claim in the first place.

    The predicate is contract DATA — `{column: required_value}`, keyed exactly like
    `legacy_columns` — so a portal that needs a different split is a version bump and never
    a branch in this module. It is one equality against a provenance stamp, deliberately:
    a NULL stamp equals nothing, which is the refusal §6.1.3 asks for.
    """
    required = entry.locator.get("require_column_equals")
    if not required:
        return True
    for column, expected in dict(required).items():
        actual = _legacy_column(entry, row, str(column))
        if actual is None or str(actual) != str(expected):
            return False
    return True


@reader("legacy_text_column")
def _read_legacy_text_column(entry: Entry, row: ListingRow) -> list[Claim]:
    """A class-B `listings` TEXT column, migrated as a claim (06 §6.1.1, §6.1.3).

    The payload is not always the substrate: `listings.locality` is populated on rows whose
    slim-dict payload carries the locality key with a NULL value (a parse that found
    nothing, a portal that never published the string at all — remax has no locality key),
    and for those rows the column is the only surviving copy. Class B is exactly that case:
    `extraction_method='legacy_column'`, `surface='legacy_column'`,
    `snapshot_anchor='unanchored_legacy'` (all three from `_base`), `licence_class='portal'`,
    `blur_evidence='none'` written explicitly (§6.6 rule 7) and confidence capped at
    `medium` by the CONTRACT (`locator.claim_confidence`), never by this function.

    `legacy_write_path_unknown` is the entry's own declaration (§6.6 rule 3): these columns
    have no provenance stamp, and on realitymix the geocode backfill synthesised some of
    them ('Hranicka, Prerov' where the payload's own `locality_text` is null), so a claim
    that cannot name its writer says so rather than passing as portal-published. A column
    that DOES carry a stamp is guarded on it instead (`legacy_guard_passes`), and then the
    writer is named rather than unknown.

    A blocked guard produces nothing — no claim AND no absence. W1 records exactly the two
    negatives of 06 §6.1.5, and a class-D value is not "tried and found nothing": the
    portal stated no such thing, our own resolver did, and §6.1.5 puts that in quarantine,
    not in the claims layer.
    """
    if not legacy_guard_passes(entry, row):
        return []
    column = str(entry.locator["legacy_source_column"])
    value = apply_transforms(_text(_legacy_column(entry, row, column)), entry.transform)
    if value is None:
        return []
    return [_base(entry, row, value_text=value,
                  claim_confidence=entry.locator.get("claim_confidence"),
                  legacy_write_path_unknown=bool(
                      entry.locator.get("write_path_unknown", False)))]


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


@reader("geom_column")
def _read_geom_column(entry: Entry, row: ListingRow) -> list[Claim]:
    """The six slim-dict portals never wrote lat/lon into `raw_json` — only the provenance
    stamp — so `listings.geom` is the only copy of the value (06 §6.1.3). It is migrated as
    a legacy column, and only after the ladder has licensed it."""
    if row.lat is None or row.lon is None:
        return []
    coords_source = _text(json_pointer(row.raw_json, "/coords/source"))
    verdict = coordinate_verdict(row.source, coords_source,
                                 in_mapy_inventory=row.in_mapy_inventory)
    if not verdict.admitted:
        return []
    if not guard_admits(entry, GUARD_CZ_BBOX, (row.lat, row.lon)):
        return []
    return [_base(entry, row, value_geom_wkt=point_wkt(row.lat, row.lon),
                  licence_class=verdict.licence_class or "portal",
                  legacy_source_column="listings.geom",
                  value_jsonb={"coords_source": coords_source, "ladder": verdict.reason})]


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


@reader("coords_stamp_quality")
def _read_coords_stamp_quality(entry: Entry, row: ListingRow) -> list[Claim]:
    """`raw_json.coords{}` is a scraper-authored provenance record grading OUR OWN
    geocoder [db-raw §3.4]. It is emitted as a `precision_declaration` (there is no
    `geocode_quality_declared` claim type — 00 §2.2) and ONLY when the coordinate it
    describes was itself admitted: grading a coordinate the licence ladder refused to store
    would assert a precision for a value that does not exist (06 §6.2.1)."""
    coords = json_pointer(row.raw_json, "/coords")
    if not isinstance(coords, dict):
        return []
    coords_source = _text(coords.get("source"))
    verdict = coordinate_verdict(row.source, coords_source,
                                 in_mapy_inventory=row.in_mapy_inventory)
    if not verdict.admitted:
        return []
    confidence = _text(coords.get("confidence")) or _text(coords.get("locality_confidence"))
    stamp = {k: v for k, v in coords.items() if k not in ("lat", "lng", "lon", "latitude",
                                                          "longitude")}
    return [_base(entry, row, value_text=coords_source,
                  declared_confidence=confidence, value_jsonb=stamp,
                  legacy_source_column="raw_json.coords")]


# ------------------------------------------------------------------ the value-size cap

def claim_value_bytes(claim: Claim) -> int:
    """Serialized size of a claim's VALUE payload — the only unbounded part of a claim row.

    Everything else on the row is identity and provenance: bounded by the contract. The
    value is not: `raw_json` on the legacy-shape sreality cohort carries whole geometry
    objects (the same blobs that truncated one row's payload at 80 KB — sreality.yaml
    §caveats), and a reader that stores its node verbatim into `value_jsonb` inherits that
    size. Measured with `default=str` for the same reason `payload_hash` uses it: the
    payload can hold a Decimal or a datetime that plain `json.dumps` refuses.
    """
    total = 0
    if claim.value_jsonb is not None:
        total += len(json.dumps(claim.value_jsonb, ensure_ascii=False,
                                default=str).encode("utf-8"))
    for text in (claim.value_text, claim.value_geom_wkt, claim.value_shape_wkt,
                 claim.target_text):
        if text is not None:
            total += len(text.encode("utf-8"))
    return total


def _refuse_oversized(
    row: ListingRow, claims: list[Claim], *, max_value_bytes: int,
) -> tuple[list[Claim], list[Absence], list[EnrichmentTask]]:
    """Partition off claims whose value exceeds the cap. NOTHING is silently dropped.

    A dropped claim and a claim that was never produced are indistinguishable in an
    append-only store, which is the exact failure 03 §3.2 forbids ("every attempt is
    recorded, including negatives" — without it "recall and honesty are indistinguishable").
    So a refusal writes the same two artefacts the truncated-payload path writes:

      * `location_claim_absences` — reason `not_attempted`. The vocabulary is CHECK-
        constrained to ('not_stated','stated_but_ambiguous','only_in_excluded_block',
        'not_attempted') by migration 382 / 01 §4.4, so there is no `oversized_payload`
        label to add without DDL, and the other three would misreport what happened:
        the portal DID state the value (`not_stated` is false), the value is not
        semantically ambiguous (01 §4.4 binds `stated_but_ambiguous` to the extraction
        schema's `ambiguity_flags`), and no contract `exclusion_zone` was involved
        (03 §3.2 rule 4 binds `only_in_excluded_block` to that mechanism). `not_attempted`
        is this module's own precedent for "the substrate was there and this lane declined
        to complete the extraction into a stored value" — it is what the licence ladder
        already writes for a withheld coordinate.
      * `location_enrichment_state` — the per-method refetch cohort (02 P6), exactly as the
        truncated-locality path routes. `last_error` is the only free-text field persisted
        anywhere on this path (`location_claim_absences` has no note column), so it carries
        the detail the absence row cannot: which claim type, and how many bytes.
    """
    kept: list[Claim] = []
    absences: list[Absence] = []
    enrichment: list[EnrichmentTask] = []
    for claim in claims:
        size = claim_value_bytes(claim)
        if size <= max_value_bytes:
            kept.append(claim)
            continue
        detail = (f"{claim.claim_type} value from {claim.extractor_id} is {size} bytes "
                  f"(cap {max_value_bytes}); refused so the claim array cannot exceed "
                  f"Postgres's 256 MB jsonb limit")
        LOG.warning("INTAKE oversized value refused listing_id=%d source=%s claim_type=%s "
                    "extractor_id=%s bytes=%d cap=%d",
                    row.listing_id, row.source, claim.claim_type, claim.extractor_id,
                    size, max_value_bytes)
        absences.append(Absence(
            listing_id=row.listing_id, surface=claim.surface, field_=claim.claim_type,
            reason="not_attempted", extraction_method=claim.extraction_method,
            detail=detail))
        enrichment.append(EnrichmentTask(
            listing_id=row.listing_id, method=claim.extraction_method,
            lane=f"{row.source}_detail_refetch", outcome="error",
            input_hash=payload_hash(row.raw_json), error=detail))
    return kept, absences, enrichment


# ------------------------------------------------------------------ extraction

def extract_listing(
    row: ListingRow, entries: list[Entry], *, max_value_bytes: int | None = None,
    route_legacy_shape_to_refetch: bool = True,
) -> IntakeResult:
    """Everything this lane knows about one listing. Pure — no DB, no clock, no network.

    `route_legacy_shape_to_refetch` gates the sreality-legacy-shape tail below. It defaults
    True (unchanged W1 behaviour: a CURRENT listing whose payload is legacy-shape or
    truncated is genuinely worth a live detail refetch). `location_data.claims_remine`
    (W3) passes False: a SNAPSHOT's payload is whatever sreality's API actually returned at
    that historical instant — legacy-shape there is an accurate historical fact, not a gap
    a refetch could ever close, and routing it into `location_enrichment_state` would flood
    the real refetch cohort with attempts against rows that were never wrong, only old.
    """
    if max_value_bytes is None:
        max_value_bytes = env_positive_int(MAX_CLAIM_VALUE_BYTES_ENV,
                                           DEFAULT_MAX_CLAIM_VALUE_BYTES)
    result = IntakeResult()
    coordinate_entry: Entry | None = None

    for entry in entries:
        name = entry.reader
        if not name:
            continue  # declared for a W2 surface; inert here.
        fn = READERS.get(name)
        if fn is None:
            raise IntakeRefused(
                f"{entry.source}:{entry.entry_id} declares unknown reader '{name}'")
        if entry.claim_type == "coordinate":
            coordinate_entry = entry
        for claim in fn(entry, row):
            if claim.licence_class not in EMITTABLE_LICENCE_CLASSES:
                raise IntakeRefused(
                    f"{entry.entry_id} produced licence_class='{claim.licence_class}'; "
                    f"this lane may only emit {sorted(EMITTABLE_LICENCE_CLASSES)} "
                    f"(06 §6.6 rule 6)")
            result.claims.append(claim)

    # Before anything else reads `result.claims`: a value too large to write is not a claim
    # this lane can make. Runs FIRST among the negative-artefact producers so that when a
    # legacy-shape sreality row is BOTH oversized and refetch-worthy for its shape, the
    # refusal's `last_error` is the enrichment row that survives `dedupe_enrichment_rows`
    # (first-writer-wins on the same (listing, method, lane) key).
    result.claims, refused_absences, refused_enrichment = _refuse_oversized(
        row, result.claims, max_value_bytes=max_value_bytes)
    result.absences.extend(refused_absences)
    result.enrichment.extend(refused_enrichment)
    result.oversized = len(refused_absences)

    withheld = (row.lat is not None and row.lon is not None
                and not any(c.claim_type == "coordinate" for c in result.claims))
    if coordinate_entry is not None and withheld:
        verdict = coordinate_verdict(
            row.source, _text(json_pointer(row.raw_json, "/coords/source")),
            in_mapy_inventory=row.in_mapy_inventory)
        if not verdict.admitted:
            # A coordinate the ladder refused must not read as "the portal published
            # none". The class-E cohort is recorded as a negative assertion instead
            # (06 §6.1.5's "same negative artefact with no value attached", 03 §3.2's
            # "every attempt is recorded, including negatives") — identity and reason
            # only, never the coordinate.
            result.absences.append(Absence(
                listing_id=row.listing_id, surface=coordinate_entry.surface,
                field_="coordinate", reason="not_attempted",
                extraction_method=coordinate_entry.extraction_method, detail=verdict.reason))

    if row.source == "sreality":
        shape = sreality_payload_shape(row.raw_json)
        if shape != "post_cutover":
            # 06 §6.2.1 caveat: a legacy-shape row can never yield
            # zip/housenumber/entity_type/inaccuracy_type and a truncated one lost the
            # locality object outright. `route_legacy_shape_to_refetch` gates ONLY the
            # refetch-cohort enrollment (meaningless for a snapshot — there is nothing left
            # to refetch, the payload IS what sreality returned back then); the truncation
            # absence stays unconditional either way, because it is a negative ASSERTION
            # about this specific payload (03 §3.2 rule 4: every attempt is recorded,
            # including negatives), not a request for future work.
            if route_legacy_shape_to_refetch:
                result.enrichment.append(EnrichmentTask(
                    listing_id=row.listing_id,
                    method="portal_structured_field",
                    lane="sreality_detail_refetch",
                    outcome="skipped" if shape == "legacy" else "error",
                    input_hash=payload_hash(row.raw_json),
                    error=None if shape == "legacy"
                    else "locality object absent from raw_json (payload truncation)"))
            if shape == "absent":
                result.absences.append(Absence(
                    listing_id=row.listing_id, surface="api_json", field_="coordinate",
                    reason="not_attempted", extraction_method="portal_structured_field",
                    detail="sreality locality object absent (payload truncation)"))
    return result


# ------------------------------------------------------------------ SQL

_REGCLASS_SQL = "SELECT to_regclass(%(name)s)"
_MAPY_COUNT_SQL = "SELECT count(*) FROM mapy_affected"

# The inventory is only a W1 INPUT once it is TERMINAL AND COMPLETE. `count(*) > 0` is
# the wrong gate: the inventory job is batched and resumable, so a run that stopped at
# its budget leaves a perfectly non-empty table describing a PREFIX of `listings` — and
# every listing past that prefix would then be read as "absent from the inventory", which
# is exactly the verdict that admits a carry_forward coordinate as first-party. The
# question this asks is migration 385's own completeness contract, verbatim: inside the
# CURRENT restart epoch (a `--restart` opens a new one and the older epoch's completion
# says nothing about it), is there a run that finished the whole table without an
# operator-chosen start anchor?
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
    "location_claims", "location_claim_observations", "location_claim_absences",
    "location_claim_batches", "location_enrichment_state", "dirty_locations",
    "portal_contracts", "portal_contract_entries", "mapy_affected",
    "mapy_inventory_runs",
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
           pce.default_licence_class::text, pce.cardinality, pce.guards
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

# `outcome = 'ok'` is now load-bearing and narrow: migration 387 splits the terminal
# states so that 'ok' means "the scan ran out of rows", never "the scan ran out of
# budget". A budget-stopped run stamps 'stopped' and is INVISIBLE here, so the
# incremental floor stays where it was and the rows it never opened are still in the
# next run's window. (The failure this closes: a 30k-row budgeted run over a 650k-row
# table used to move the floor past 620k unscanned rows — permanently, for the ~270k
# delisted ones whose `last_seen_at` will never move again.)
#
# `coverage_since`, not `started_at`: for a chain of budgeted runs the completing run
# began long after the scan did, and the claim the watermark makes — "everything written
# before this instant has been mined" — is only true back to the FIRST run's start.
# For an unresumed run the two are the same value.
_WATERMARK_SQL = """
    SELECT max(coalesce(coverage_since, started_at))
    FROM location_claim_batches
    WHERE lane = %(lane)s AND outcome = 'ok' AND source IS NOT DISTINCT FROM %(source)s
"""

# The resume point: the newest TERMINAL batch of this (lane, source, scan_mode) among the
# resumable ones. `outcome` comes back with it rather than being filtered on, because
# "the last full pass finished" and "there has never been a full pass" must not look the
# same to the caller — only a 'stopped' row is resumed from, and an 'ok' row means the
# next pass legitimately starts over at the beginning of the range.
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

# Keyset over the whole table (active AND inactive: a delisted row's payload is exactly the
# evidence the history waves need, and nothing is ever deleted).
_LISTINGS_FULL_SQL = """
    SELECT l.id, l.source, l.source_id_native, l.raw_json, l.last_seen_at,
           ST_Y(l.geom::geometry), ST_X(l.geom::geometry),
           (a.listing_id IS NOT NULL), l.locality, l.street, l.street_source
    FROM listings l
    LEFT JOIN mapy_affected a ON a.listing_id = l.id
    WHERE l.id > %(after_id)s
      AND (%(source)s::text IS NULL OR l.source = %(source)s)
    ORDER BY l.id
    LIMIT %(batch_size)s
"""

_LISTINGS_INCREMENTAL_SQL = """
    SELECT l.id, l.source, l.source_id_native, l.raw_json, l.last_seen_at,
           ST_Y(l.geom::geometry), ST_X(l.geom::geometry),
           (a.listing_id IS NOT NULL), l.locality, l.street, l.street_source
    FROM listings l
    LEFT JOIN mapy_affected a ON a.listing_id = l.id
    WHERE l.last_seen_at >= %(watermark)s
      AND (l.last_seen_at, l.id) > (%(after_ts)s, %(after_id)s)
      AND (%(source)s::text IS NULL OR l.source = %(source)s)
    ORDER BY l.last_seen_at, l.id
    LIMIT %(batch_size)s
"""

# One statement, so the claim insert, the re-sight observations and the dirty_locations
# enqueue are atomic together (03 §3.2: the enqueue happens INSIDE the claim-insert
# transaction; it is the only coupling between intake and resolution).
#
# claim_fingerprint is computed in SQL, deliberately, and by a NAMED FUNCTION rather than
# an expression pasted here. Two reasons, and both are about an append-only table whose
# only dedup mechanism is a UNIQUE index over this value:
#
#   * Not Python. `value_norm` is written by `location_value_norm()` (migration 382),
#     which is `lower(unaccent(...))` — and PostgreSQL's `unaccent` dictionary is NOT
#     Python's NFKD combining-mark strip (it additionally expands ß→ss, ø→o, đ→d, ł→l …).
#     A Python mirror would drift on exactly the foreign-address cohort this program
#     exists to detect, and a drifted fingerprint does not conflict — it inserts.
#   * Not inline. W1 intake is the FIRST claim producer; W2's HTML re-mine, W3's snapshot
#     backfill and the LLM lane are the next three. A 22-element tuple transcribed four
#     times is four chances to lose a byte. `location_claim_fingerprint()` (migration 386)
#     is the one definition all of them call.
#
# The tuple is 01 §4.2.1's, in its order, and is TIME-FREE.
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
            history_completeness text, subject_scoped boolean)
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
            listing_id, source, source_id_native, snapshot_id, snapshot_anchor,
            first_observed_at, claim_type, surface, page_kind, extraction_method,
            extractor_id, extractor_version, contract_entry_id, batch_id, value_text,
            value_norm, value_num, value_geom, value_shape, value_jsonb, distance_m,
            travel_mode, target_text, declared_precision_label, declared_confidence,
            declared_radius_m, claim_confidence, blur_evidence, licence_class,
            legacy_source_column, legacy_write_path_unknown, history_completeness,
            subject_scoped, claim_fingerprint)
        SELECT d.listing_id, d.source, d.source_id_native, d.snapshot_id,
               d.snapshot_anchor, d.first_observed_at, d.claim_type::location_claim_type,
               d.surface::location_claim_surface, d.page_kind::location_page_kind,
               d.extraction_method::location_extraction_method, d.extractor_id,
               d.extractor_version, d.contract_entry_id, %(batch_id)s, d.value_text,
               d.value_norm, d.value_num, d.geom, d.shape, d.value_jsonb, d.distance_m,
               d.travel_mode, d.target_text, d.declared_precision_label,
               d.declared_confidence, d.declared_radius_m,
               d.claim_confidence::match_confidence,
               d.blur_evidence::blur_evidence, d.licence_class::licence_class,
               d.legacy_source_column, d.legacy_write_path_unknown,
               d.history_completeness, d.subject_scoped, d.claim_fingerprint
        FROM deduped d
        ON CONFLICT (claim_fingerprint) DO NOTHING
        RETURNING id, listing_id
    ), resighted AS (
        -- The statement snapshot cannot see `ins`, so this join is exactly the set of
        -- claims that already existed: the re-sight cohort. `snapshot_id` rides along so
        -- a W3 re-sighting of an already-known value still names WHICH snapshot re-observed
        -- it (lco_snapshot, migration 382) — NULL for a W1 re-sighting, exactly as before.
        SELECT c.id, d.first_observed_at, d.snapshot_id, d.extractor_version
        FROM deduped d
        JOIN location_claims c ON c.claim_fingerprint = d.claim_fingerprint
    ), obs AS (
        INSERT INTO location_claim_observations
            (claim_id, observed_at, snapshot_id, extractor_version)
        SELECT r.id, r.first_observed_at, r.snapshot_id, r.extractor_version
        FROM resighted r
        WHERE NOT EXISTS (
            SELECT 1 FROM location_claim_observations o
            WHERE o.claim_id = r.id AND o.observed_at = r.first_observed_at)
        RETURNING claim_id
    ), enqueued AS (
        INSERT INTO dirty_locations (listing_id, reason)
        SELECT DISTINCT listing_id, 'claim_insert' FROM ins
        ON CONFLICT (listing_id) DO NOTHING
        RETURNING listing_id
    )
    SELECT (SELECT count(*) FROM ins), (SELECT count(*) FROM obs),
           (SELECT count(*) FROM enqueued)
"""

# snapshot_id is NULL in the W1 lane (the substrate is latest-wins `listings.raw_json`,
# 00 §3.3), so its generated `snapshot_key` is -1. W3 (`location_data.claims_remine`) sets
# it, so the SAME (listing, surface, field) withheld at two different snapshots is two
# rows, not one collapsed by the unique index — it is named explicitly in the conflict
# target because that is the column migration 382's unique key actually carries.
#
# ON CONFLICT, not NOT EXISTS, and the difference is a whole aborted run. A statement's
# snapshot cannot see rows the SAME statement is inserting, so a NOT EXISTS anti-join
# arbitrates against the table as it was BEFORE the insert and lets two identical rows in
# one batch through to the unique index — where the second one raises and takes the entire
# intake run with it. That is not hypothetical: one sreality listing that is in
# `mapy_affected` AND has a truncated payload produces the withheld-coordinate absence and
# the missing-locality absence with the same (listing_id, surface, field) key. The Python
# dedupe in `write_result` collapses those; ON CONFLICT is the second rail, for the
# cross-run case the anti-join used to cover.
_ABSENCE_WRITE_SQL = """
    INSERT INTO location_claim_absences
        (listing_id, snapshot_id, surface, field, reason, extraction_method,
         extractor_version, surfaces_seen)
    SELECT i.listing_id, i.snapshot_id, i.surface::location_claim_surface,
           i.field::location_claim_type, i.reason,
           i.extraction_method::location_extraction_method, i.extractor_version,
           '{}'::location_claim_surface[]
    FROM jsonb_to_recordset(%(rows)s::jsonb) AS i(
        listing_id bigint, snapshot_id bigint, surface text, field text, reason text,
        extraction_method text, extractor_version text)
    ON CONFLICT (listing_id, snapshot_key, surface, field, extractor_version)
    DO NOTHING
"""

# `input_hash` is the cost gate (01 §9): attempts only advance when the payload actually
# changed, so an hourly re-run over an unchanged legacy-shape cohort is a no-op.
_ENRICHMENT_WRITE_SQL = """
    INSERT INTO location_enrichment_state
        (listing_id, method, lane, attempts, last_attempt_at, last_outcome, last_error,
         input_hash, extractor_version, next_eligible_at)
    SELECT i.listing_id, i.method::location_extraction_method, i.lane, 1, now(),
           i.last_outcome, i.last_error, decode(i.input_hash, 'hex'), i.extractor_version,
           now() + interval '6 hours'
    FROM jsonb_to_recordset(%(rows)s::jsonb) AS i(
        listing_id bigint, method text, lane text, last_outcome text, last_error text,
        input_hash text, extractor_version text)
    ON CONFLICT (listing_id, method, lane) DO UPDATE
    SET attempts = location_enrichment_state.attempts + 1,
        last_attempt_at = now(),
        last_outcome = EXCLUDED.last_outcome,
        last_error = EXCLUDED.last_error,
        input_hash = EXCLUDED.input_hash,
        extractor_version = EXCLUDED.extractor_version,
        next_eligible_at = EXCLUDED.next_eligible_at
    WHERE location_enrichment_state.input_hash IS DISTINCT FROM EXCLUDED.input_hash
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
    """06 §6.1.2: the C7.2 R2 inventory is a W1 INPUT. Without it, `carry_forward` cannot
    be classified and the W1 licence gate cannot be met, so the lane refuses to run.

    "Without it" means TERMINAL AND COMPLETE, not merely non-empty. The inventory job is
    batched and resumable: a run that stopped at its `--limit`/`--max-seconds` budget
    leaves a populated table that describes a PREFIX of `listings`, and absence from a
    prefix is indistinguishable from absence from the inventory — which is the exact
    verdict that admits a Mapy-derived `carry_forward` coordinate as first-party. So the
    gate is migration 385's own completeness contract: in the CURRENT restart epoch, a
    run with `status='completed'` that was not anchored at an operator-chosen listing_id
    (`resumable`)."""
    with conn.cursor() as cur:
        cur.execute(_REGCLASS_SQL, {"name": "mapy_affected"})
        if cur.fetchone()[0] is None:
            raise IntakeRefused(
                "mapy_affected does not exist: migration 385 is not applied. The Mapy "
                "affected-set inventory is a W1 INPUT (06 §6.1.2) — run "
                "`python -m scripts.location_mapy_inventory` first.")
        cur.execute(_MAPY_COUNT_SQL)
        count = int(cur.fetchone()[0])
        cur.execute(_INVENTORY_TERMINAL_SQL)
        run_count, epoch, complete, statuses = cur.fetchone()
    if count == 0:
        raise IntakeRefused(
            "mapy_affected is empty: the Mapy affected-set inventory has not been "
            "materialised. Every carry_forward coordinate would be admitted as "
            "first-party and the W1 licence gate would fail (06 §6.1.2). Run "
            "`python -m scripts.location_mapy_inventory` to completion first.")
    if not int(run_count or 0):
        raise IntakeRefused(
            "mapy_inventory_runs has no rows: mapy_affected holds data no run "
            "accounted for, so its completeness cannot be established. The inventory "
            "is a W1 INPUT (06 §6.1.2) — run "
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
                cardinality=row[15], guards=tuple(row[16] or ()))
            by_source.setdefault(entry.source, []).append(entry)
    return by_source


def _row_from_record(record: tuple[Any, ...]) -> ListingRow:
    # The legacy columns are unpacked as a TAIL and zipped `strict`, so a column added to
    # the two batch queries but not to `LEGACY_COLUMNS` (or the reverse) raises here on the
    # first row instead of shifting every value one position to the left.
    (listing_id, source, native, raw_json, last_seen_at, lat, lon, in_inventory,
     *legacy) = record
    return ListingRow(
        listing_id=int(listing_id),
        source=source,
        source_id_native=str(native) if native is not None else str(listing_id),
        raw_json=raw_json if isinstance(raw_json, dict) else {},
        lat=float(lat) if lat is not None else None,
        lon=float(lon) if lon is not None else None,
        # 06 §6.6 rule 1: a claim mined from `listings.raw_json` keeps the payload's own
        # observation time — the listing's last sighting — never the migration date.
        observed_at=last_seen_at,
        in_mapy_inventory=bool(in_inventory),
        legacy_columns=dict(zip(LEGACY_COLUMNS, legacy, strict=True)))


def dedupe_absence_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse absence rows onto migration 382's unique key, first-writer-wins.

    `location_claim_absences` is UNIQUE on (listing_id, snapshot_key, surface, field,
    extractor_version); `snapshot_id` is part of the key here (constant at NULL in this
    lane, so `snapshot_key` is constant at -1) because W3 (`location_data.claims_remine`)
    passes a real one and a batch there routinely holds several snapshots of ONE listing —
    without it in the key, this Python-level pre-DB collapse would silently drop distinct
    per-snapshot absence rows the unique index would have kept. Two absences for one
    listing+snapshot on the same surface+field are still the same row: a sreality listing
    that is in `mapy_affected` AND lost its `locality` object to the 80 KB truncation emits
    the withheld-coordinate absence and the missing-locality absence, both
    `(api_json, coordinate)`. `reason` is intentionally NOT in the key — the first
    assertion made about a (listing, snapshot, surface, field) is the one kept, and keeping
    both was never possible."""
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row["listing_id"], row.get("snapshot_id"), row["surface"], row["field"],
               row["extractor_version"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def dedupe_enrichment_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse refetch-cohort rows onto migration 384's primary key, first-writer-wins.

    `location_enrichment_state` is `PRIMARY KEY (listing_id, method, lane)` and the write is
    `ON CONFLICT ... DO UPDATE`, which Postgres refuses to apply twice to the same row in
    one statement ("cannot affect row a second time") — so two rows sharing that key inside
    one array is not a duplicate, it is an aborted run. One listing produces them: a
    legacy-shape sreality row routes to `sreality_detail_refetch` for its SHAPE and, if one
    of its geometry values is over the cap, again for the REFUSAL — same method, same lane.
    `_refuse_oversized` runs first precisely so the surviving row is the one whose
    `last_error` names the refusal.
    """
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row["listing_id"], row["method"], row["lane"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def chunk_rows(
    rows: list[dict[str, Any]], *, max_rows: int, max_bytes: int,
) -> Iterator[list[dict[str, Any]]]:
    """Split one jsonb array into statement-sized arrays, WITHOUT splitting a listing.

    Two bounds, whichever trips first — a row count (cheap, predictable) and a cumulative
    serialized-byte budget (the one that actually matters, because a batch's row count says
    nothing about its bytes: legacy-shape sreality rows carry geometry an order of magnitude
    larger than a street name).

    The listing boundary is what makes chunking SEMANTICS-PRESERVING, not merely smaller.
    `claim_fingerprint` is computed in SQL and its tuple (01 §4.2.1) begins with
    `listing_id, source, source_id_native`, so two fingerprint-equal claims are necessarily
    the same listing's. Keeping a listing's rows in one array therefore keeps every
    fingerprint-equal set inside ONE statement, where `DISTINCT ON (claim_fingerprint)`
    still arbitrates it. Split them across two statements instead and the second copy stops
    being a same-statement duplicate: the first chunk's INSERT is visible to the second
    chunk's snapshot, so it would join the `resighted` cohort and append a spurious
    "re-sighting" observation for a claim this batch had just created. `extract_listing`
    appends per listing, so a listing's rows are contiguous and grouping is a single pass.

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


def write_result(
    cur: psycopg.Cursor, result: IntakeResult, *, batch_id: int,
    extractor_version: str = INTAKE_VERSION,
) -> tuple[int, int, int]:
    """Write one scan batch. Same transaction as before; several statements per array.

    The caller's transaction is unchanged — every chunk of every array is flushed inside it,
    so the batch is still all-or-nothing and a failure still rolls the whole batch back.

    `extractor_version` stamps the ABSENCE/ENRICHMENT rows only (a claim already carries
    its own, from the contract entry that produced it — 06 §6.6 Rule 3). It defaults to
    this module's own `INTAKE_VERSION` so every existing W1 call site is unchanged;
    `location_data.claims_remine` (W3) passes its own, so a re-mined absence is never
    misattributed to the W1 lane that never saw the snapshot it came from.
    """
    max_rows = env_positive_int(WRITE_CHUNK_ROWS_ENV, DEFAULT_WRITE_CHUNK_ROWS)
    max_bytes = env_positive_int(WRITE_CHUNK_BYTES_ENV, DEFAULT_WRITE_CHUNK_BYTES)
    inserted = observed = enqueued = 0
    claim_rows = [c.to_row() for c in result.claims]
    for chunk in chunk_rows(claim_rows, max_rows=max_rows, max_bytes=max_bytes):
        cur.execute(_CLAIM_WRITE_SQL, {"rows": Jsonb(chunk), "batch_id": batch_id})
        chunk_inserted, chunk_observed, chunk_enqueued = (int(x) for x in cur.fetchone())
        inserted += chunk_inserted
        observed += chunk_observed
        enqueued += chunk_enqueued
    absence_rows = dedupe_absence_rows(
        [a.to_row(extractor_version) for a in result.absences])
    for chunk in chunk_rows(absence_rows, max_rows=max_rows, max_bytes=max_bytes):
        cur.execute(_ABSENCE_WRITE_SQL, {"rows": Jsonb(chunk)})
    enrichment_rows = dedupe_enrichment_rows(
        [e.to_row(extractor_version) for e in result.enrichment])
    for chunk in chunk_rows(enrichment_rows, max_rows=max_rows, max_bytes=max_bytes):
        cur.execute(_ENRICHMENT_WRITE_SQL, {"rows": Jsonb(chunk)})
    return inserted, observed, enqueued


def _resume_point(
    conn: psycopg.Connection, *, mode: str, source: str | None, watermark: datetime | None,
) -> dict[str, Any] | None:
    """Where this scan should pick up, or None to start at the beginning of its range.

    Only a 'stopped' predecessor is resumed from, and only one written by the SAME
    `scan_mode`: a full cursor is a bare `listings.id` and an incremental one is
    `(last_seen_at, id)`, so crossing them would skip an arbitrary slice.

    In incremental mode the stored `(after_ts, after_id)` is only usable when it sits at
    or after the floor this run computed. It normally does — a stopped run does not move
    the watermark, so the next run recomputes the SAME floor and the stopped run's cursor
    is exactly how far into that identical window it got. `--overlap-hours` changing
    between runs is the case where it doesn't, and there the floor wins."""
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
            f"(migrations 380-387)")
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
    unknown = sorted(
        f"{e.entry_id}:{e.reader}"
        for s in wanted for e in entries_by_source[s] if e.reader and e.reader not in READERS)
    if unknown:
        raise IntakeRefused(
            f"active contract declares readers this extractor does not implement: "
            f"{', '.join(unknown)}")

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
    LOG.info("INTAKE start mode=%s source=%s batch=%d inventory_rows=%d batch_id=%s",
             mode, source or "*", batch_size, inventory_rows, batch_id)

    started = time.monotonic()
    # Resolved once, not per listing: the extractor is called 20 000 times a batch.
    max_value_bytes = env_positive_int(MAX_CLAIM_VALUE_BYTES_ENV,
                                       DEFAULT_MAX_CLAIM_VALUE_BYTES)
    stats = {
        "listings": 0, "claims": 0, "claims_inserted": 0, "observations": 0,
        "enqueued": 0, "absences": 0, "refetch_cohort": 0, "oversized_values": 0,
        "stopped_early": False, "reached_end": False, "resumed_from_id": after_id,
    }
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
                for record in records:
                    row = _row_from_record(record)
                    entries = entries_by_source.get(row.source)
                    if not entries:
                        continue
                    result.extend(extract_listing(
                        row, entries, max_value_bytes=max_value_bytes))

                after_id = int(records[-1][0])
                if mode == "incremental":
                    after_ts = records[-1][4]
                stats["listings"] += len(records)
                stats["claims"] += len(result.claims)
                stats["absences"] += len(result.absences)
                stats["refetch_cohort"] += len(result.enrichment)
                stats["oversized_values"] += result.oversized
                if not dry_run and batch_id is not None:
                    inserted, observed, enqueued = write_result(
                        cur, result, batch_id=batch_id)
                    stats["claims_inserted"] += inserted
                    stats["observations"] += observed
                    stats["enqueued"] += enqueued
            LOG.info("INTAKE progress listings=%d claims=%d inserted=%d observed=%d "
                     "absences=%d refetch=%d oversized=%d through_id=%d",
                     stats["listings"], stats["claims"], stats["claims_inserted"],
                     stats["observations"], stats["absences"], stats["refetch_cohort"],
                     stats["oversized_values"], after_id)
    except Exception as exc:
        if batch_id is not None:
            # Guarded like every other write, and for a sharper reason: this is the
            # FAILURE path. Whatever broke the run may be the same pressure that would
            # hang this stamp, and a bookkeeping write that hangs replaces the exception
            # you need with a wedge (the lesson `loader_db.record_discrepancy` already
            # carries). A short ceiling here fails fast and re-raises the real cause.
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

    # 'ok' means ONE thing: the scan ran out of rows. A run that ran out of budget
    # instead stamps 'stopped', which `_WATERMARK_SQL` does not see — so the incremental
    # floor stays where it was and everything behind the cursor is still in the next
    # run's window. The cursor rides on the row either way, so the next same-mode run
    # picks up exactly where this one left off instead of re-walking the same prefix.
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
