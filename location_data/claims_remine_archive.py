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
    reader from `ARCHIVE_READERS`. Fourteen readers are registered — the four generic DOM
    ones W2-6 shipped plus the ten of the W2 reader canon (four DOM, five embedded-JSON,
    one JSON-LD breadcrumb) — but NO shipped contract names one yet, so a run still returns
    before it opens a batch row (see `run()`) rather than stamping 'ok' over a corpus it
    never mined: a batch stamped 'ok' moves the incremental watermark, and a watermark is a
    claim of coverage. The lane stops being inert on the first portal activation
    (W2-6…W2-12), and it has no dispatcher until W2-13 either way.
  * EVERY reader here is portal-AGNOSTIC and stays that way (rule 21). They differ by the
    QUESTION they ask of a node — its own text, a pattern over its text, a pattern over one
    attribute, whether a marker is present, one scalar of a JSON document it carries — never
    by which portal is being read. Which element, which pointer, which pattern, which junk
    pins: all contract data.
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
  Migration 382's `loc_claim_text_evidence` / `loc_claim_evidence_payload` /
  `loc_claim_llm_model` are the last line of defence, never the first: a batch is one
  transaction, so a single malformed `regex_text` claim would roll back every good claim
  beside it, and the error would name a constraint rather than the entry that produced it.
  `assert_evidence_complete` raises before the write, naming the extractor — and it covers
  all three, including the `llm_text` model attribution that has no reader yet, because a
  claim shape that can be spelled but not written is a trap laid for whoever builds one.

A READER STATES WHAT IT READ; IT DOES NOT STAMP WHAT THAT MEANS
  Readers return `ArchiveRead`, not a bare `Claim`, for one reason: a coordinate's licence
  class is decided by WHICH branch of the portal's map produced it (C6), only the reader
  knows that, and the claim's own `licence_class` arrives pre-filled from the contract
  entry's default — so a reader that says nothing looks exactly like one that read the
  first-party pin. `position_branch` is required on a coordinate read and refused on
  anything else; the ladder, not the reader, then stamps the class.

CLI:
    python -m location_data.claims_remine_archive --mode full --max-seconds 2400
    python -m location_data.claims_remine_archive --mode incremental --source remax
Required: SUPABASE_DB_URL, plus the R2_* env vars — W2a made the bucket the bodies' home
(the spill threshold is Postgres's own ~2 KB TOAST boundary, migrations 403/406), so a
run with a reader and no store is refused rather than left to mine the handful of
database-resident rows. Requires migrations 380-389 + 403-408 and an ACTIVE portal
contract per source. No new migration ships with this module.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from math import cos, hypot, isfinite, radians
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import unquote

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
    _base,
    _number,
    _text,
    json_pointer,
    MAX_CLAIM_VALUE_BYTES_ENV,
    DEFAULT_MAX_CLAIM_VALUE_BYTES,
    GUARD_CZ_BBOX,
    apply_transforms,
    assert_inventory_ready,
    claim_value_bytes,
    coordinate_verdict,
    env_positive_int,
    point_wkt,
    guard_admits,
    guarded,
    load_entries,
    missing_relations,
    write_result,
)
from location_data.html_scope import (
    ScopeRegister, ScopedDocument, collapse_ws, scope_html,
)
from location_data.resolver import lease
from scraper import db
from scraper.remax_parser import parse_dms_pair

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
# evidence but is not required to. `llm_text` additionally has to satisfy
# `loc_claim_llm_model` — a model assertion that cannot name the model that made it is not
# evidence — which is why `LLM_METHOD` is checked separately below rather than folded in.
EVIDENCE_METHODS = frozenset({"llm_text", "regex_text"})
LLM_METHOD = "llm_text"

# Which branch of a portal's detail map a coordinate was read from. The READER states this;
# it is never inferred from what the reader happened to stamp on the claim. Inferring it
# (say, from `licence_class != 'odbl'`) has one silent failure mode and it is the expensive
# one: a Nominatim-fallback reader that simply forgets to say so inherits the entry's
# `licence_class: portal` default and a republished OSM position is filed as first-party,
# with nothing anywhere to catch it. A required argument cannot be forgotten quietly.
# The substrate unescapes a reader may be told to apply before it reads. Both are opt-in
# contract data and both are named, never inferred: `percent` is a URL property (a
# percent-encoded slug normalises to a gazetteer-unjoinable string), `js_string` is a
# script property (maxima ships its map config as a JS string literal). A name outside the
# set is refused rather than ignored — silently not decoding is how a claim's value stops
# joining to anything with no error anywhere.
_ATTR_DECODERS = frozenset({"none", "percent"})
_JSON_DECODERS = frozenset({"none", "js_string"})

POSITION_BRANCH_PORTAL_PIN = "portal_pin"
POSITION_BRANCH_PORTAL_GEOCODED = "portal_geocoded"
POSITION_BRANCHES = frozenset({POSITION_BRANCH_PORTAL_PIN, POSITION_BRANCH_PORTAL_GEOCODED})

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


class BodyStore(Protocol):
    """The one R2 operation this lane needs — a GET, where `payloads.ObjectStore` is the
    writer's PUT-only half. Declared here rather than widened onto that protocol so the
    archive writer's fakes are not forced to grow a method they never call.
    `scraper.image_storage.R2Client` satisfies both."""

    def download_bytes(self, key: str) -> bytes: ...


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


@dataclass(frozen=True, slots=True)
class ArchiveRead:
    """One thing a reader found, plus the one fact about HOW it found it that the claim
    itself cannot carry.

    `position_branch` is required on a `claim_type='coordinate'` read and refused on any
    other. It exists because the licence class of a coordinate is decided by which branch of
    the portal's map markup produced it (C6), and only the reader knows that — while the
    claim's own `licence_class` arrives pre-filled from the contract entry's default, so a
    reader that says nothing is indistinguishable from one that read the pin. Making it an
    argument rather than an inference converts "forgot to declare the fallback branch" from
    a silent mis-licensing into a refusal that names the entry."""
    claim: Claim
    position_branch: str | None = None


class SubjectNotFound(RuntimeError):
    """`subject_scope: {kind: id_match, on_miss: fail}` and no object on this page is the
    subject's.

    NOT an `IntakeRefused`: a page whose embedded id no longer matches the listing it was
    fetched for is a portal fact about ONE row (a re-id, a redirect, an interstitial saved
    under the wrong key), and refusing would roll back a batch of thousands over it. NOT a
    bare `[]` either — then "the portal changed its id scheme fleet-wide" and "this page
    genuinely carries no address" would be the same green zero-claim sweep. `extract_payload`
    turns it into one `not_attempted` absence per applicable entry, which is the countable
    cohort 03 §3.2 rule 4 asks for."""


ArchiveReaderFn = Callable[
    [Entry, ListingRow, ArchivedPayload, ScopedDocument], list[ArchiveRead]]

# Populated from W2-6 onward. Deliberately a second registry rather than an extension of
# `claims_intake.READERS`: W1's readers take `(entry, row)` and read `raw_json`, these take a
# scoped DOM, and a name present in one but not the other must be a refusal rather than a
# silent no-op. The lane is inert whenever this is empty (see the module docstring).
ARCHIVE_READERS: dict[str, ArchiveReaderFn] = {}


def archive_reader(name: str) -> Callable[[ArchiveReaderFn], ArchiveReaderFn]:
    def register(fn: ArchiveReaderFn) -> ArchiveReaderFn:
        ARCHIVE_READERS[name] = fn
        return fn
    return register


def _entry_css(entry: Entry) -> str:
    """The CSS selector a DOM entry must declare.

    Refused rather than defaulted: an entry that reaches a DOM reader without a selector is
    a contract/projection mismatch, and the alternative — treating it as "match nothing" —
    is a coverage hole that produces no claim, no absence and no error."""
    css = entry.locator.get("css")
    if not css or not isinstance(css, str):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} uses a DOM reader but declares no "
            f"`locator.css` (got {css!r})")
    return css


def _evidenced(
    entry: Entry, row: ListingRow, document: ScopedDocument, *,
    value: str, within: Any, quote: str | None = None, **overrides: Any,
) -> Claim:
    """A DOM claim carrying migration 382's evidence set.

    `subject_scoped` comes from the CONTRACT (`subject_scope.subject_scoped`), never from
    the reader: whether a node is the subject's own is a per-portal fact the entry declares
    and the scoper enforces, and a reader that decided it for itself would be re-litigating
    D7 once per portal. `find_span` is entity- and whitespace-tolerant and returns None
    rather than guessing — a span pointing at the wrong occurrence of a common street name
    still satisfies the CHECK's substring test, which makes it worse than no span.

    `quote` exists because for some readers the VALUE is not a substring of the body. A
    coordinate assembled from two separate attributes has a readable value ("lat,lon") that
    appears nowhere in the HTML, so quoting it produces an unlocatable span — a claim
    asserting evidence it cannot point at. Those readers pass the node's own serialisation,
    which does contain both attributes and is genuinely findable. Default stays
    `quote = value`, which is correct wherever the value was lifted verbatim."""
    quote = value if quote is None else quote
    span = document.find_span(quote, within=within)
    return _base(
        entry, row,
        value_text=value,
        evidence_quote=quote,
        span_start=span[0] if span else None,
        span_end=span[1] if span else None,
        subject_scoped=bool(entry.subject_scope.get("subject_scoped", True)),
        **overrides,
    )


@archive_reader("html_text")
def _read_html_text(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """Text of the FIRST node matching the entry's selector, as one evidenced claim.

    First-match, not all-matches, and that is the whole point on this substrate: remax's
    contamination class is a page where the subject's address and a neighbour's are both
    present in the DOM, so "every match" would re-import exactly what the exclusion zones
    exist to strip. A portal that genuinely needs every match declares a different reader
    rather than widening this one.

    The node is read from the SCOPED document, so an excluded zone cannot match here even
    if a contract's selector would otherwise reach into one."""
    node = document.css_first(_entry_css(entry))
    if node is None:
        return []
    raw = _text(node.text())
    value = apply_transforms(raw, entry.transform)
    if value is None:
        return []
    return [ArchiveRead(_evidenced(entry, row, document, value=value, within=node,
                                   quote=_transformed_quote(raw, value)))]


# The same alphabet `html_scope` collapses and matches spans with: `\s` already covers
# NBSP, the zero-width space it does not, and a scrubbed archive body carries both.
_OWN_TEXT_WS_RE = re.compile("[\\s\\u00a0\\u200b]+")


def _transformed_quote(raw: str | None, value: str) -> str | None:
    """The literal a transformed value was read FROM, or None to quote the value itself.

    `_evidenced` defaults the quote to the value, which is right whenever the value was
    lifted verbatim. With a transform it is not: the claimed value is a NORMALISED form, and
    `find_span` would then anchor on whatever occurrence of that shorter string comes first
    inside the node — measured on the ceskereality fixture, a `data-city` transformed to
    `České Budějovice` resolved its span into the node's `value="Nádražní 1067, České
    Budějovice"` attribute rather than into `data-city`. A span pointing at a different
    attribute is worse than no span. No-op for every entry without a transform."""
    return None if raw is None or raw == value else raw


@archive_reader("html_own_text")
def _read_html_own_text(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """The first matching node's OWN text — direct text children only, whitespace collapsed.

    `html_text` reads `node.text()`, which concatenates every descendant. That is right for
    a leaf and wrong for a subject header that nests chrome: remax's `h2.pd-header__address`
    ends with an `<a …>mapa</a>` jump-link on 12/12 mined pages, so the deep read states the
    subject's address as "ulice Pod Slovany, Úvaly mapa". Reading only the element's own text
    nodes is stable against that link's LABEL changing, which a strip-the-suffix transform
    would not be, and it needs no per-portal selector surgery.

    Whitespace is collapsed in the same act and for the same reason: the portal breaks one
    address line across source lines, so both reads carry a 15-tab run that is not part of
    the value the page states. `find_span` matches whitespace runs entity- and NBSP-
    tolerantly, so the collapsed value still resolves to the REAL span in the source — the
    evidence span is then LONGER than the quote, which is correct, not a defect."""
    node = document.css_first(_entry_css(entry))
    if node is None:
        return []
    raw = _text(_OWN_TEXT_WS_RE.sub(" ", node.text(deep=False) or ""))
    value = apply_transforms(raw, entry.transform)
    if value is None:
        return []
    return [ArchiveRead(_evidenced(entry, row, document, value=value, within=node,
                                   quote=_transformed_quote(raw, value)))]


@archive_reader("html_attr")
def _read_html_attr(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """One ATTRIBUTE of the first matching node — the carrier for markup that puts the fact
    in an attribute rather than in text (remax's `data-display-address`, and every index
    card that stamps its address on the element)."""
    attribute = _entry_attr(entry, "html_attr")
    node = document.css_first(_entry_css(entry))
    if node is None:
        return []
    raw = _text(node.attributes.get(attribute))
    value = apply_transforms(raw, entry.transform)
    if value is None:
        return []
    return [ArchiveRead(_evidenced(entry, row, document, value=value, within=node,
                                   quote=_transformed_quote(raw, value)))]


def _entry_attr(entry: Entry, reader: str) -> str:
    """The single attribute name an attribute reader must declare, refused not defaulted —
    the same call `_entry_css` makes about the selector, for the same reason."""
    attribute = entry.locator.get("attr")
    if not attribute or not isinstance(attribute, str):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} uses `{reader}` but declares no "
            f"`locator.attr` (got {attribute!r})")
    return attribute


def _entry_pattern(entry: Entry, reader: str) -> tuple[re.Pattern[str], str | int]:
    """`locator.pattern` compiled, plus the ONE group `locator.group` names.

    Both are refused rather than defaulted. A pattern that will not compile matches nothing
    forever, and a defaulted group ("group 0", "the only group") would make a claim's
    meaning depend on the order the groups happen to be written in — bazos' one href carries
    the obec and the PSČ as two groups read by two entries."""
    pattern = entry.locator.get("pattern")
    if not pattern or not isinstance(pattern, str):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} uses `{reader}` but declares no "
            f"`locator.pattern` (got {pattern!r})")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares an uncompilable `locator.pattern` "
            f"{pattern!r} ({exc})") from exc
    group = entry.locator.get("group")
    if isinstance(group, bool) or group is None or group == "":
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} uses `{reader}` but names no `locator.group` "
            f"to claim (got {group!r})")
    if isinstance(group, int) or str(group).isdigit():
        index = int(group)
        if index < 1 or index > compiled.groups:
            raise IntakeRefused(
                f"{entry.source}:{entry.entry_id} names capture group {index}, which "
                f"{pattern!r} does not define ({compiled.groups} group(s))")
        return compiled, index
    if str(group) not in compiled.groupindex:
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} names capture group '{group}', which "
            f"{pattern!r} does not define "
            f"({', '.join(sorted(compiled.groupindex)) or 'no named groups'})")
    return compiled, str(group)


@archive_reader("html_regex")
def _read_html_regex(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """One capture group of a pattern run over the TEXT of the first matching node.

    ceskereality stamps the accented street inside its `<title>` — `…, ulice Májová, okres
    Karlovy Vary - ČESKÉREALITY.cz inzerce realit` — and that title is the only place the
    diacritics survive on a portal whose `listings.street` is 97.9% ASCII-folded.

    The EVIDENCE QUOTE is the WHOLE MATCH, not the captured value. `regex_text` is an
    evidence-bearing method (01 §4.2), a bare street name occurs in several places on a
    portal page, and `find_span` takes the first occurrence within the node — so quoting the
    match keeps the span pointing at the pattern that actually produced the value, and keeps
    `document.html[span] == evidence_quote` true, which quoting only the group could not.

    A match whose span cannot be located yields NO claim: `assert_evidence_complete` refuses
    a span-less `regex_text` claim and that refusal aborts the whole batch — one page is
    never worth thousands of good ones."""
    compiled, group = _entry_pattern(entry, "html_regex")
    node = document.css_first(_entry_css(entry))
    if node is None:
        return []
    match = compiled.search(node.text() or "")
    if match is None:
        return []
    value = apply_transforms(_text(match.group(group)), entry.transform)
    if value is None:
        return []
    claim = _evidenced(entry, row, document, value=value, within=node, quote=match.group(0))
    if claim.span_start is None or claim.span_end is None:
        return []
    return [ArchiveRead(claim)]


@archive_reader("html_attr_regex")
def _read_html_attr_regex(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """One capture group of a pattern run over a URL-bearing ATTRIBUTE of a DOM node.

    The carrier for a fact a portal publishes ONLY in a link: bazos names the true
    municipality nowhere on the page except the town-listings anchor's href
    (`/inzeraty/<obec-slug>/<psc5>/`), while that anchor's visible TEXT is the okres — the
    defect that put 29,546 active rows onto 90 distinct `locality` values.

    ALL matching nodes are considered, in document order, and the PATTERN is the
    discriminator — not `css_first`. That is the whole reason this is not `html_attr`: a
    Lokalita cell holds two anchors and a page can hold a category link with the same prefix,
    so "the first node matching the selector" is the wrong node about as often as the right
    one. The first node whose attribute MATCHES wins; once one matches it is the node, and a
    transform that then nulls the value yields no claim rather than a scan for a more
    agreeable neighbour.

    `decode: percent` unescapes the attribute before matching, and it is opt-in because it
    is a property of a URL substrate rather than of every attribute: on a percent-encoded
    slug `ho%C5%99ice-v-podkrkono%C5%A1%C3%AD` normalises through `location_value_norm` to
    `ho c5 99ice v podkrkono c5 a1 c3 ad`, which joins to no gazetteer row, while the decoded
    form normalises to `horice v podkrkonosi`, which does.

    The QUOTE is the node's own serialisation, for the same reason `html_point_attrs` quotes
    `node.html`: a decoded slug appears nowhere in the body and a bare `12` or `50801` would
    resolve to some other digit run, while the opening tag carries the whole URL."""
    compiled, group = _entry_pattern(entry, "html_attr_regex")
    attribute = _entry_attr(entry, "html_attr_regex")
    decode = str(entry.locator.get("decode") or "none")
    if decode not in _ATTR_DECODERS:
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares decode={decode!r}; "
            f"`html_attr_regex` implements {sorted(_ATTR_DECODERS)}")
    for node in document.css(_entry_css(entry)):
        raw = _text(node.attributes.get(attribute))
        if raw is None:
            continue
        match = compiled.search(unquote(raw) if decode == "percent" else raw)
        if match is None:
            continue
        value = apply_transforms(_text(match.group(group)), entry.transform)
        if value is None:
            return []
        return [ArchiveRead(_evidenced(entry, row, document, value=value, within=node,
                                       quote=node.html or raw))]
    return []


@archive_reader("html_marker")
def _read_html_marker(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """A PRESENCE detector: the portal's own marker, typed as the label the contract gives it.

    Three shapes, one reader, because the difference is contract data: a selector alone
    (realitymix's `--estimated` block), a selector plus a literal the node's text must
    contain (idnes' "Nemovitost nemá přesnou adresu…" disclaimer), or a selector plus an
    attribute that must be present (bazos' maps-anchor `title="Přibližná lokalita"`).

    The claim's VALUE is the contract's canonical label and its EVIDENCE is the portal's own
    text or attribute — two different fields for exactly this case, so a portal that rewords
    its sentence stops matching instead of silently restating a different fact under the same
    label. Blur is decided the way `declared_quality` decides it, by membership of that label
    in the entry's `precision_map.blurred_labels`, so recalibrating which label means
    "blurred" is a contract version bump and never a code change (06 §6.6 rule 7 — the axis
    is written explicitly, never defaulted).

    No transform: normalising a label the contract itself wrote would break the membership
    test that decides the blur axis."""
    label = entry.locator.get("value_label")
    if not label or not isinstance(label, str):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} uses `html_marker` but declares no "
            f"`locator.value_label` — the claim's value is contract data here, never page "
            f"text (got {label!r})")
    contains = entry.locator.get("contains")
    if contains is not None and (not isinstance(contains, str) or not contains):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares locator.contains={contains!r}; it "
            f"must be the non-empty literal the node's text has to carry")
    names = entry.locator.get("attr")
    if names is not None:
        names = [names] if isinstance(names, str) else list(names)
        if not names or not all(isinstance(n, str) and n for n in names):
            raise IntakeRefused(
                f"{entry.source}:{entry.entry_id} declares locator.attr="
                f"{entry.locator.get('attr')!r}; it must be one attribute name or a "
                f"non-empty list of them, all of which must be present to mark")
    node = document.css_first(_entry_css(entry))
    if node is None:
        return []
    if names:
        values = [_text(node.attributes.get(name)) for name in names]
        if any(value is None for value in values):
            return []
        # One attribute quotes its own value, which is genuinely findable; a PAIR has no
        # single literal to quote (the fact is that both are there), so the node's own
        # serialisation is the honest evidence — the same call `html_point_attrs` makes.
        evidence = str(values[0]) if len(values) == 1 else (node.html or str(label))
        haystack = str(values[0]) if len(values) == 1 else " ".join(str(v) for v in values)
    else:
        evidence = _text(node.text()) or node.html or str(label)
        haystack = evidence
    if contains is not None:
        if collapse_ws(contains) not in collapse_ws(haystack):
            return []
        evidence = contains
    blurred = {str(x) for x in (entry.precision_map.get("blurred_labels") or [])}
    claim = _evidenced(
        entry, row, document, value=str(label), within=node, quote=evidence,
        declared_precision_label=str(label),
        blur_evidence="declared" if label in blurred else "none")
    return [ArchiveRead(claim)]


@archive_reader("html_point_dms")
def _read_html_point_dms(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """A coordinate from a DMS attribute (remax stamps `data-gps="50°04'26.1"N,14°43'41.5"E"`).

    Parsing is `scraper.remax_parser.parse_dms_pair` — the SAME function the live scraper
    has used since the portal was onboarded, including its CZ-bbox refusal, rather than a
    second implementation that would drift from it silently.

    `position_branch` is contract DATA (`locator.position_branch`), so which branch of the
    portal's map produced a pin is declared once per entry and the LADDER stamps the licence
    class from it (C6). A reader that inferred the branch would be deciding a licence
    question per portal, which is exactly what `_licensed_coordinate` refuses."""
    branch = _coordinate_branch(entry)
    attribute = str(entry.locator.get("attr") or "data-gps")
    node = document.css_first(_entry_css(entry))
    if node is None:
        return []
    raw = _text(node.attributes.get(attribute))
    if raw is None:
        return []
    lat, lon = parse_dms_pair(raw)
    if lat is None or lon is None:
        return []
    claim = _evidenced(
        entry, row, document, value=raw, within=node,
        value_geom_wkt=point_wkt(lat, lon),
    )
    return [ArchiveRead(claim, position_branch=str(branch))]


def _coordinate_branch(entry: Entry) -> str:
    """The C6 branch a coordinate entry declares, refused rather than defaulted.

    Shared by both coordinate readers so the refusal wording and the enum check cannot
    drift apart — which they already had one chance to, since `html_point_dms` grew this
    check inline first."""
    branch = entry.locator.get("position_branch")
    if branch not in POSITION_BRANCHES:
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares position_branch={branch!r}; a DOM "
            f"coordinate entry must name one of {sorted(POSITION_BRANCHES)} (C6)")
    return str(branch)


@archive_reader("html_point_attrs")
def _read_html_point_attrs(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """A coordinate from a PAIR of decimal attributes on one node.

    realitymix publishes `<div id="print-map" data-gps-lat="49.73561" data-gps-lon="13.39051">`
    — two separate decimal attributes, not the single DMS string remax uses, which is why
    `html_point_dms` cannot read it and why this exists (W2-7 verification, 2026-08-18).

    `locator.attr` is an ORDERED PAIR `[lat_attr, lon_attr]`, not a single name. The order
    is contract data because nothing in the markup states it: `data-gps-lat`/`data-gps-lon`
    happen to be self-describing, but a portal publishing `data-x`/`data-y` would not be,
    and silently guessing which is latitude is how a coordinate lands in the wrong
    hemisphere. A malformed pair is refused, never reordered.

    **The CZ-bbox guard is genuinely evaluated here**, and that is the difference from
    `html_point_dms`. That reader gets the envelope for free inside `parse_dms_pair` and
    therefore declares `consults_guards=False` — a review caught it declaring True while
    never calling `guard_admits`, which would have admitted a guard the runtime ignored.
    A decimal attribute pair goes through no such helper, so the check has to be explicit,
    and the reader calls it rather than the contract merely naming it."""
    branch = _coordinate_branch(entry)
    names = entry.locator.get("attr")
    if not isinstance(names, (list, tuple)) or len(names) != 2 or not all(names):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} uses `html_point_attrs` but declares "
            f"`locator.attr`={names!r}; it must be an ordered [lat_attr, lon_attr] pair")
    node = document.css_first(_entry_css(entry))
    if node is None:
        return []
    raw_lat = _text(node.attributes.get(str(names[0])))
    raw_lon = _text(node.attributes.get(str(names[1])))
    if raw_lat is None or raw_lon is None:
        return []
    try:
        lat, lon = float(raw_lat), float(raw_lon)
    except ValueError:
        # A non-numeric attribute is the portal changing shape under us. No claim, and no
        # exception either: one malformed page must not abort a batch of thousands.
        return []
    # STRUCTURAL, not contract-optional: `float()` happily returns nan/inf, and a
    # `POINT(nan nan)` reaching `ST_GeomFromText` either stores a non-finite geometry in an
    # append-only table or aborts the whole batch INSERT around it. The CZ envelope below
    # is contract-declared policy — a portal may legitimately publish foreign coordinates —
    # but finiteness is not policy, and it must not depend on an entry remembering to name
    # a guard. `html_point_dms` gets this for free inside `parse_dms_pair`; this path has
    # no such helper, so it is asserted here.
    if not (isfinite(lat) and isfinite(lon)):
        return []
    if not guard_admits(entry, GUARD_CZ_BBOX, (lat, lon)):
        return []
    claim = _evidenced(
        entry, row, document, value=f"{raw_lat},{raw_lon}", within=node,
        # The node's own serialisation, NOT the value: "lat,lon" is assembled by this
        # reader and appears nowhere in the HTML, so quoting it would leave a claim
        # asserting evidence it cannot point at. The opening tag carries both attributes
        # and is genuinely findable in the scoped body.
        quote=node.html or f"{raw_lat},{raw_lon}",
        value_geom_wkt=point_wkt(lat, lon),
    )
    return [ArchiveRead(claim, position_branch=branch)]


# --------------------------------------------- the embedded-JSON acquisition layer
#
# Five readers below address a JSON document the PAGE carries: idnes' `<script
# data-maptiler-json>`, mmreality's `:property` Vue prop, maxima's `JSON.parse('…')`
# OpenLayers config, realitymix's schema.org block. They differ in what they EXTRACT, never
# in how they get the document, so acquisition is one function with four optional locator
# keys — `attr` (the JSON lives in an attribute rather than in the node's text),
# `script_match` (it is one argument inside a script's source), `decode` (it is a JS string
# literal), and the subject match below. Writing it once is what stops "one portal needed
# something special" becoming five slightly different parsers (rule 21).

# What a JS single-quoted string literal can carry. Decoded IN FULL and only then handed to
# `json.loads`, because that is what the browser does: a JS-layer `\\` is one backslash,
# which the JSON layer may then read as the start of ITS own escape. Half-decoding gets
# `"a\\\\b"` wrong by exactly one level. NOT `codecs.decode(raw, "unicode_escape")`, which
# round-trips through latin-1 and mangles every Czech diacritic in the blob.
_JS_ESCAPES = {"'": "'", '"': '"', "\\": "\\", "/": "/", "n": "\n", "r": "\r",
               "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}

# The `ListingRow` fields a subject match may compare against. A closed set, because the
# alternative is a predicate language over the row (02 §2.1.3 refuses one for
# `require_column_equals` for the same reason).
SUBJECT_MATCH_ROW_FIELDS = frozenset({"source_id_native"})
SUBJECT_MATCH_KIND = "id_match"
SUBJECT_MISS_FAIL = "fail"

# `locator.reject_points` is compared at five decimal places (~1.1 m). The junk pins it
# rejects are EXACT 5-dp shares in the stored corpus (119 idnes rows on 49.19186,16.61109)
# while the page publishes 8 dp, so equality on the raw value would match nothing.
_REJECT_POINT_DP = 5

# A coordinate array as it is WRITTEN in a JSON source — the slice an evidence span points
# at. Matched against the parsed pair rather than trusted positionally, so a re-serialised
# quote can never claim a position the body does not contain.
_COORD_ARRAY_RE = re.compile(r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]")
_JSON_MEMBER_WS = r"[ \t\r\n]*"
_JSON_SCALAR_RE = re.compile(r"(?:true|false|null|-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")


@dataclass(frozen=True, slots=True)
class EmbeddedDocument:
    """One JSON document a page carries, plus the two things an evidence span needs.

    `source` is the JSON text AS WRITTEN in the body — that is what a span indexes into, and
    a quote rebuilt by `json.dumps` is a different document. `verbatim` says whether that
    text is still the literal JSON: after a `js_string` decode it is not (the body spells
    `\\"zoom\\"`), so a member slice computed against the decoded form would not resolve and
    the readers fall back to quoting the captured source itself."""
    node: Any
    source: str
    data: Any
    verbatim: bool


def _decode_js_string(raw: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char != "\\" or index + 1 >= len(raw):
            out.append(char)
            index += 1
            continue
        nxt = raw[index + 1]
        if nxt == "u" and index + 6 <= len(raw):
            try:
                out.append(chr(int(raw[index + 2:index + 6], 16)))
                index += 6
                continue
            except ValueError:
                pass
        if nxt == "x" and index + 4 <= len(raw):
            try:
                out.append(chr(int(raw[index + 2:index + 4], 16)))
                index += 4
                continue
            except ValueError:
                pass
        out.append(_JS_ESCAPES.get(nxt, nxt))
        index += 2
    text = "".join(out)
    try:  # recombine any surrogate pair the `\uXXXX` branch split
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return text


def embedded_documents(entry: Entry, document: ScopedDocument) -> list[EmbeddedDocument]:
    """Every JSON document this entry's locator addresses, in document order.

    A malformed CONTRACT is an `IntakeRefused` naming the entry; a malformed PAGE is simply
    not in the list. One portal changing shape must not abort a batch of thousands, and an
    archived body is immutable, so a raise here would be a permanently failing row rather
    than a retryable one. That is not hypothetical: this repo's only captured idnes page has
    had its map JSON destroyed by the fixture anonymiser and no longer parses."""
    attribute = entry.locator.get("attr")
    if attribute is not None and (not isinstance(attribute, str) or not attribute):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares locator.attr={attribute!r}; an "
            f"embedded-JSON entry either names ONE attribute carrying the document or "
            f"names none and reads the node's own text")
    decode = str(entry.locator.get("decode") or "none")
    if decode not in _JSON_DECODERS:
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares decode={decode!r}; this lane "
            f"implements {sorted(_JSON_DECODERS)}")
    script_match = entry.locator.get("script_match")
    pattern: re.Pattern[str] | None = None
    if script_match is not None:
        if not isinstance(script_match, str) or not script_match:
            raise IntakeRefused(
                f"{entry.source}:{entry.entry_id} declares locator.script_match="
                f"{script_match!r}; it must be the pattern that captures the document")
        try:
            pattern = re.compile(script_match)
        except re.error as exc:
            raise IntakeRefused(
                f"{entry.source}:{entry.entry_id} declares a script_match that will not "
                f"compile ({exc})") from exc
        if "config" not in pattern.groupindex and pattern.groups < 1:
            raise IntakeRefused(
                f"{entry.source}:{entry.entry_id} declares a script_match that captures "
                f"nothing; it must carry a `(?P<config>…)` group (or one positional group)")
    found: list[EmbeddedDocument] = []
    for node in document.css(_entry_css(entry)):
        raw = node.attributes.get(attribute) if attribute else node.text()
        if not raw or not raw.strip():
            continue
        source = raw
        if pattern is not None:
            match = pattern.search(raw)
            if match is None:
                continue
            source = (match.group("config") if "config" in pattern.groupindex
                      else match.group(1))
            if source is None:
                continue
        try:
            data = json.loads(_decode_js_string(source) if decode == "js_string" else source)
        except (ValueError, TypeError):
            continue
        found.append(EmbeddedDocument(node, source, data, decode != "js_string"))
    return found


def _subject_object(
    entry: Entry, row: ListingRow, documents: list[EmbeddedDocument],
) -> tuple[EmbeddedDocument, Any] | None:
    """(the document, the object this entry reads out of it), or None when there is nothing.

    Without `locator.match` this is simply the first parsed document, narrowed by
    `locator.then` when the entry names one (maxima's `/features/0`).

    With `locator.match` it is EQUALITY on the listing's own key — never "the first
    feature", never "the largest blob", never a map's view centre. Both defects are measured:
    idnes ships 20 neighbour features per page, each with a complete address, so a positional
    pick is precisely how a neighbour's address becomes this listing's street; and on the
    pinned archived mmreality body the NEIGHBOUR's `:property` blob (23,656 chars) is LARGER
    than the subject's (13,827), so `mmreality_parser`'s largest-blob fallback returns
    another listing's location. `locator.exclude_where` additionally honours an exclusion
    zone that is a PREDICATE (idnes' `features[isSimilar=true]`), which no RFC 6901 pointer
    can pop and `html_scope` therefore defers to the reader.

    `on_miss: fail` — the only mode implemented — means NO CLAIM plus an absence, raised as
    `SubjectNotFound`. TWO matches inside one document mean the same: a coin toss between two
    subjects is not evidence. A duplicate ACROSS documents is not ambiguity (mmreality serves
    the subject blob on several components of the same page), so the first document carrying
    exactly one match wins."""
    pointer = entry.locator.get("then")
    match = entry.locator.get("match")
    if match is None:
        if not documents:
            return None
        head = documents[0]
        found = json_pointer(head.data, str(pointer)) if pointer else head.data
        return None if found is None else (head, found)
    if not isinstance(match, Mapping) or not match.get("json_pointer"):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} selects a subject but declares no "
            f"`locator.match.json_pointer` naming the key to compare (got {match!r})")
    field = str(match.get("equals_row_field") or "")
    if field not in SUBJECT_MATCH_ROW_FIELDS:
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares "
            f"locator.match.equals_row_field={field!r}; the extractor compares against the "
            f"listing's own key and knows only {sorted(SUBJECT_MATCH_ROW_FIELDS)}")
    scope = entry.subject_scope or {}
    if scope.get("kind") != SUBJECT_MATCH_KIND or scope.get("on_miss") != SUBJECT_MISS_FAIL:
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares subject_scope kind="
            f"{scope.get('kind')!r} on_miss={scope.get('on_miss')!r}; an entry that selects "
            f"its subject by id must declare {{kind: {SUBJECT_MATCH_KIND}, on_miss: "
            f"{SUBJECT_MISS_FAIL}}} — the narrowing and the declaration of what a miss means "
            f"are one rule, and any other mode would be the positional fallback this entry "
            f"exists to forbid")
    exclude = entry.locator.get("exclude_where")
    if exclude is not None and (not isinstance(exclude, Mapping)
                                or not exclude.get("json_pointer")
                                or "equals" not in exclude):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares malformed `locator.exclude_where` "
            f"({exclude!r}); it must name a json_pointer and the value it equals")
    wanted = _text(getattr(row, field, None))
    key = str(match["json_pointer"])
    seen = 0
    for candidate_document in documents:
        found = (json_pointer(candidate_document.data, str(pointer)) if pointer
                 else candidate_document.data)
        if found is None:
            continue
        pool = found if isinstance(found, list) else [found]
        seen += len(pool)
        if exclude is not None:
            excluded = str(exclude["json_pointer"])
            pool = [item for item in pool
                    if json_pointer(item, excluded) != exclude.get("equals")]
        hits = [item for item in pool if _text(json_pointer(item, key)) == wanted]
        if len(hits) == 1:
            return candidate_document, hits[0]
        if len(hits) > 1:
            raise SubjectNotFound(
                f"{entry.entry_id}: {len(hits)} objects on this {row.source} body carry "
                f"{key}=={wanted!r}; two subjects is not evidence, on_miss=fail")
    raise SubjectNotFound(
        f"{entry.entry_id}: none of the {seen} candidate object(s) on this {row.source} "
        f"body carries {key}=={wanted!r}; on_miss=fail")


def _json_value_end(source: str, start: int) -> int | None:
    """End offset of the JSON value beginning at `start`. String-aware brace/bracket
    matching, so a `{` inside a string cannot unbalance an object."""
    if start >= len(source):
        return None
    char = source[start]
    if char == '"':
        index = start + 1
        while index < len(source):
            if source[index] == "\\":
                index += 2
                continue
            if source[index] == '"':
                return index + 1
            index += 1
        return None
    if char in "{[":
        depth, index, in_string = 0, start, False
        while index < len(source):
            current = source[index]
            if in_string:
                if current == "\\":
                    index += 2
                    continue
                if current == '"':
                    in_string = False
            elif current == '"':
                in_string = True
            elif current in "{[":
                depth += 1
            elif current in "}]":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        return None
    found = _JSON_SCALAR_RE.match(source, start)
    return found.end() if found else None


def _json_member_source(source: str, pointer: str) -> tuple[str, str] | None:
    """`("key":value, value)` AS SPELLED IN THE SOURCE at an RFC 6901 object pointer.

    This is what makes an evidence span possible on this substrate at all. mmreality
    JSON-escapes accents, so the DECODED value ("Křižíkova") is not a substring of the scoped
    payload while its source form (`"street":"K\\u0159i\\u017e\\u00edkova"`) is — modulo the
    `"` -> `&quot;` the attribute serialisation applies, which `ScopedDocument.find_span`
    already bridges.

    The KEY is included on purpose: the bare escaped value also occurs in `title`,
    `location` and `slug`, and `find_span` returns the first occurrence within the anchor
    node — a span pointing at the wrong occurrence still satisfies migration 382's substring
    CHECK, which html_scope's own docstring calls worse than no span.

    Object members only. Each segment is matched as `"segment"` followed by optional
    whitespace and `:`, searched forward from the previous segment's value start, so a parent
    key always precedes its child and a key name occurring as a VALUE cannot match (a value
    is not followed by a colon). None on any miss; callers state their own fallback."""
    if not pointer or pointer == "/":
        return None
    key_start = value_start = -1
    position = 0
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        found = re.compile(
            '"' + re.escape(token) + '"' + _JSON_MEMBER_WS + ":" + _JSON_MEMBER_WS
        ).search(source, position)
        if found is None:
            return None
        key_start, value_start, position = found.start(), found.end(), found.end()
    end = _json_value_end(source, value_start)
    if end is None:
        return None
    return source[key_start:end], source[value_start:end]


def _pointer_parent(first: str, second: str) -> str:
    """Longest common RFC 6901 prefix of two pointers ('' when they share no segment)."""
    left = first.lstrip("/").split("/")
    right = second.lstrip("/").split("/")
    shared: list[str] = []
    for a, b in zip(left, right):
        if a != b:
            break
        shared.append(a)
    return "/" + "/".join(shared) if shared else ""


def _pointer_leaf(pointer: str) -> str:
    return pointer.rstrip("/").rpartition("/")[2]


def _json_literals(value: Any) -> tuple[str, ...]:
    """Both spellings a portal may use for one value: `"Křižíkova"` and its `\\uXXXX`
    escape. mmreality serves the second, idnes the first, and a quote has to match the
    document rather than the parser's preference."""
    plain = json.dumps(value, ensure_ascii=False)
    escaped = json.dumps(value, ensure_ascii=True)
    return (plain,) if plain == escaped else (plain, escaped)


def _json_quote(entry_document: EmbeddedDocument, pointer: str, value: Any) -> str:
    """The narrowest slice of the document's SOURCE that carries this value.

    Ladder, narrowest first: the member at the pointer (only meaningful while the source is
    the literal JSON), then `"leaf": <literal>` anywhere in it, then the literal alone, then
    the captured document itself. The last rung is not a cop-out — for a `js_string` config
    it is exactly what maxima's spec asks for, since the decoded member text appears nowhere
    in the body while the captured literal does."""
    if pointer and entry_document.verbatim:
        member = _json_member_source(entry_document.source, pointer)
        if member is not None:
            return member[0]
    leaf = _pointer_leaf(pointer or "")
    for literal in _json_literals(value):
        if leaf:
            found = re.search(re.escape(f'"{leaf}"') + r"\s*:\s*" + re.escape(literal),
                              entry_document.source)
            if found:
                return found.group(0)
        if literal in entry_document.source:
            return literal
    return entry_document.source


def _coordinate_source_quote(source: str, lon: float, lat: float) -> str | None:
    """The coordinate array AS WRITTEN, so the span points at what was read.

    The value a coordinate reader states ("lat,lon") is assembled and appears nowhere in the
    body. `html_point_attrs` solves that by quoting the node's own serialisation, but this
    node can be a 13 KB map config: an evidence quote rides in the same jsonb array as the
    claim and is counted by `archived_claim_value_bytes`, so quoting the blob would put tens
    of KB on every coordinate claim of the portal. The array literal is ~26 characters,
    verbatim, and genuinely findable."""
    for found in _COORD_ARRAY_RE.finditer(source):
        try:
            first, second = float(found.group(1)), float(found.group(2))
        except ValueError:
            continue
        if first == lon and second == lat:
            return found.group(0)
    return None


def _rejected_point(entry: Entry, lat: float, lon: float) -> bool:
    """Is this pin one the CONTRACT names as junk?

    Calibration data on the contract, never a code constant — the same rule
    `precision_map.blurred_labels` already follows. idnes serves a handful of centroids as if
    they were addresses: 119 active rows on 49.19186,16.61109 and 113 on 49.19752,16.65812
    (Brno centre, street NULL), and 71 rows on 49.81150,15.61824 — the CZ geographic centroid
    — spanning 56 municipalities.

    ENUMERATED, never inferred from pin-sharing: 58 rows on 50.12413,14.12853 are a
    legitimate development cluster, so "many listings share this pin" is a corpus statistic
    for a different lane, not a reject rule. A malformed literal is refused rather than
    skipped, because a junk pin readmitted by a typo is the outcome this list exists to
    stop."""
    declared = entry.locator.get("reject_points")
    if declared is None:
        return False
    if isinstance(declared, str) or not isinstance(declared, (list, tuple)) or not declared:
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares locator.reject_points={declared!r}; "
            f"it must be a non-empty list of 'lat,lon' literals")
    for item in declared:
        head, separator, tail = str(item).partition(",")
        try:
            if not separator:
                raise ValueError(item)
            rejected_lat, rejected_lon = float(head), float(tail)
        except ValueError:
            raise IntakeRefused(
                f"{entry.source}:{entry.entry_id} declares reject_points entry {item!r}, "
                f"which is not a 'lat,lon' decimal pair; a junk pin readmitted by a typo is "
                f"exactly what this list exists to stop") from None
        if (round(lat, _REJECT_POINT_DP) == round(rejected_lat, _REJECT_POINT_DP)
                and round(lon, _REJECT_POINT_DP) == round(rejected_lon, _REJECT_POINT_DP)):
            return True
    return False


def _evidenced_optional(
    entry: Entry, row: ListingRow, document: ScopedDocument, *,
    value: str, within: Any, quote: str | None, **overrides: Any,
) -> Claim:
    """`_evidenced`, except that a quote the scoped body cannot show is DROPPED.

    An `evidence_quote` is a promise the payload contains that text — 01 §4.2 pairs it with
    `payload_sha256` for exactly that reason. A value this reader cannot point at has no
    honest quote, and asserting one anyway is worse than asserting none:
    `assert_evidence_complete` REQUIRES the evidence set only for `llm_text`/`regex_text`, so
    a `map_widget_parse` claim may legally carry a value with no span."""
    if quote is None:
        return _base(
            entry, row, value_text=value,
            subject_scoped=bool(entry.subject_scope.get("subject_scoped", True)),
            **overrides)
    return _evidenced(entry, row, document, value=value, within=within, quote=quote,
                      **overrides)


@archive_reader("json_scalar")
def _read_json_scalar(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """One scalar at a JSON pointer inside the document this page carries.

    Two shapes, one reader, because the difference is contract data and not code: a plain
    pointer (`json_pointer: /mtMapOptions/zoom`, `/infoText`, `/zoom`), or a subject-matched
    one (`then: /geojson/features` + `match` + `json_pointer: /properties/address`). It does
    NOT stamp `declared_precision_label` on any claim type: idnes' `infoText` is a two-valued
    Czech SENTENCE, not a label, and mapping a sentence to a label is contract calibration
    (`html_marker` plus `blurred_labels`), not something a generic scalar reader may
    invent."""
    subject = _subject_object(entry, row, embedded_documents(entry, document))
    if subject is None:
        return []
    found_document, obj = subject
    pointer = str(entry.locator.get("json_pointer") or "")
    found = json_pointer(obj, pointer) if pointer else obj
    value = apply_transforms(_text(found), entry.transform)
    if value is None:
        return []
    number = _number(found) if entry.locator.get("value_kind") == "num" else None
    claim = _evidenced_optional(
        entry, row, document, value=value, within=found_document.node,
        quote=_json_quote(found_document, pointer, found), value_num=number)
    return [ArchiveRead(claim)]


@archive_reader("json_regex")
def _read_json_regex(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """One capture group of a pattern run over a STRING member of the embedded document.

    mmreality's `ul. <Street>` inside `originalTitle`: `raw_json.street` is populated on 1/12
    sampled rows while the title carries the street on 5/12.

    The regex runs over the DECODED string — a pattern must not have to know the portal's
    escaping — while the QUOTE is the member's SOURCE slice, because the decoded capture is
    not a substring of the scoped payload and a quote that cannot be located is a claim
    asserting evidence it cannot point at. The member and not the capture alone: the escaped
    street also occurs in `title`, `location` and `slug`, and `find_span` takes the first
    occurrence.

    FIRST match only, and a missing span emits nothing rather than raising: `regex_text` is
    evidence-bearing, so a span-less claim would reach `assert_evidence_complete` and take
    the whole batch with it."""
    compiled, group = _entry_pattern(entry, "json_regex")
    subject = _subject_object(entry, row, embedded_documents(entry, document))
    if subject is None:
        return []
    found_document, obj = subject
    pointer = str(entry.locator.get("json_pointer") or "")
    member = json_pointer(obj, pointer) if pointer else obj
    text = _text(member)
    if text is None:
        return []
    match = compiled.search(text)
    if match is None:
        return []
    value = apply_transforms(_text(match.group(group)), entry.transform)
    if value is None:
        return []
    claim = _evidenced(entry, row, document, value=value, within=found_document.node,
                       quote=_json_quote(found_document, pointer, member))
    if claim.span_start is None or claim.span_end is None:
        return []
    return [ArchiveRead(claim)]


@archive_reader("json_bool")
def _read_json_bool(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """A portal's own BOOLEAN precision flag, mapped to the label the contract gives it.

    mmreality's `accurate` is the case that shaped it: 3,917 of 10,538 active rows are
    `accurate:false`, that cohort shares a pin 49.8% of the time against 13.2% for `true`,
    and a cap read off a different blob than the coordinate it caps is not a cap — which is
    why this reads the SUBJECT's document through the same selector the coordinate does.

    The blur axis is decided identically to `declared_quality`: membership of the mapped
    label in the contract's `precision_map.blurred_labels`. Which label means blurred is a
    portal fact, so re-calibrating it is a version bump, not a code change, and the axis is
    written EXPLICITLY rather than defaulted (06 §6.6 rule 7)."""
    labels = entry.locator.get("labels")
    if not isinstance(labels, Mapping) or not labels.get("true") or not labels.get("false"):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} uses `json_bool` but declares "
            f"locator.labels={labels!r}; both the true and the false label are contract "
            f"data — a boolean with one name states nothing about the other branch")
    subject = _subject_object(entry, row, embedded_documents(entry, document))
    if subject is None:
        return []
    found_document, obj = subject
    pointer = str(entry.locator.get("json_pointer") or "")
    found = json_pointer(obj, pointer) if pointer else obj
    if not isinstance(found, bool):
        return []
    label = str(labels["true" if found else "false"])
    blurred = {str(x) for x in (entry.precision_map.get("blurred_labels") or [])}
    claim = _evidenced_optional(
        entry, row, document, value=label, within=found_document.node,
        quote=_json_quote(found_document, pointer, found),
        declared_precision_label=label, value_num=1.0 if found else 0.0,
        blur_evidence="declared" if label in blurred else "none")
    return [ArchiveRead(claim)]


@archive_reader("json_point")
def _read_json_point(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """A coordinate out of the embedded document, in either of the two shapes portals use.

    POINTER PAIR (`lat_pointer` + `lon_pointer`) is mmreality's `point{latitude,longitude}`:
    the axis order is not derivable from the document, so it is contract data. GEOJSON
    (`feature`, a pointer to the geometry inside the selected object) is idnes': RFC 7946
    fixes `[lon, lat]`, so there the order is the FORMAT and must NOT be taken as data. A
    geometry that is not a Point yields nothing — a marked area is a different claim type and
    reading its first vertex as a pin would be a fabrication.

    `value_text` is the SOURCE digits, never a re-rounded float: mmreality publishes 9 dp and
    that is spurious precision we store verbatim and cap elsewhere.

    Three refusals, each a different failure: a declared junk pin (`reject_points`, contract
    data), the CZ envelope (`guard_admits`, genuinely evaluated — 16,833 active idnes rows
    sit outside it), and non-finite floats (structural: `POINT(nan nan)` either stores a
    non-finite geometry in an append-only table or aborts the whole batch INSERT around
    it)."""
    branch = _coordinate_branch(entry)
    subject = _subject_object(entry, row, embedded_documents(entry, document))
    if subject is None:
        return []
    found_document, obj = subject
    feature = entry.locator.get("feature")
    lat_pointer = entry.locator.get("lat_pointer")
    lon_pointer = entry.locator.get("lon_pointer")
    if feature:
        geometry = json_pointer(obj, str(feature))
        if not isinstance(geometry, dict) or geometry.get("type") != "Point":
            return []
        pair = geometry.get("coordinates")
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            return []
        raw_lon, raw_lat = _text(pair[0]), _text(pair[1])
        lat, lon = _number(pair[1]), _number(pair[0])
        quote = (_coordinate_source_quote(found_document.source, lon, lat)
                 if lat is not None and lon is not None else None)
        quote = quote or _json_quote(found_document, str(feature), geometry)
    elif lat_pointer and lon_pointer:
        raw_lat = _text(json_pointer(obj, str(lat_pointer)))
        raw_lon = _text(json_pointer(obj, str(lon_pointer)))
        lat, lon = _number(raw_lat), _number(raw_lon)
        parent = _pointer_parent(str(lat_pointer), str(lon_pointer))
        member = (_json_member_source(found_document.source, parent)
                  if parent and found_document.verbatim else None)
        quote = member[0] if member else _json_quote(
            found_document, str(lat_pointer), json_pointer(obj, str(lat_pointer)))
    else:
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} uses `json_point` but names neither a "
            f"`locator.feature` pointer (a GeoJSON geometry, axis order fixed by RFC 7946) "
            f"nor a `locator.lat_pointer`/`lon_pointer` pair (axis order contract data)")
    if lat is None or lon is None or raw_lat is None or raw_lon is None:
        return []
    if not (isfinite(lat) and isfinite(lon)):
        return []
    if _rejected_point(entry, lat, lon):
        return []
    if not guard_admits(entry, GUARD_CZ_BBOX, (lat, lon)):
        return []
    claim = _evidenced_optional(
        entry, row, document, value=f"{raw_lat},{raw_lon}", within=found_document.node,
        quote=quote, value_geom_wkt=point_wkt(lat, lon))
    return [ArchiveRead(claim, position_branch=branch)]


_EARTH_RADIUS_M = 6371008.8
# 02 §2.2.9's geometry ladder, and the one number in it that is a convention rather than a
# measurement: maxima ships a Circle radius in DEGREES and the contract's own
# `precision_caps.feature_circle.uncertainty_radius_m` names the conversion
# (`radius_deg_times_111000`). Reproduces the recon exactly — 0.01225° -> 1360 m (observed
# "1.36 km"), 0.02032° -> 2255 m (observed "2.26 km").
_DEG_TO_M = 111000.0


@dataclass(frozen=True, slots=True)
class MapGeometry:
    """What a map config DECLARED, as the four things a claim needs from it."""
    kind: str                    # 'Point' | 'LineString' | 'Circle', verbatim
    lat: float
    lon: float                   # the representative point
    shape_wkt: str | None        # None for Point: a point declares no uncertainty shape
    radius_m: float | None


def _lon_lat(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        lon, lat = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    return (lon, lat) if isfinite(lon) and isfinite(lat) else None


def _segment_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    mid = radians((first[1] + second[1]) / 2.0)
    dx = radians(second[0] - first[0]) * cos(mid) * _EARTH_RADIUS_M
    dy = radians(second[1] - first[1]) * _EARTH_RADIUS_M
    return hypot(dx, dy)


def _openlayers_geometry(feature: Any) -> MapGeometry | None:
    """The `geometry_reader: openlayers` ladder — the feature TYPE is the declared precision.

    The observed shape is a BARE geometry object (`{type, coordinates}`), not a GeoJSON
    `Feature` wrapper; the wrapper is unwrapped defensively because a theme change is likelier
    than a schema the recon got wrong."""
    if not isinstance(feature, dict):
        return None
    if str(feature.get("type")) == "Feature":
        feature = feature.get("geometry")
        if not isinstance(feature, dict):
            return None
    kind = str(feature.get("type") or "")
    coordinates = feature.get("coordinates")
    if kind == "Point":
        point = _lon_lat(coordinates)
        return None if point is None else MapGeometry(
            "Point", point[1], point[0], None, None)
    if kind == "LineString":
        if not isinstance(coordinates, list):
            return None
        vertices = [p for p in (_lon_lat(c) for c in coordinates) if p is not None]
        if len(vertices) != len(coordinates) or len(vertices) < 2:
            return None
        lengths = [_segment_m(vertices[i], vertices[i + 1])
                   for i in range(len(vertices) - 1)]
        total = sum(lengths)
        walked = 0.0
        lon, lat = vertices[0]
        for index, length in enumerate(lengths):
            if walked + length >= total / 2.0 or index == len(lengths) - 1:
                fraction = 0.5 if length <= 0 else (total / 2.0 - walked) / length
                fraction = min(max(fraction, 0.0), 1.0)
                (x0, y0), (x1, y1) = vertices[index], vertices[index + 1]
                lon, lat = x0 + (x1 - x0) * fraction, y0 + (y1 - y0) * fraction
                break
            walked += length
        wkt = "LINESTRING(" + ", ".join(f"{x!r} {y!r}" for x, y in vertices) + ")"
        return MapGeometry("LineString", lat, lon, wkt, total / 2.0)
    if kind == "Circle":
        # `coordinates` OR `center`: the recon recorded the circle's VALUES but never its
        # keys, and OpenLayers has no single serialisation for `ol/geom/Circle`.
        point = _lon_lat(coordinates if coordinates is not None else feature.get("center"))
        try:
            radius_deg = float(feature.get("radius"))
        except (TypeError, ValueError):
            return None
        if point is None or not isfinite(radius_deg) or radius_deg <= 0:
            return None
        return MapGeometry("Circle", point[1], point[0],
                           f"POINT({point[0]!r} {point[1]!r})", radius_deg * _DEG_TO_M)
    return None


@archive_reader("json_geometry")
def _read_json_geometry(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """A map feature TYPED — the one reader for both halves of what a drawn geometry states.

    maxima draws the subject as a Point, a LineString along the street, or a Circle, and the
    TYPE is the declared precision. So one entry (`claim_type: coordinate`) takes the
    representative position — the point, the linear-referenced midpoint of the segment, the
    circle's centre — and a second (`claim_type: uncertainty_geometry`) takes the shape
    itself plus the radius it declares: half the polyline length, or radius° × 111 000. A
    Point emits nothing on the shape arm; migration 383's class default is the honest bound
    there.

    An EMPTY `features` array emits nothing at all, and structurally rather than by a guard
    the contract has to remember to name: the entry's own `then` pointer misses, so there is
    no geometry to type. That is why v1's `reject_empty_geometry` guard is dropped rather than
    implemented — a guard is `(lat, lon) -> bool` and there is no point to hand it.

    The zoom rail is the second refusal and a different failure: a page that DOES carry a
    feature but is drawn at a regional zoom (`d40031686` serves a centre 9.2 km from its
    stored pin, in a different okres, at zoom 10.20). `reject_zoom_at_or_below` is contract
    data; the pointer to the zoom is a property of the config FORMAT, which is why it
    defaults.

    The map's VIEW CENTRE is never read here, on any branch — it is 130 m and 660 m from the
    circle centre on the two Circle rows and 9.2 km out on the empty-features one."""
    reader_name = str(entry.locator.get("geometry_reader") or "openlayers")
    if reader_name != "openlayers":
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares geometry_reader={reader_name!r}; "
            f"this lane implements 'openlayers'")
    coordinate = entry.claim_type == "coordinate"
    branch = _coordinate_branch(entry) if coordinate else None
    subject = _subject_object(entry, row, embedded_documents(entry, document))
    if subject is None:
        return []
    found_document, feature = subject
    floor = entry.locator.get("reject_zoom_at_or_below")
    if floor is not None:
        try:
            zoom_floor = float(floor)
        except (TypeError, ValueError):
            raise IntakeRefused(
                f"{entry.source}:{entry.entry_id} declares "
                f"reject_zoom_at_or_below={floor!r}, which is not a number") from None
        zoom = _number(json_pointer(found_document.data,
                                    str(entry.locator.get("zoom_pointer") or "/zoom")))
        if zoom is not None and zoom <= zoom_floor:
            return []
    geometry = _openlayers_geometry(feature)
    if geometry is None:
        return []
    if not guard_admits(entry, GUARD_CZ_BBOX, (geometry.lat, geometry.lon)):
        return []
    pointer = str(entry.locator.get("then") or "")
    quote = _json_quote(found_document, pointer, feature)
    # 06 §6.6 rule 7 lets exactly one thing set this, and a Circle is it: the portal is
    # drawing its own imprecision, the one sanctioned case where blur rides on the
    # coordinate rather than on a separate declaration.
    blur = "declared" if geometry.kind == "Circle" else entry.default_blur_evidence
    if coordinate:
        claim = _evidenced_optional(
            entry, row, document, value=f"{geometry.lat!r},{geometry.lon!r}",
            within=found_document.node, quote=quote,
            value_geom_wkt=point_wkt(geometry.lat, geometry.lon),
            declared_precision_label=geometry.kind.lower(), blur_evidence=blur)
        return [ArchiveRead(claim, position_branch=branch)]
    if geometry.shape_wkt is None:
        return []
    claim = _evidenced_optional(
        entry, row, document, value=geometry.kind, within=found_document.node, quote=quote,
        value_shape_wkt=geometry.shape_wkt,
        declared_radius_m=(None if geometry.radius_m is None
                           else round(geometry.radius_m, 1)),
        declared_precision_label=geometry.kind.lower(), blur_evidence=blur,
        # WHICH arm of the ladder produced `declared_radius_m`. Migration 383 says the radius
        # is 'declared' and to read it off this claim; it does not say how it was derived,
        # and a metre count with no basis is not auditable.
        value_jsonb={"geometry_type": geometry.kind,
                     "radius_basis": ("radius_deg_times_111000" if geometry.kind == "Circle"
                                      else "half_segment_length")})
    return [ArchiveRead(claim)]


# The geo chain of a schema.org BreadcrumbList, as OFFSETS FROM THE KRAJ rather than
# absolute positions: the offset moves with the category path (realitymix's
# `domy/pronajem` chain starts at position 4, `byty/2+1/pronajem` at 5), so an entry
# declaring `positions: [5,6,7,8]` is wrong on every two-level category. A named level, not
# an integer, because `_check_executable` requires a declared locator key to be TRUTHY and
# `offset: 0` (the kraj) would be refused.
BREADCRUMB_LEVELS = {"kraj": 0, "okres": 1, "obec": 2, "quarter": 3}


def _jsonld_blocks(data: Any) -> list[dict[str, Any]]:
    """Every schema.org block a JSON-LD document carries — bare, in a list, or under
    `@graph`."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    graph = data.get("@graph")
    if isinstance(graph, list):
        return [data] + [item for item in graph if isinstance(item, dict)]
    return [data]


def _breadcrumb_items(block: Mapping[str, Any]) -> list[tuple[str, str]]:
    """`(name, id url)` per list element, in `position` order.

    Both schema.org shapes: the name and `@id` live either on a nested `item` object (what
    realitymix's live page serves) or flat on the element itself (what the pinned fixture
    carries). An element with no usable name is dropped rather than counted, so a level can
    never be silently off by one."""
    elements = block.get("itemListElement")
    if not isinstance(elements, list):
        return []
    ordered: list[tuple[int, str, str]] = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue
        item = element.get("item")
        carrier = item if isinstance(item, dict) else element
        name = _text(carrier.get("name"))
        if name is None:
            continue
        url = _text(carrier.get("@id")) or _text(element.get("@id")) or ""
        try:
            position = int(element.get("position", index))
        except (TypeError, ValueError):
            position = index
        ordered.append((position, name, url))
    ordered.sort(key=lambda item: item[0])
    return [(name, url) for _position, name, url in ordered]


@archive_reader("json_breadcrumb")
def _read_json_breadcrumb(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[ArchiveRead]:
    """One typed level of a JSON-LD breadcrumb's geo chain, anchored on the kraj slug.

    realitymix publishes `Plzeňský kraj -> Plzeň-město -> Plzeň -> Skvrňany`, each with a
    stable slug path, and `category_from_breadcrumb` parses those slugs and throws them away.
    The chain's OFFSET is not stable — it starts one position later on a three-level category
    path — so the kraj slug set the contract declares is the anchor and the level is counted
    forward from it.

    FAILS CLOSED: no anchor, no claim. An unverified kraj slug then costs coverage, never
    correctness, and a per-kraj claim rate of zero is what identifies the wrong slug."""
    level = entry.locator.get("level")
    if level not in BREADCRUMB_LEVELS:
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares level={level!r}; `json_breadcrumb` "
            f"claims one of {sorted(BREADCRUMB_LEVELS)}")
    wanted = entry.locator.get("type")
    if not wanted or not isinstance(wanted, str):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} uses `json_breadcrumb` but names no "
            f"`locator.type` (the schema.org @type to read, got {wanted!r})")
    slugs = entry.locator.get("anchor_slugs")
    if not isinstance(slugs, (list, tuple)) or not slugs or not all(slugs):
        raise IntakeRefused(
            f"{entry.source}:{entry.entry_id} declares anchor_slugs={slugs!r}; the chain's "
            f"offset moves with the category path, so the anchor is contract data")
    anchors = {str(slug).strip().lower() for slug in slugs}
    offset = BREADCRUMB_LEVELS[str(level)]
    for found_document in embedded_documents(entry, document):
        for block in _jsonld_blocks(found_document.data):
            if block.get("@type") != wanted:
                continue
            items = _breadcrumb_items(block)
            anchor = next(
                (index for index, (_name, url) in enumerate(items)
                 if url and url.rstrip("/").rsplit("/", 1)[-1].lower() in anchors),
                None)
            if anchor is None or anchor + offset >= len(items):
                return []
            value = apply_transforms(items[anchor + offset][0], entry.transform)
            if value is None:
                return []
            return [ArchiveRead(_evidenced(entry, row, document, value=value,
                                           within=found_document.node))]
    return []


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
    if claim.extraction_method == LLM_METHOD:
        unattributed = [
            name for name, value in (("model", claim.model),
                                     ("prompt_version", claim.prompt_version))
            if value is None
        ]
        if unattributed:
            raise IntakeRefused(
                f"{claim.extractor_id} produced an llm_text claim without "
                f"{', '.join(unattributed)}; 01 §4.2's loc_claim_llm_model refuses a model "
                f"assertion that cannot name the model that made it")


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
    claim: Claim, row: ListingRow, entry: Entry, branch: str | None,
) -> tuple[Claim | None, str]:
    """The archived arm of the licence ladder, applied to a coordinate claim.

    The READER declares which branch of the page it read (`ArchiveRead.position_branch`)
    and the LADDER stamps the class — never the other way round. Whatever `licence_class`
    the reader left on the claim is DISCARDED here: a reader that stamps `'portal'` on the
    Nominatim branch gets `'odbl'` anyway, so C6 is decided once, in
    `ARCHIVED_COORDINATE_RULES`, instead of re-litigated in nine portal readers."""
    if branch not in POSITION_BRANCHES:
        raise IntakeRefused(
            f"{entry.entry_id} returned a coordinate without a position_branch "
            f"(got {branch!r}, expected one of {sorted(POSITION_BRANCHES)}). Which branch "
            f"of the portal's map produced a position IS its licence class (C6) and only "
            f"the reader knows it — it is never inferred from what the claim was stamped "
            f"with")
    verdict = coordinate_verdict(
        row.source, None, in_mapy_inventory=row.in_mapy_inventory,
        substrate=SUBSTRATE_ARCHIVED_HTML, entry_id=entry.entry_id,
        portal_pin_present=branch == POSITION_BRANCH_PORTAL_PIN)
    if not verdict.admitted or verdict.licence_class is None:
        return None, verdict.reason
    return replace(claim, licence_class=verdict.licence_class), verdict.reason


def archived_claim_value_bytes(claim: Claim) -> int:
    """`claim_value_bytes` PLUS the evidence quote, and the quote counts on purpose.

    W1's cap exists so one claim array cannot exceed Postgres's 256 MB jsonb limit, and
    `claim_value_bytes` measures the value columns because on W1's substrate they are the
    only unbounded ones. `evidence_quote` rides in the SAME `jsonb_to_recordset` array
    (`Claim.to_row()`), it is NULL on every W1 claim, and on this substrate it is a span of
    an HTML body 41-245 KB long — so leaving it out would exempt the one field most likely
    to blow the bound from the bound written to stop it."""
    total = claim_value_bytes(claim)
    if claim.evidence_quote is not None:
        total += len(claim.evidence_quote.encode("utf-8"))
    return total


def _refuse_oversized_archived(
    row: ListingRow, claim: Claim, *, max_value_bytes: int,
) -> Absence | None:
    """The cap, applied to one archived claim. None means keep it.

    An absence and NO refetch-cohort row, which is where this parts company with W1's
    `_refuse_oversized`. That one enrols the listing in `{source}_detail_refetch` because a
    truncated `raw_json` really can be repaired by fetching the page again. An archived body
    is immutable and content-addressed: re-reading it yields the same oversized value
    forever, so a refetch row would be a permanently-failing attempt counter. What fixes
    this is a narrower locator or a transform in the contract — a reviewed change, not a
    retry — and the absence is the durable record that says so (03 §3.2: a dropped claim and
    a claim never produced must not be indistinguishable)."""
    size = archived_claim_value_bytes(claim)
    if size <= max_value_bytes:
        return None
    detail = (f"{claim.claim_type} value from {claim.extractor_id} is {size} bytes "
              f"(cap {max_value_bytes}) on the archived body; refused — an archived body is "
              f"immutable, so this needs a narrower locator, not a refetch")
    LOG.warning("REMINE-ARCHIVE oversized value refused listing_id=%d source=%s "
                "claim_type=%s extractor_id=%s bytes=%d cap=%d",
                row.listing_id, row.source, claim.claim_type, claim.extractor_id,
                size, max_value_bytes)
    return Absence(
        listing_id=row.listing_id, surface=ARCHIVE_SURFACE, field_=claim.claim_type,
        reason="not_attempted", extraction_method=claim.extraction_method, detail=detail)


def extract_payload(
    payload: ArchivedPayload,
    row: ListingRow,
    entries: list[Entry],
    *,
    register: ScopeRegister,
    max_value_bytes: int | None = None,
) -> IntakeResult:
    """Everything this lane knows about one archived body. Pure — no DB, no clock, no
    network."""
    if max_value_bytes is None:
        max_value_bytes = env_positive_int(MAX_CLAIM_VALUE_BYTES_ENV,
                                           DEFAULT_MAX_CLAIM_VALUE_BYTES)
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
        try:
            reads = ARCHIVE_READERS[str(entry.reader)](entry, row, payload, document)
        except SubjectNotFound as miss:
            # `on_miss: fail` — an id-matched reader looked and found no object that is this
            # listing's. RECORDED, not swallowed: without the absence, "the portal changed
            # its id scheme" and "this page carried no address" are the same green
            # zero-claim sweep, and the batch still stamps 'ok' and moves the watermark.
            LOG.info("REMINE-ARCHIVE subject miss listing_id=%d source=%s entry=%s %s",
                     row.listing_id, row.source, entry.entry_id, miss)
            result.absences.append(Absence(
                listing_id=row.listing_id, surface=ARCHIVE_SURFACE,
                field_=entry.claim_type, reason="not_attempted",
                extraction_method=entry.extraction_method, detail=str(miss)))
            continue
        for read in reads:
            claim = stamp_archive_claim(
                read.claim, payload, scope_version=document.scope_version)
            if claim.claim_type != "coordinate" and read.position_branch is not None:
                raise IntakeRefused(
                    f"{entry.entry_id} declared position_branch="
                    f"'{read.position_branch}' on a {claim.claim_type} read; the branch is "
                    f"a fact about a POSITION's licence lineage and means nothing here")
            if claim.claim_type == "coordinate":
                claim, reason = _licensed_coordinate(
                    claim, row, entry, read.position_branch)
                if claim is None:
                    result.absences.append(Absence(
                        listing_id=row.listing_id, surface=ARCHIVE_SURFACE,
                        field_="coordinate", reason="not_attempted",
                        extraction_method=entry.extraction_method, detail=reason))
                    continue
            assert_stampable(claim)
            assert_evidence_complete(claim)
            refused = _refuse_oversized_archived(
                row, claim, max_value_bytes=max_value_bytes)
            if refused is not None:
                result.absences.append(refused)
                result.oversized += 1
                continue
            result.claims.append(claim)
    return result


# ------------------------------------------------------------------ SQL

# No local relation-existence check for `portal_raw_payloads`: `missing_relations()`
# establishes that migration 382 is applied, and 382 is the migration that CREATES it, so a
# second check could never fail on its own.
#
# THE JOIN IS ON THE PORTAL'S OWN KEY, NOT ON `listing_id`. That column is nullable and
# nothing populates it: `scraper.db.append_payload_if_enabled` passes `listing_id=None`
# ("inventing a listing_id here would mean an extra lookup per page") and
# `payload_backfill._INSERT_SQL` selects `NULL::bigint` for all 445k migrated pages. An
# inner join on it therefore matches ZERO rows over the whole archive — and this lane would
# not have raised: the first batch would come back empty, `reached_end` would trip, the
# batch would stamp 'ok' and the watermark would claim coverage of a corpus never opened.
# `(source, source_id_native)` is what both writers populate, is the store's own uniqueness
# key, and is UNIQUE on `listings` too (`listings_source_native_uidx`, migration 091), so
# the join stays 1:1.
#
# ONLY OK BODIES ARE MINED, matching `payloads._PRUNE_SQL`'s own ranking (which sorts
# `http_status IS NULL OR BETWEEN 200 AND 299` first — migration 403 cites idnes' 503
# interstitial as the real case). Filtering rather than merely re-ranking is the stronger
# form: a group whose only bodies are error pages is honestly out of scope instead of being
# mined for claims an interstitial cannot carry. The anti-join carries the same predicate,
# so a newer 503 can never veto the 200 underneath it.
#
# "Latest body per (source, source_id_native, page_kind)" is then an anti-join on
# `(first_observed_at, id)` rather than on `version_seq`: 403 added that counter with no
# backfill, so every body written before it is NULL there and a `>` comparison against NULL
# would silently rank the older row as the latest. `first_observed_at` is NOT NULL from 382,
# is monotonic per key by construction (a new body is first observed after the one it
# replaced), and `prp_native (source, source_id_native, page_kind, first_observed_at desc)`
# indexes exactly this predicate. `id` breaks the ties a bare timestamp sort would reshuffle.
#
# The scan projects NO body and NO `listings.raw_json`. The body is fetched separately and
# only for the rows an entry actually applies to (`_PAYLOAD_BODIES_SQL`), because the whole
# archive is ~14 GB and a scan that materialises it to discover it has no reader is the
# exact failure W2-0's denominator query was written to avoid — a cost that only went up
# when W2a moved the bytes to R2, since detoasting a row became a round trip to a bucket.
# `raw_json` is W1's substrate and this lane never reads it, so a `ListingRow` carries `{}`.
_PAYLOAD_SCAN_FULL_SQL = """
    SELECT p.id, p.source, p.source_id_native, p.page_kind::text,
           encode(p.payload_sha256, 'hex'), p.first_observed_at,
           l.id, (a.listing_id IS NOT NULL) AS in_mapy_inventory
    FROM portal_raw_payloads p
    JOIN listings l ON l.source = p.source AND l.source_id_native = p.source_id_native
    LEFT JOIN mapy_affected a ON a.listing_id = l.id
    WHERE p.id > %(after_id)s
      AND p.source = %(source)s
      AND (p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299)
      AND NOT EXISTS (
          SELECT 1 FROM portal_raw_payloads n
          WHERE n.source = p.source
            AND n.source_id_native = p.source_id_native
            AND n.page_kind = p.page_kind
            AND (n.http_status IS NULL OR n.http_status BETWEEN 200 AND 299)
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
    JOIN listings l ON l.source = p.source AND l.source_id_native = p.source_id_native
    LEFT JOIN mapy_affected a ON a.listing_id = l.id
    WHERE p.first_observed_at >= %(watermark)s
      AND (p.first_observed_at, p.id) > (%(after_ts)s, %(after_id)s)
      AND p.source = %(source)s
      AND (p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299)
      AND NOT EXISTS (
          SELECT 1 FROM portal_raw_payloads n
          WHERE n.source = p.source
            AND n.source_id_native = p.source_id_native
            AND n.page_kind = p.page_kind
            AND (n.http_status IS NULL OR n.http_status BETWEEN 200 AND 299)
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
    cur: psycopg.Cursor, payload_ids: list[int], *, store: BodyStore | None,
) -> tuple[dict[int, bytes], int]:
    """The bodies for one batch's applicable rows, decoded. Returns (bodies, from_r2).

    Takes the batch's OWN cursor rather than opening a transaction of its own: the whole
    batch is one all-or-nothing transaction, and a nested `guarded()` here would only add a
    savepoint around a read.

    R2 IS WHERE THE BODIES LIVE, not an exceptional path. The threshold shipped at 256 KB,
    where nothing spilled and the archive was database-resident; it is 2 KB now
    (`payloads.DEFAULT_R2_THRESHOLD_BYTES`, migration 406's header) — Postgres's own TOAST
    boundary — so `body IS NULL AND body_r2_key IS NOT NULL` holds on essentially every
    row. A version of this that counted those and moved on would mine an empty corpus and
    report success. `store` is therefore required whenever a row is spilled, and a spilled
    row with no store is an error, not a skipped page."""
    if not payload_ids:
        return {}, 0
    bodies: dict[int, bytes] = {}
    from_r2 = 0
    cur.execute(_PAYLOAD_BODIES_SQL, {"ids": payload_ids})
    for payload_id, body, body_r2_key, content_encoding in cur.fetchall():
        encoding = content_encoding or "identity"
        if body is not None:
            bodies[int(payload_id)] = payloads.decode_body(bytes(body), encoding)
            continue
        if not body_r2_key:
            # `prp_body_present` (382) forbids this: exactly one of the two is always set.
            continue
        if store is None:
            raise IntakeRefused(
                f"payload {payload_id} holds its body in R2 (body_r2_key={body_r2_key}) and "
                f"no object store is configured; set the R2_* env vars — mining the "
                f"database-resident rows alone would report coverage over a corpus that is "
                f"almost entirely in the bucket")
        bodies[int(payload_id)] = payloads.decode_body(
            store.download_bytes(body_r2_key), encoding)
        from_r2 += 1
    return bodies, from_r2


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
    store: BodyStore | None = None,
) -> dict[str, Any]:
    """Preflight, then ONE BATCH PER READABLE SOURCE, sharing one budget.

    Per-source and not one pass over everything, because `location_claim_batches`'
    resume/watermark is keyed on `(lane, source, scan_mode)` and a batch stamped 'ok' under
    a NULL source is a claim of coverage over all nine portals. Readers arrive one portal at
    a time (W2-6…W2-12), so a single-reader run under a NULL key would stamp a watermark the
    NEXT portal's first run then starts behind — mining nothing and reporting success. One
    batch per source means each portal's coverage is its own fact.
    """
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
                "readable_sources": [], "per_source": {}}

    if start_after_id > 0 and len(readable) > 1:
        raise IntakeRefused(
            f"--start-after-id anchors ONE source's keyset and {len(readable)} are readable "
            f"({', '.join(readable)}); pass --source too so the anchor names the scan it "
            f"belongs to")

    registers = load_registers(conn)
    # Opened only past the inert return: a lane with no reader has no body to fetch, so it
    # must not require R2 credentials to establish that it has nothing to do.
    if store is None:
        store = payloads.open_store()

    LOG.info("REMINE-ARCHIVE start mode=%s batch=%d inventory_rows=%d readable=%s",
             mode, batch_size, inventory_rows, ",".join(readable))

    # ONE budget across the sources, not one each: `--max-seconds` is the workflow's wall
    # clock and `--limit` is the operator's bound on a trial run. A per-source copy of
    # either would multiply the run by the number of portals with a reader.
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    remaining = limit

    totals: dict[str, Any] = {
        "payloads": 0, "claims": 0, "claims_inserted": 0, "observations": 0,
        "enqueued": 0, "absences": 0, "bodies_from_r2": 0, "oversized_values": 0,
    }
    per_source: dict[str, dict[str, Any]] = {}
    for scan_source in readable:
        if remaining is not None and remaining <= 0:
            break
        if deadline is not None and time.monotonic() > deadline:
            break
        stats = _run_source(
            conn, source=scan_source, mode=mode, batch_size=batch_size, deadline=deadline,
            limit=remaining, start_after_id=start_after_id, overlap_hours=overlap_hours,
            statement_timeout=statement_timeout, dry_run=dry_run, note=note, store=store,
            entries=entries_by_source[scan_source], register=registers.get(scan_source))
        per_source[scan_source] = stats
        for key in totals:
            totals[key] += stats[key]
        if remaining is not None:
            remaining -= stats["payloads"]

    result: dict[str, Any] = dict(totals)
    # 'ok' only when every readable source reached the end of its own scan. A source the
    # budget never reached has not been covered, and the aggregate must not say otherwise.
    result["reached_end"] = (
        len(per_source) == len(readable)
        and all(s["reached_end"] for s in per_source.values()))
    result["stopped_early"] = (
        len(per_source) < len(readable)
        or any(s["stopped_early"] for s in per_source.values()))
    result["outcome"] = "ok" if result["reached_end"] else "stopped"
    result["mode"] = mode
    result["readable_sources"] = readable
    result["per_source"] = per_source
    result["batch_ids"] = [s["batch_id"] for s in per_source.values()]
    if len(readable) == 1:
        only = per_source.get(readable[0], {})
        result["batch_id"] = only.get("batch_id")
        result["resumed_from_id"] = only.get("resumed_from_id")
        result["cursor_after_id"] = only.get("cursor_after_id")
    return result


def _run_source(
    conn: psycopg.Connection,
    *,
    source: str,
    mode: str,
    batch_size: int,
    deadline: float | None,
    limit: int | None,
    start_after_id: int,
    overlap_hours: int,
    statement_timeout: int,
    dry_run: bool,
    note: str | None,
    store: BodyStore | None,
    entries: list[Entry],
    register: ScopeRegister | None,
) -> dict[str, Any]:
    """One source's batch, from its own resume point to its own watermark."""
    contract_id: int | None = None
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
                     "incremental degrades to a full pass", source)
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
                     "after_id=%d after_ts=%s", mode, source, after_id,
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

    stats: dict[str, Any] = {
        "payloads": 0, "claims": 0, "claims_inserted": 0, "observations": 0,
        "enqueued": 0, "absences": 0, "bodies_from_r2": 0, "oversized_values": 0,
        "stopped_early": False, "reached_end": False, "resumed_from_id": after_id,
        "source": source,
    }
    try:
        while True:
            if limit is not None and stats["payloads"] >= limit:
                stats["stopped_early"] = True
                break
            if deadline is not None and time.monotonic() > deadline:
                LOG.info("REMINE-ARCHIVE stopping %s: --max-seconds reached", source)
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
                    if archive_entries(entries, payload.page_kind)
                ]
                bodies, from_r2 = load_bodies(cur, wanted_bodies, store=store)
                stats["bodies_from_r2"] += from_r2

                result = IntakeResult()
                if register is not None:
                    for payload, row in scanned:
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
            LOG.info("REMINE-ARCHIVE progress source=%s payloads=%d claims=%d inserted=%d "
                     "observed=%d absences=%d oversized=%d from_r2=%d through_id=%d",
                     source, stats["payloads"], stats["claims"], stats["claims_inserted"],
                     stats["observations"], stats["absences"], stats["oversized_values"],
                     stats["bodies_from_r2"], after_id)
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
                        f"bodies_from_r2={stats['bodies_from_r2']}",
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
