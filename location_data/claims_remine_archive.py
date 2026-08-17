"""W2 archived-HTML re-mining — location claims from `portal_raw_payloads` bodies.

Design: 06-migration-backfill.md §6.2.3 (the archived substrate + the exclusion-zone
security boundary), §6.6 rules 1/2/5/6/7 (observation time, fetch anchoring, deterministic
ordering, no claim for a non-storable signal, blur written never defaulted),
01-schema.md §4.2 (`loc_claim_text_evidence`, `loc_claim_evidence_payload`), §4.3
(observations), §4.4 (`snapshot_key`), §4.7 (`batch_id` is mandatory for a bulk lane),
00-shared-contracts.md §3.3 (the canonical `snapshot_anchor` vocabulary — the tie-breaker).
This module restates no DDL: every column it writes ships in migration 382.

NAMING — READ THIS BEFORE ADDING A SIBLING LANE
  Two waves designed a re-mine lane independently and both drafts called it
  `claims_remine.py` with `LANE = "location_claims_remine"`. They re-mine DIFFERENT
  substrates: W3's reads `listing_snapshots.raw_json`; this one reads archived page
  bodies. `location_claim_batches` resume/watermark is keyed on
  `(lane, source, scan_mode)`, so one shared lane string would have had the two cursors
  overwriting each other the first time both ran — a silent coverage hole, not a crash.
  Everything here is therefore disambiguated up front: module `claims_remine_archive`,
  `LANE = "location_claims_remine_archive"`, `REMINE_VERSION = "claims_remine_archive@1"`,
  leaving the short names free for W3 whenever it lands. The dispatch workflow W2-13 will
  add takes the same treatment — `location_claims_remine_archive.yml`, inner concurrency
  group `location-remine-archive` — so the collision is not recreated one layer out.
  `tests/location_data/test_lane_identifiers.py` is the standing gate.

WHAT THIS LANE IS
  * Re-derive, not re-invent. It reuses W1's `Entry` / `ListingRow` / `Claim` / `Absence`
    shapes, `_base`'s stamping, the licence ladder, the chunking/dedup helpers and the
    claim/absence write SQL wholesale. What it OWNS is (a) the scan over
    `portal_raw_payloads` — latest body per `(source, source_id_native, page_kind)` — (b)
    the archived-body stamping (C9/C10/C4 below), (c) the D7 evidence discipline the
    archived substrate is the first to need at all, and (d) the archived arm of the
    coordinate ladder.
  * INERT ON MERGE, and structurally so: an entry runs here only when its locator names a
    reader from `ARCHIVE_READERS`, which is EMPTY. No portal has an archived reader until
    W2-6…W2-12 land one each. A run therefore returns before it opens a batch row (see
    `run()`) rather than stamping 'ok' over a corpus it never mined — a batch stamped 'ok'
    moves the incremental watermark, and a watermark is a claim of coverage.
  * `ARCHIVE_READERS` is deliberately NOT `claims_intake.READERS`. The W1 registry's
    readers address `raw_json` by JSON pointer; several archived-substrate entries name one
    of those readers (`mm.det.point` declares `reader: point_pair`) because it is the same
    shape read out of a different document. Sharing one registry would silently make this
    lane re-run W1's payload reads against `listings.raw_json` — the wrong substrate, under
    the wrong surface, on this lane's batch id.

THE THREE RULINGS THIS LANE IMPLEMENTS (BUILD-PLAN §1)
  * C9 — the contract entry keeps its published `locator_kind` (that is where the value
    LIVES: `html_selector`, `embedded_json`, `map_config`, …); the re-miner stamps
    `surface='archived_html'` at write time because that is the substrate it READ. A
    runtime mapping, not a rewrite of 70 immutable entry ids. Absence semantics follow for
    free: `location_claim_absences` is unique on `surface`, so "no street in the JSON" and
    "no street in the archived HTML" stay two different facts.
  * C10 — `page_kind` is the PAGE's own kind (`detail`/`index`/`map`). A body does not
    change what kind of page it is, so the `archive` enum member stays unused.
  * C4/06 §6.6 Rule 2 — `snapshot_anchor='unanchored_latest_fetch'`, over a NULL
    `snapshot_id`. `Claim` carries no `snapshot_id` at all today, so the column simply
    defaults; that is the only anchor `loc_claim_anchor` permits beside a NULL
    `snapshot_id`, and forcing a snapshot onto an archived body would assert an anchoring
    that pre-W2a's latest-wins archive cannot support. `payload_sha256` is a column on the
    same row, not an anchor KIND, and it is what makes a post-W2a body replayable.

EVIDENCE IS REFUSED IN PYTHON, NOT BY THE CHECK
  Migration 382's `loc_claim_text_evidence` / `loc_claim_evidence_payload` are the last
  line of defence, never the first: a batch is one transaction, so a single malformed
  `regex_text` claim would roll back every good claim beside it, and the error would name a
  constraint rather than the entry that produced it. `assert_evidence_complete` raises
  before the write, naming the extractor.

CLI:
    python -m location_data.claims_remine_archive --mode full --max-seconds 2400
    python -m location_data.claims_remine_archive --mode incremental --source remax
Required: SUPABASE_DB_URL. Requires migrations 380-389 + 403 (same as W1 plus the W2a
archive) and an ACTIVE portal contract per source. No new migration ships with this module.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import psycopg

from location_data import loader_db, payloads
from location_data.claims_intake import (
    ARCHIVED_COORDINATE_RULES,
    DEFAULT_BATCH_SIZE,
    EMITTABLE_LICENCE_CLASSES,
    LEGACY_COLUMNS,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    SOURCES,
    SUBSTRATE_ARCHIVED_HTML,
    Absence,
    Claim,
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
    coordinate_verdict,
    guarded,
    load_entries,
    missing_relations,
    write_result,
)
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from location_data.resolver import lease
from scraper import db

LOG = logging.getLogger("location_data.claims_remine_archive")

# Bumped whenever this lane's extraction SEMANTICS change. It rides in
# `location_claim_batches.extractor_version` and in every absence row; the PER-CLAIM
# `extractor_version` stays the contract's own `contract:{source}@{version}`, which is what
# keeps a value re-mined here deduping onto the same claim row W1 wrote rather than forking
# a parallel identity.
REMINE_VERSION = "claims_remine_archive@1"
LANE = "location_claims_remine_archive"
WAVE = "W2"

JOB_NAME = "location_claims_remine_archive"
CONCURRENCY_GROUP = "location-remine-archive"
DEFAULT_LEASE_TTL_S = 3600

# C9 / C10 / C4, as constants so a reader cannot spell one of them differently.
ARCHIVE_SURFACE = "archived_html"
ARCHIVE_ANCHOR = "unanchored_latest_fetch"
FORBIDDEN_PAGE_KIND = "archive"

# 06 §6.6 Rule 7: the two values a migration may write. `'detected'` / `'both'` belong to
# the collision detector, and letting the column default fire would stamp "no blur
# observed" onto exactly the rows carrying a portal blur flag.
ARCHIVE_BLUR_EVIDENCE = frozenset({"none", "declared"})

# W1 may emit `'portal'` and nothing else. This substrate adds exactly one class, and only
# because C6 rules that realitymix's absent-`data-gps` branch is a portal-republished
# Nominatim position: ODbL follows the geometry, not the republisher. It is a STORABLE
# lineage (unlike `ephemeral_display_only`, which is the class 06 §6.6 Rule 6 refuses), it
# is separable by a single predicate for the attribution query, and nothing else widens.
GEOCODED_LICENCE_CLASS = "odbl"
ARCHIVE_EMITTABLE_LICENCE_CLASSES = EMITTABLE_LICENCE_CLASSES | {GEOCODED_LICENCE_CLASS}

# 01 §4.2's `loc_claim_text_evidence` names these two methods; every other method may carry
# evidence but is not required to.
EVIDENCE_METHODS = frozenset({"llm_text", "regex_text"})

# The archived body is one fetch, not a series: pre-W2a `portal_raw_pages` was latest-wins
# (`ON CONFLICT DO UPDATE SET html`) and every body older than the last fetch is simply
# gone, and the post-W2a append-on-change store only starts accumulating from 2026-08. So
# "how much of this listing's history does this substrate carry" is honestly 'none' — the
# claim's own time series lives in `location_claim_observations`, and W1's per-source
# `HISTORY_COMPLETENESS` answers a question about a different substrate.
ARCHIVE_HISTORY_COMPLETENESS = "none"

# Every `LEGACY_COLUMNS` key present, every value NULL. `_legacy_column` REFUSES a key the
# scan never selected (a scan/contract mismatch must not read as NULL), so the mapping has
# to be complete even though no archived entry can name a legacy column: `archive_entries`
# already excludes the `legacy_column` surface, and this is the second rail.
_DUMMY_LEGACY_COLUMNS: dict[str, Any] = dict.fromkeys(LEGACY_COLUMNS, None)

STATEMENT_TIMEOUT_ENV = "LOCATION_REMINE_ARCHIVE_TIMEOUT_S"
DEFAULT_STATEMENT_TIMEOUT_S = 600
_FAILURE_STAMP_TIMEOUT_S = 30
DEFAULT_OVERLAP_HOURS = 3


@dataclass(frozen=True, slots=True)
class ArchivedPayload:
    """One `portal_raw_payloads` row: the body plus everything a claim mined from it must
    cite. `payload_sha256` is hex text all the way through — `Claim.to_row()` rides a
    `jsonb_to_recordset`, which has no bytea literal, and the write SQL decodes it."""
    id: int
    source: str
    source_id_native: str
    page_kind: str
    payload_sha256: str
    first_observed_at: datetime
    body: bytes | None = None


ArchiveReaderFn = Callable[[Entry, ListingRow, ArchivedPayload, ScopedDocument], list[Claim]]

# EMPTY until W2-6…W2-12. See the module docstring: this is what makes the lane inert, and
# it is deliberately a second registry rather than an extension of `claims_intake.READERS`.
ARCHIVE_READERS: dict[str, ArchiveReaderFn] = {}


def archive_reader(name: str) -> Callable[[ArchiveReaderFn], ArchiveReaderFn]:
    def register(fn: ArchiveReaderFn) -> ArchiveReaderFn:
        ARCHIVE_READERS[name] = fn
        return fn
    return register


# ------------------------------------------------------------------ extraction

def archive_entries(entries: list[Entry], page_kind: str) -> list[Entry]:
    """The entries this lane may execute against ONE archived body.

    Three conditions, and each excludes a different failure: the entry must name a reader
    this lane implements (not W1's registry — see the docstring), it must be declared for
    the page kind the body actually is (a detail-page selector run over an index body is
    how a neighbour's address becomes the subject's), and it must not be a `legacy_column`
    entry (those read a `listings` column, which no archived body carries)."""
    return [
        entry for entry in entries
        if entry.reader in ARCHIVE_READERS
        and entry.page_kind == page_kind
        and entry.surface != "legacy_column"
    ]


def assert_evidence_complete(claim: Claim) -> None:
    """Migration 382's two evidence CHECKs, enforced BEFORE the write.

    The CHECK must never be the first line of defence. A batch is one transaction, so one
    malformed claim rolls back every good claim beside it, and `new row violates check
    constraint "loc_claim_text_evidence"` names the constraint rather than the extractor
    that produced the row. Raising here names the entry."""
    if claim.extraction_method in EVIDENCE_METHODS:
        missing = [
            name for name, value in (
                ("evidence_quote", claim.evidence_quote),
                ("span_start", claim.span_start),
                ("span_end", claim.span_end),
                ("payload_scope_version", claim.payload_scope_version),
                ("subject_scoped", claim.subject_scoped),
            ) if value is None
        ]
        if missing:
            raise IntakeRefused(
                f"{claim.extractor_id} produced an {claim.extraction_method} claim without "
                f"{', '.join(missing)}; 01 §4.2's loc_claim_text_evidence requires the "
                f"whole set (a span is only meaningful against a named scoped document)")
        if claim.span_end <= claim.span_start:
            raise IntakeRefused(
                f"{claim.extractor_id} produced span_end={claim.span_end} <= "
                f"span_start={claim.span_start}; loc_claim_text_evidence requires "
                f"span_end > span_start")
    if claim.evidence_quote is not None and claim.payload_sha256 is None:
        raise IntakeRefused(
            f"{claim.extractor_id} produced an evidence quote with no payload_sha256; "
            f"01 §4.2's loc_claim_evidence_payload is D7's rule that a span is meaningless "
            f"without the document it indexes into")


def assert_stampable(claim: Claim) -> None:
    """The two axes 06 §6.6 rules 6 and 7 forbid this lane to default or widen."""
    if claim.blur_evidence not in ARCHIVE_BLUR_EVIDENCE:
        raise IntakeRefused(
            f"{claim.extractor_id} produced blur_evidence='{claim.blur_evidence}'; a "
            f"migration writes only {sorted(ARCHIVE_BLUR_EVIDENCE)} (06 §6.6 rule 7 — "
            f"'detected'/'both' are the collision detector's)")
    if claim.licence_class not in ARCHIVE_EMITTABLE_LICENCE_CLASSES:
        raise IntakeRefused(
            f"{claim.extractor_id} produced licence_class='{claim.licence_class}'; this "
            f"lane may only emit {sorted(ARCHIVE_EMITTABLE_LICENCE_CLASSES)} "
            f"(06 §6.6 rule 6)")


def stamp_archive_claim(
    claim: Claim, payload: ArchivedPayload, *, scope_version: str,
) -> Claim:
    """C9 + C10 + C4 + 06 §6.6 rules 1/2, applied to whatever the reader returned.

    `Claim` is frozen, so this is `dataclasses.replace`, never a mutation. The reader owns
    the VALUE; this owns where the value came from — and that split is what keeps the three
    rulings in one place instead of once per portal reader."""
    if payload.page_kind == FORBIDDEN_PAGE_KIND:
        raise IntakeRefused(
            f"payload {payload.id} carries page_kind='{FORBIDDEN_PAGE_KIND}'; C10 keeps the "
            f"page's own kind on the claim and leaves that enum member unused")
    return replace(
        claim,
        surface=ARCHIVE_SURFACE,
        page_kind=payload.page_kind,
        snapshot_anchor=ARCHIVE_ANCHOR,
        first_observed_at=payload.first_observed_at,
        history_completeness=ARCHIVE_HISTORY_COMPLETENESS,
        payload_id=payload.id,
        payload_sha256=payload.payload_sha256,
        payload_scope_version=scope_version,
    )


def _licensed_coordinate(
    claim: Claim, row: ListingRow, entry: Entry,
) -> tuple[Claim | None, str]:
    """The archived arm of the licence ladder, applied to a coordinate claim.

    The READER declares which branch of the page it read — a portal pin, or the portal's
    own geocode — and the LADDER stamps the class. That order matters: a reader that
    guessed `licence_class` would be re-litigating C6 once per portal, whereas here the only
    thing a reader can influence is which row of `ARCHIVED_COORDINATE_RULES` applies, and
    an entry with no row gets no coordinate at all."""
    verdict = coordinate_verdict(
        row.source, None, in_mapy_inventory=row.in_mapy_inventory,
        substrate=SUBSTRATE_ARCHIVED_HTML, entry_id=entry.entry_id,
        portal_pin_present=claim.licence_class != GEOCODED_LICENCE_CLASS)
    if not verdict.admitted or verdict.licence_class is None:
        return None, verdict.reason
    return replace(claim, licence_class=verdict.licence_class), verdict.reason


def extract_payload(
    payload: ArchivedPayload,
    row: ListingRow,
    entries: list[Entry],
    *,
    register: ScopeRegister,
) -> IntakeResult:
    """Everything this lane knows about one archived body. Pure — no DB, no clock, no
    network."""
    result = IntakeResult()
    applicable = archive_entries(entries, payload.page_kind)
    if not applicable or payload.body is None:
        return result

    document = scope_html(payload.body, register=register)
    if not document.is_complete:
        # `html_scope` fails CLOSED and an incomplete result admits nothing: "the scoper
        # broke" must never read as "no zones matched, extract freely". The attempt is
        # still recorded — 03 §3.2 rule 4 — so the cohort is countable rather than
        # indistinguishable from a page that genuinely carried no address.
        for entry in applicable:
            result.absences.append(Absence(
                listing_id=row.listing_id, surface=ARCHIVE_SURFACE,
                field_=entry.claim_type, reason="not_attempted",
                extraction_method=entry.extraction_method,
                detail="exclusion-zone scoping incomplete; the boundary had a hole"))
        return result

    for entry in applicable:
        for claim in ARCHIVE_READERS[str(entry.reader)](entry, row, payload, document):
            claim = stamp_archive_claim(claim, payload, scope_version=document.scope_version)
            if claim.claim_type == "coordinate":
                claim, reason = _licensed_coordinate(claim, row, entry)
                if claim is None:
                    result.absences.append(Absence(
                        listing_id=row.listing_id, surface=ARCHIVE_SURFACE,
                        field_="coordinate", reason="not_attempted",
                        extraction_method=entry.extraction_method, detail=reason))
                    continue
            assert_stampable(claim)
            assert_evidence_complete(claim)
            result.claims.append(claim)
    return result


# ------------------------------------------------------------------ SQL

# No local relation-existence check for `portal_raw_payloads`: `missing_relations()`
# establishes that migration 382 is applied, and 382 is the migration that CREATES it, so a
# second check could never fail on its own.
#
# "Latest body per (source, source_id_native, page_kind)" is expressed as an anti-join on
# `(first_observed_at, id)` rather than on `version_seq`: 403 added that counter with no
# backfill, so every body written before it is NULL there and a `>` comparison against NULL
# would silently rank the older row as the latest. `first_observed_at` is NOT NULL from 382,
# is monotonic per key by construction (a new body is first observed after the one it
# replaced), and `prp_native (source, source_id_native, page_kind, first_observed_at desc)`
# indexes exactly this predicate. `id` breaks the ties a bare timestamp sort would reshuffle.
#
# The scan projects NO body and NO `listings.raw_json`. The body is fetched separately and
# only for the rows an entry actually applies to (`_PAYLOAD_BODIES_SQL`), because the whole
# archive is 14 GB of TOASTed text and a scan that detoasts it to discover it has no reader
# is the exact failure W2-0's denominator query was written to avoid. `raw_json` is W1's
# substrate and this lane never reads it, so a `ListingRow` here carries `{}`.
_PAYLOAD_SCAN_FULL_SQL = """
    SELECT p.id, p.source, p.source_id_native, p.page_kind::text,
           encode(p.payload_sha256, 'hex'), p.first_observed_at,
           l.id, (a.listing_id IS NOT NULL) AS in_mapy_inventory
    FROM portal_raw_payloads p
    JOIN listings l ON l.id = p.listing_id
    LEFT JOIN mapy_affected a ON a.listing_id = l.id
    WHERE p.id > %(after_id)s
      AND (%(source)s::text IS NULL OR p.source = %(source)s)
      AND NOT EXISTS (
          SELECT 1 FROM portal_raw_payloads n
          WHERE n.source = p.source
            AND n.source_id_native = p.source_id_native
            AND n.page_kind = p.page_kind
            AND (n.first_observed_at, n.id) > (p.first_observed_at, p.id))
    ORDER BY p.id
    LIMIT %(batch_size)s
"""

# `first_observed_at`, never `last_observed_at`: the latter is bumped by every unchanged
# refetch, so an incremental cursor over it would re-walk the whole archive on every run
# while a genuinely new body could still slip behind the watermark.
_PAYLOAD_SCAN_INCREMENTAL_SQL = """
    SELECT p.id, p.source, p.source_id_native, p.page_kind::text,
           encode(p.payload_sha256, 'hex'), p.first_observed_at,
           l.id, (a.listing_id IS NOT NULL) AS in_mapy_inventory
    FROM portal_raw_payloads p
    JOIN listings l ON l.id = p.listing_id
    LEFT JOIN mapy_affected a ON a.listing_id = l.id
    WHERE p.first_observed_at >= %(watermark)s
      AND (p.first_observed_at, p.id) > (%(after_ts)s, %(after_id)s)
      AND (%(source)s::text IS NULL OR p.source = %(source)s)
      AND NOT EXISTS (
          SELECT 1 FROM portal_raw_payloads n
          WHERE n.source = p.source
            AND n.source_id_native = p.source_id_native
            AND n.page_kind = p.page_kind
            AND (n.first_observed_at, n.id) > (p.first_observed_at, p.id))
    ORDER BY p.first_observed_at, p.id
    LIMIT %(batch_size)s
"""

_PAYLOAD_BODIES_SQL = """
    SELECT id, body, body_r2_key, content_encoding
    FROM portal_raw_payloads
    WHERE id = ANY(%(ids)s::bigint[])
"""

_EXCLUSION_ZONES_SQL = """
    SELECT source, exclusion_zones
    FROM portal_contracts
    WHERE is_active
"""


def _row_from_payload_record(
    record: tuple[Any, ...],
) -> tuple[ArchivedPayload, ListingRow]:
    (payload_id, source, native, page_kind, sha_hex, first_observed_at, listing_id,
     in_inventory) = record
    payload = ArchivedPayload(
        id=int(payload_id),
        source=source,
        source_id_native=str(native),
        page_kind=page_kind,
        payload_sha256=str(sha_hex),
        first_observed_at=first_observed_at,
    )
    row = ListingRow(
        listing_id=int(listing_id),
        source=source,
        source_id_native=str(native),
        raw_json={},
        lat=None,
        lon=None,
        # 06 §6.6 Rule 1 + Rule 2: the BODY's own first observation, never now() and never
        # `last_observed_at` (which an unchanged refetch moves without new evidence).
        observed_at=first_observed_at,
        in_mapy_inventory=bool(in_inventory),
        legacy_columns=_DUMMY_LEGACY_COLUMNS,
    )
    return payload, row


def load_registers(conn: psycopg.Connection) -> dict[str, ScopeRegister]:
    """One exclusion-zone register per active contract. The register is CONTRACT DATA (02
    §2.1.4) and its hash is the `payload_scope_version` every claim carries, so it is read
    from `portal_contracts` here rather than re-parsed from the YAML on disk: a lane must
    scope by the register that is deployed, not by the one in the working tree."""
    registers: dict[str, ScopeRegister] = {}
    with conn.cursor() as cur:
        cur.execute(_EXCLUSION_ZONES_SQL)
        for source, zones in cur.fetchall():
            registers[source] = ScopeRegister.from_zones(source, zones or ())
    return registers


def load_bodies(
    cur: psycopg.Cursor, payload_ids: list[int],
) -> tuple[dict[int, bytes], int]:
    """The bodies for one batch's applicable rows, decoded. Returns (bodies, spilled).

    Takes the batch's OWN cursor rather than opening a transaction of its own: the whole
    batch is one all-or-nothing transaction, and a nested `guarded()` here would only add a
    savepoint around a read.

    A body that lives in R2 (`body IS NULL AND body_r2_key IS NOT NULL`) is COUNTED and
    skipped, never silently treated as absent: `payloads.ObjectStore` is an upload-only
    protocol today, so there is no GET to call. At the shipped 256 KB spill threshold no
    portal's page spills at all (migration 403), so the counter is expected to stay zero —
    and if it does not, the first per-portal PR that needs those pages will see the number
    rather than a quietly short corpus."""
    if not payload_ids:
        return {}, 0
    bodies: dict[int, bytes] = {}
    spilled = 0
    cur.execute(_PAYLOAD_BODIES_SQL, {"ids": payload_ids})
    for payload_id, body, body_r2_key, content_encoding in cur.fetchall():
        if body is None:
            if body_r2_key:
                spilled += 1
            continue
        bodies[int(payload_id)] = payloads.decode_body(
            bytes(body), content_encoding or "identity")
    return bodies, spilled


def _resume_point(
    conn: psycopg.Connection, *, mode: str, source: str | None, watermark: datetime | None,
) -> dict[str, Any] | None:
    """`claims_intake._resume_point`'s logic against THIS lane's rows — that function closes
    over its own module-level `LANE`, so it cannot be shared across two lanes writing to one
    `location_claim_batches`."""
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

    readable = sorted(
        s for s in wanted
        if any(e.reader in ARCHIVE_READERS for e in entries_by_source.get(s, ()))
    )
    if not readable:
        # Inert, and it returns BEFORE `_BATCH_INSERT_SQL`. A batch row that reached the end
        # of the scan is stamped 'ok', and 'ok' is what the incremental watermark reads —
        # so opening one here would let a lane with no readers claim it had mined the whole
        # archive, and the first portal PR to land would start behind a watermark covering
        # bodies nothing ever looked at.
        LOG.info("REMINE-ARCHIVE inert: no ACTIVE contract entry names a reader from "
                 "ARCHIVE_READERS (sources=%s). No batch opened.", ",".join(wanted))
        return {"outcome": "inert", "mode": mode, "payloads": 0, "claims": 0,
                "claims_inserted": 0, "observations": 0, "absences": 0, "batch_id": None,
                "readable_sources": []}

    registers = load_registers(conn)

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
            LOG.info("REMINE-ARCHIVE no prior successful batch for source=%s; "
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
            LOG.info("REMINE-ARCHIVE resuming a budget-stopped %s scan for source=%s from "
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
    LOG.info("REMINE-ARCHIVE start mode=%s source=%s batch=%d inventory_rows=%d "
             "readable=%s batch_id=%s", mode, source or "*", batch_size, inventory_rows,
             ",".join(readable), batch_id)

    started = time.monotonic()
    stats: dict[str, Any] = {
        "payloads": 0, "claims": 0, "claims_inserted": 0, "observations": 0,
        "enqueued": 0, "absences": 0, "spilled_bodies": 0, "oversized_values": 0,
        "stopped_early": False, "reached_end": False, "resumed_from_id": after_id,
        "readable_sources": readable,
    }
    try:
        while True:
            if limit is not None and stats["payloads"] >= limit:
                stats["stopped_early"] = True
                break
            if max_seconds is not None and time.monotonic() - started > max_seconds:
                LOG.info("REMINE-ARCHIVE stopping: --max-seconds reached")
                stats["stopped_early"] = True
                break
            size = batch_size if limit is None else min(batch_size, limit - stats["payloads"])

            with guarded(conn, statement_timeout) as cur:
                if mode == "incremental":
                    cur.execute(_PAYLOAD_SCAN_INCREMENTAL_SQL, {
                        "watermark": watermark, "after_ts": after_ts, "after_id": after_id,
                        "source": source, "batch_size": size,
                    })
                else:
                    cur.execute(_PAYLOAD_SCAN_FULL_SQL, {
                        "after_id": after_id, "source": source, "batch_size": size})
                records = cur.fetchall()
                if not records:
                    stats["reached_end"] = True
                    break

                scanned = [_row_from_payload_record(record) for record in records]
                wanted_bodies = [
                    payload.id for payload, _ in scanned
                    if archive_entries(entries_by_source.get(payload.source, []),
                                       payload.page_kind)
                ]
                bodies, spilled = load_bodies(cur, wanted_bodies)
                stats["spilled_bodies"] += spilled

                result = IntakeResult()
                for payload, row in scanned:
                    entries = entries_by_source.get(payload.source)
                    register = registers.get(payload.source)
                    if not entries or register is None:
                        continue
                    body = bodies.get(payload.id)
                    if body is None:
                        continue
                    result.extend(extract_payload(
                        replace(payload, body=body), row, entries, register=register))

                after_id = int(records[-1][0])
                if mode == "incremental":
                    after_ts = records[-1][5]
                stats["payloads"] += len(records)
                stats["claims"] += len(result.claims)
                stats["absences"] += len(result.absences)
                stats["oversized_values"] += result.oversized
                if not dry_run and batch_id is not None:
                    inserted, observed, enqueued = write_result(
                        cur, result, batch_id=batch_id, extractor_version=REMINE_VERSION)
                    stats["claims_inserted"] += inserted
                    stats["observations"] += observed
                    stats["enqueued"] += enqueued
            LOG.info("REMINE-ARCHIVE progress payloads=%d claims=%d inserted=%d observed=%d "
                     "absences=%d spilled=%d through_id=%d",
                     stats["payloads"], stats["claims"], stats["claims_inserted"],
                     stats["observations"], stats["absences"], stats["spilled_bodies"],
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
                LOG.exception("REMINE-ARCHIVE could not stamp batch %s as failed", batch_id)
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
                "note": f"payloads={stats['payloads']} "
                        f"stopped_early={stats['stopped_early']} "
                        f"reached_end={stats['reached_end']} through_id={after_id} "
                        f"spilled_bodies={stats['spilled_bodies']}",
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
        # Lease-row CAS, never an advisory lock: the transaction-mode pooler strands a lock
        # acquired on one backend and released on another. Its concurrency group is
        # `location-remine-archive`, distinct from W3's `location-remine` — the two lanes
        # write to one claim store but scan different substrates and must not serialise
        # behind each other.
        with lease.held(
            conn, JOB_NAME, cadence="1 hour", concurrency_group=CONCURRENCY_GROUP,
            ttl_seconds=args.lease_ttl_seconds,
        ) as acquired:
            if not acquired:
                LOG.info("REMINE-ARCHIVE skipped: another run holds the %s lease", JOB_NAME)
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
    LOG.info("REMINE-ARCHIVE done %s", json.dumps(stats, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
