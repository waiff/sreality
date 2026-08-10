"""W1 claim intake — deterministic extraction of location claims from `listings.raw_json`.

Design: 06-migration-backfill.md §6.2.1 (the raw_json substrate — cheapest, richest,
first; all nine sources), §6.1.2 (the coordinate-provenance ladder, a filter on the INPUT
applied before anything is written), §6.6 rules 1/3/6/7 (observation timestamps, legacy
anchoring, no claim for a non-storable signal, blur written never defaulted),
03-resolution-pipeline.md §3.2 (the S0 admission contract and the dirty_locations
side effect), 02-portal-contracts.md §2.2 (what each portal publishes).

WHAT THIS LANE IS
  * Pure deterministic extraction. No model, no network, no re-fetch.
  * Contract-driven: every claim is stamped with the `portal_contract_entries` row that
    produced it, and the extractor executes exactly those entries whose `locator` names a
    `reader` from the registry below. Entries declared for W2 surfaces (html_selector,
    map_config, url_slug, og_meta, jsonld, description) carry no reader and are inert here.
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
Required: SUPABASE_DB_URL. Additionally requires migrations 380-385 and a projected,
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

from scraper import db

LOG = logging.getLogger("location_data.claims_intake")

# Bumped whenever the extraction SEMANTICS change. It rides in every claim's batch row and
# in `location_claim_observations.extractor_version`; the per-claim `extractor_version` is
# the contract's own `contract:<portal>@<version>` (02 §2.1.8).
INTAKE_VERSION = "claims_intake@1"
LANE = "location_claims_intake"
WAVE = "W1"

SOURCES = (
    "sreality", "bezrealitky", "bazos", "idnes", "mmreality", "remax", "ceskereality",
    "realitymix", "maxima",
)

MIN_BATCH_SIZE = 10_000
MAX_BATCH_SIZE = 30_000
DEFAULT_BATCH_SIZE = 20_000
# Incremental runs re-read a window behind the last successful batch: a listing written
# while the previous run was mid-flight would otherwise fall between the two watermarks.
# Re-reading is free — values dedupe on the fingerprint and a re-sight appends at most one
# observation per (claim, observed_at).
DEFAULT_OVERLAP_HOURS = 3

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

    def to_row(self) -> dict[str, Any]:
        row = {
            "listing_id": self.listing_id,
            "source": self.source,
            "source_id_native": self.source_id_native,
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

    def to_row(self, extractor_version: str) -> dict[str, Any]:
        return {
            "listing_id": self.listing_id,
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

    def extend(self, other: IntakeResult) -> None:
        self.claims.extend(other.claims)
        self.absences.extend(other.absences)
        self.enrichment.extend(other.enrichment)


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


def apply_transforms(value: str | None, transforms: tuple[str, ...]) -> str | None:
    """The ordered normalisers a contract entry declares (02 §2.1.2)."""
    for transform in transforms:
        if value is None:
            return None
        name, _, arg = transform.partition(":")
        if name == "sentinel_drop":
            if value == arg:
                return None
        elif name == "psc_normalise":
            digits = "".join(ch for ch in value if ch.isdigit())
            value = digits if len(digits) == 5 else None
        elif name == "split_cp_co":
            # Czech `čp/čo` pairs arrive as "655/31"; the pair is not two alternatives.
            head, sep, tail = value.partition("/")
            if arg == "cp":
                value = head.strip() or None
            elif arg == "co":
                value = (tail.strip() or None) if sep else None
        elif name == "strip_prefix":
            value = value[len(arg):].strip() if value.startswith(arg) else value
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
    if "reject_outside_cz_bbox" in entry.guards and not in_cz_bbox(lat, lon):
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
    if "reject_outside_cz_bbox" in entry.guards and not in_cz_bbox(row.lat, row.lon):
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
    nowhere today. `false` is the declared-blur label; `true` is a declared-precise one and
    still writes `blur_evidence='none'` EXPLICITLY (06 §6.6 rule 7)."""
    raw = json_pointer(row.raw_json, str(entry.locator["json_pointer"]))
    if raw is None or not isinstance(raw, bool):
        return []
    labels = entry.locator.get("labels") or {"true": "accurate", "false": "not_accurate"}
    label = str(labels["true" if raw else "false"])
    blur = "declared" if not raw else "none"
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
    if "reject_outside_cz_bbox" in entry.guards and not (
            in_cz_bbox(lat_min, lon_min) and in_cz_bbox(lat_max, lon_max)):
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


# ------------------------------------------------------------------ extraction

def extract_listing(row: ListingRow, entries: list[Entry]) -> IntakeResult:
    """Everything this lane knows about one listing. Pure — no DB, no clock, no network."""
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
            # locality object outright. Both route to the refetch cohort (02 P6) instead of
            # silently producing nothing.
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

_RELATIONS = (
    "location_claims", "location_claim_observations", "location_claim_absences",
    "location_claim_batches", "location_enrichment_state", "dirty_locations",
    "portal_contracts", "portal_contract_entries", "mapy_affected",
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
        (lane, source, extractor_version, contract_id, wave, job_run_id, outcome, note)
    VALUES (%(lane)s, %(source)s, %(extractor_version)s, %(contract_id)s, %(wave)s,
            %(job_run_id)s, 'running', %(note)s)
    RETURNING id
"""

_BATCH_FINISH_SQL = """
    UPDATE location_claim_batches
    SET finished_at = now(), outcome = %(outcome)s, row_count = %(row_count)s,
        note = concat_ws(' | ', note, %(note)s::text)
    WHERE id = %(batch_id)s
"""

_WATERMARK_SQL = """
    SELECT max(started_at)
    FROM location_claim_batches
    WHERE lane = %(lane)s AND outcome = 'ok' AND source IS NOT DISTINCT FROM %(source)s
"""

# Keyset over the whole table (active AND inactive: a delisted row's payload is exactly the
# evidence the history waves need, and nothing is ever deleted).
_LISTINGS_FULL_SQL = """
    SELECT l.id, l.source, l.source_id_native, l.raw_json, l.last_seen_at,
           ST_Y(l.geom::geometry), ST_X(l.geom::geometry),
           (a.listing_id IS NOT NULL)
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
           (a.listing_id IS NOT NULL)
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
# claim_fingerprint is computed HERE, in SQL, deliberately. `value_norm` is written by
# `location_value_norm()` (migration 382), which is `lower(unaccent(...))` — and
# PostgreSQL's `unaccent` dictionary is NOT the same function as Python's NFKD
# combining-mark strip (it additionally expands ß→ss, ø→o, đ→d, ł→l …). A Python mirror
# would therefore drift on exactly the foreign-address cohort this program exists to
# detect, and drift in the fingerprint means the unique index stops deduping: silent
# duplicate claims in an append-only table. One definition, on the side that owns the
# normalization. The tuple is 01 §4.2.1's, in its order, and is TIME-FREE.
_CLAIM_FINGERPRINT_SQL = """
    sha256(convert_to(jsonb_build_array(
        t.listing_id, t.source, t.source_id_native,
        t.claim_type, t.surface, t.page_kind, t.extraction_method,
        t.extractor_id, t.extractor_version, t.contract_entry_id,
        coalesce(t.value_norm, t.value_text, ''),
        t.value_num, encode(ST_AsEWKB(t.geom), 'hex'), encode(ST_AsEWKB(t.shape), 'hex'),
        t.value_jsonb, t.distance_m, t.travel_mode, t.target_text,
        t.declared_precision_label, t.declared_confidence, t.declared_radius_m,
        t.legacy_source_column
    )::text, 'UTF8'))
"""

_CLAIM_WRITE_SQL = f"""
    WITH input AS (
        SELECT * FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
            listing_id bigint, source text, source_id_native text,
            snapshot_anchor text, first_observed_at timestamptz,
            claim_type text, surface text, page_kind text, extraction_method text,
            extractor_id text, extractor_version text, contract_entry_id bigint,
            value_text text, value_num numeric, value_geom_wkt text, value_shape_wkt text,
            value_jsonb jsonb, distance_m integer, travel_mode text, target_text text,
            declared_precision_label text, declared_confidence text,
            declared_radius_m numeric, blur_evidence text, licence_class text,
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
            listing_id, source, source_id_native, snapshot_anchor, first_observed_at,
            claim_type, surface, page_kind, extraction_method, extractor_id,
            extractor_version, contract_entry_id, batch_id, value_text, value_norm,
            value_num, value_geom, value_shape, value_jsonb, distance_m, travel_mode,
            target_text, declared_precision_label, declared_confidence, declared_radius_m,
            blur_evidence, licence_class, legacy_source_column, legacy_write_path_unknown,
            history_completeness, subject_scoped, claim_fingerprint)
        SELECT d.listing_id, d.source, d.source_id_native, d.snapshot_anchor,
               d.first_observed_at, d.claim_type::location_claim_type,
               d.surface::location_claim_surface, d.page_kind::location_page_kind,
               d.extraction_method::location_extraction_method, d.extractor_id,
               d.extractor_version, d.contract_entry_id, %(batch_id)s, d.value_text,
               d.value_norm, d.value_num, d.geom, d.shape, d.value_jsonb, d.distance_m,
               d.travel_mode, d.target_text, d.declared_precision_label,
               d.declared_confidence, d.declared_radius_m,
               d.blur_evidence::blur_evidence, d.licence_class::licence_class,
               d.legacy_source_column, d.legacy_write_path_unknown,
               d.history_completeness, d.subject_scoped, d.claim_fingerprint
        FROM deduped d
        ON CONFLICT (claim_fingerprint) DO NOTHING
        RETURNING id, listing_id
    ), resighted AS (
        -- The statement snapshot cannot see `ins`, so this join is exactly the set of
        -- claims that already existed: the re-sight cohort.
        SELECT c.id, d.first_observed_at, d.extractor_version
        FROM deduped d
        JOIN location_claims c ON c.claim_fingerprint = d.claim_fingerprint
    ), obs AS (
        INSERT INTO location_claim_observations
            (claim_id, observed_at, extractor_version)
        SELECT r.id, r.first_observed_at, r.extractor_version
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

# snapshot_id is always NULL in this lane (the substrate is latest-wins `listings.raw_json`,
# 00 §3.3), so the generated `snapshot_key` is always -1; the NOT EXISTS spells that out
# rather than relying on ON CONFLICT inference over a generated column.
_ABSENCE_WRITE_SQL = """
    INSERT INTO location_claim_absences
        (listing_id, snapshot_id, surface, field, reason, extraction_method,
         extractor_version, surfaces_seen)
    SELECT i.listing_id, NULL, i.surface::location_claim_surface,
           i.field::location_claim_type, i.reason,
           i.extraction_method::location_extraction_method, i.extractor_version,
           '{}'::location_claim_surface[]
    FROM jsonb_to_recordset(%(rows)s::jsonb) AS i(
        listing_id bigint, surface text, field text, reason text,
        extraction_method text, extractor_version text)
    WHERE NOT EXISTS (
        SELECT 1 FROM location_claim_absences a
        WHERE a.listing_id = i.listing_id
          AND a.snapshot_key = -1
          AND a.surface = i.surface::location_claim_surface
          AND a.field = i.field::location_claim_type
          AND a.extractor_version = i.extractor_version)
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
    be classified and the W1 licence gate cannot be met, so the lane refuses to run."""
    with conn.cursor() as cur:
        cur.execute(_REGCLASS_SQL, {"name": "mapy_affected"})
        if cur.fetchone()[0] is None:
            raise IntakeRefused(
                "mapy_affected does not exist: migration 385 is not applied. The Mapy "
                "affected-set inventory is a W1 INPUT (06 §6.1.2) — run "
                "`python -m scripts.location_mapy_inventory` first.")
        cur.execute(_MAPY_COUNT_SQL)
        count = int(cur.fetchone()[0])
    if count == 0:
        raise IntakeRefused(
            "mapy_affected is empty: the Mapy affected-set inventory has not been "
            "materialised. Every carry_forward coordinate would be admitted as "
            "first-party and the W1 licence gate would fail (06 §6.1.2). Run "
            "`python -m scripts.location_mapy_inventory` to completion first.")
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
    listing_id, source, native, raw_json, last_seen_at, lat, lon, in_inventory = record
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
        in_mapy_inventory=bool(in_inventory))


def write_result(
    cur: psycopg.Cursor, result: IntakeResult, *, batch_id: int,
) -> tuple[int, int, int]:
    inserted = observed = enqueued = 0
    if result.claims:
        cur.execute(_CLAIM_WRITE_SQL, {
            "rows": Jsonb([c.to_row() for c in result.claims]),
            "batch_id": batch_id,
        })
        inserted, observed, enqueued = (int(x) for x in cur.fetchone())
    if result.absences:
        cur.execute(_ABSENCE_WRITE_SQL, {
            "rows": Jsonb([a.to_row(INTAKE_VERSION) for a in result.absences]),
        })
    if result.enrichment:
        cur.execute(_ENRICHMENT_WRITE_SQL, {
            "rows": Jsonb([e.to_row(INTAKE_VERSION) for e in result.enrichment]),
        })
    return inserted, observed, enqueued


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
            f"(migrations 380-385)")
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

    contract_id: int | None = None
    if source:
        with conn.cursor() as cur:
            cur.execute(_ACTIVE_CONTRACT_SQL, {"source": source})
            row = cur.fetchone()
            contract_id = int(row[0]) if row else None

    watermark: datetime | None = None
    if mode == "incremental":
        with conn.cursor() as cur:
            cur.execute(_WATERMARK_SQL, {"lane": LANE, "source": source})
            row = cur.fetchone()
        watermark = row[0] - timedelta(hours=overlap_hours) if row and row[0] else None
        if watermark is None:
            LOG.info("INTAKE no prior successful batch for source=%s; "
                     "incremental degrades to a full pass", source or "*")
            mode = "full"

    batch_id: int | None = None
    if not dry_run:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_BATCH_INSERT_SQL, {
                "lane": LANE, "source": source, "extractor_version": INTAKE_VERSION,
                "contract_id": contract_id, "wave": WAVE,
                "job_run_id": os.environ.get("GITHUB_RUN_ID"), "note": note,
            })
            batch_id = int(cur.fetchone()[0])
    LOG.info("INTAKE start mode=%s source=%s batch=%d inventory_rows=%d batch_id=%s",
             mode, source or "*", batch_size, inventory_rows, batch_id)

    started = time.monotonic()
    after_id = start_after_id
    after_ts = watermark
    stats = {
        "listings": 0, "claims": 0, "claims_inserted": 0, "observations": 0,
        "enqueued": 0, "absences": 0, "refetch_cohort": 0, "stopped_early": False,
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
                    break

                result = IntakeResult()
                for record in records:
                    row = _row_from_record(record)
                    entries = entries_by_source.get(row.source)
                    if not entries:
                        continue
                    result.extend(extract_listing(row, entries))

                after_id = int(records[-1][0])
                if mode == "incremental":
                    after_ts = records[-1][4]
                stats["listings"] += len(records)
                stats["claims"] += len(result.claims)
                stats["absences"] += len(result.absences)
                stats["refetch_cohort"] += len(result.enrichment)
                if not dry_run and batch_id is not None:
                    inserted, observed, enqueued = write_result(
                        cur, result, batch_id=batch_id)
                    stats["claims_inserted"] += inserted
                    stats["observations"] += observed
                    stats["enqueued"] += enqueued
            LOG.info("INTAKE progress listings=%d claims=%d inserted=%d observed=%d "
                     "absences=%d refetch=%d through_id=%d",
                     stats["listings"], stats["claims"], stats["claims_inserted"],
                     stats["observations"], stats["absences"], stats["refetch_cohort"],
                     after_id)
    except Exception as exc:
        if batch_id is not None:
            with conn.cursor() as cur:
                cur.execute(_BATCH_FINISH_SQL, {
                    "batch_id": batch_id, "outcome": "failed",
                    "row_count": stats["claims_inserted"],
                    "note": f"{type(exc).__name__}: {exc}"[:500],
                })
        raise

    if batch_id is not None:
        with guarded(conn, statement_timeout) as cur:
            cur.execute(_BATCH_FINISH_SQL, {
                "batch_id": batch_id,
                # A run stopped by its own budget is still `ok`: its watermark is honest,
                # and the next run resumes from it.
                "outcome": "ok",
                "row_count": stats["claims_inserted"],
                "note": f"listings={stats['listings']} stopped_early={stats['stopped_early']}",
            })
    stats["batch_id"] = batch_id
    stats["mode"] = mode
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
    parser.add_argument("--statement-timeout", type=int, default=600)
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
