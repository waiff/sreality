"""The 14 readers that mine a STORED PAGE BODY, plus the machinery to fetch one.

The hourly lane (`location_data.claims_intake`) reads two substrates: `listings.raw_json`
(its own readers) and the latest `portal_raw_payloads.body` for the listing (these). They
live here, in a module that does NOT import the lane, because the lane imports them —
everything both halves share is `location_data.claims_common`.

A reader takes `(entry, row, payload, scoped_document)` and returns `PageRead`s; the lane
owns the scan, the R2 fetch, the hash gate and the write. The exclusion-zone scoping
(`location_data.html_scope`) fails CLOSED: an incomplete scope admits nothing.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from math import cos, hypot, isfinite, radians
from typing import Any, Protocol
from urllib.parse import unquote

import psycopg

from location_data import payloads
from location_data.claims_common import (
    ARCHIVED_COORDINATE_RULES,
    DEFAULT_MAX_CLAIM_VALUE_BYTES,
    EMITTABLE_LICENCE_CLASSES,
    GUARD_CZ_BBOX,
    MAX_CLAIM_VALUE_BYTES_ENV,
    SUBSTRATE_ARCHIVED_HTML,
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
    guard_admits,
    json_pointer,
    point_wkt,
)
from location_data.html_scope import (
    ScopeRegister, ScopedDocument, collapse_ws, scope_html,
)
from scraper.remax_parser import parse_dms_pair

LOG = logging.getLogger("location_data.page_readers")


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
# per-source `HISTORY_COMPLETENESS` answers a question about a different substrate, and the
# claim's own re-sighting series is gone (rule 25: nobody read it).
ARCHIVE_HISTORY_COMPLETENESS = "none"


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
class PageRead:
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
    genuinely carries no address" would be the same green zero-claim sweep. `extract_page`
    turns it into one `not_attempted` absence per applicable entry, which is the countable
    cohort 03 §3.2 rule 4 asks for."""


PageReaderFn = Callable[
    [Entry, ListingRow, ArchivedPayload, ScopedDocument], list[PageRead]]

# Populated from W2-6 onward. Deliberately a second registry rather than an extension of
# `claims_intake.READERS`: W1's readers take `(entry, row)` and read `raw_json`, these take a
# scoped DOM, and a name present in one but not the other must be a refusal rather than a
# silent no-op. The lane is inert whenever this is empty (see the module docstring).
PAGE_READERS: dict[str, PageReaderFn] = {}


def page_reader(name: str) -> Callable[[PageReaderFn], PageReaderFn]:
    def register(fn: PageReaderFn) -> PageReaderFn:
        PAGE_READERS[name] = fn
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


@page_reader("html_text")
def _read_html_text(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
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
    return [PageRead(_evidenced(entry, row, document, value=value, within=node,
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


@page_reader("html_own_text")
def _read_html_own_text(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
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
    return [PageRead(_evidenced(entry, row, document, value=value, within=node,
                                   quote=_transformed_quote(raw, value)))]


@page_reader("html_attr")
def _read_html_attr(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
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
    return [PageRead(_evidenced(entry, row, document, value=value, within=node,
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


@page_reader("html_regex")
def _read_html_regex(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
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
    return [PageRead(claim)]


@page_reader("html_attr_regex")
def _read_html_attr_regex(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
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
        return [PageRead(_evidenced(entry, row, document, value=value, within=node,
                                       quote=node.html or raw))]
    return []


@page_reader("html_marker")
def _read_html_marker(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
    """A PRESENCE detector: the portal's own marker, typed as the label the contract gives it.

    Three shapes, one reader, because the difference is contract data: a selector alone
    (realitymix's `--estimated` block), a selector plus a literal the node's text must
    contain (idnes' "Nemovitost nemá přesnou adresu…" disclaimer), or a selector plus an
    attribute that must be present (bazos' maps-anchor `title="Přibližná lokalita"`).

    The claim's VALUE is the contract's canonical label and its EVIDENCE is the portal's own
    text or attribute — two different fields for exactly this case, so a portal that rewords
    its sentence stops matching instead of silently restating a different fact under the same
    label. This reader states the label only; `_base` derives the blur axis from that
    label's membership in the entry's `precision_cap.blurred_labels` (W1-c R5), so
    recalibrating which label means "blurred" is a contract version bump and never a code
    change (06 §6.6 rule 7 — the axis is written explicitly, never defaulted).

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
    claim = _evidenced(
        entry, row, document, value=str(label), within=node, quote=evidence,
        declared_precision_label=str(label))
    return [PageRead(claim)]


@page_reader("html_point_dms")
def _read_html_point_dms(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
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
    return [PageRead(claim, position_branch=str(branch))]


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


@page_reader("html_point_attrs")
def _read_html_point_attrs(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
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
    return [PageRead(claim, position_branch=branch)]


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


@page_reader("json_scalar")
def _read_json_scalar(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
    """One scalar at a JSON pointer inside the document this page carries.

    Two shapes, one reader, because the difference is contract data and not code: a plain
    pointer (`json_pointer: /mtMapOptions/zoom`, `/infoText`, `/zoom`), or a subject-matched
    one (`then: /geojson/features` + `match` + `json_pointer: /properties/address`). It
    invents no label of its own: on a `precision_declaration` the portal's own value IS the
    label (W1-c R5, stamped in `_base` for every reader), and whether that label means
    blurred stays contract calibration (`precision_cap.blurred_labels`)."""
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
    return [PageRead(claim)]


@page_reader("json_regex")
def _read_json_regex(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
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
    return [PageRead(claim)]


@page_reader("json_bool")
def _read_json_bool(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
    """A portal's own BOOLEAN precision flag, mapped to the label the contract gives it.

    mmreality's `accurate` is the case that shaped it: 3,917 of 10,538 active rows are
    `accurate:false`, that cohort shares a pin 49.8% of the time against 13.2% for `true`,
    and a cap read off a different blob than the coordinate it caps is not a cap — which is
    why this reads the SUBJECT's document through the same selector the coordinate does.

    This reader states the mapped LABEL; `_base` derives the blur axis from that label's
    membership in the contract's `precision_cap.blurred_labels` (W1-c R5). Which label means
    blurred is a portal fact, so re-calibrating it is a version bump, not a code change, and
    the axis is written EXPLICITLY rather than defaulted (06 §6.6 rule 7)."""
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
    claim = _evidenced_optional(
        entry, row, document, value=label, within=found_document.node,
        quote=_json_quote(found_document, pointer, found),
        declared_precision_label=label, value_num=1.0 if found else 0.0)
    return [PageRead(claim)]


@page_reader("json_point")
def _read_json_point(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
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
    return [PageRead(claim, position_branch=branch)]


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


@page_reader("json_geometry")
def _read_json_geometry(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
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
        return [PageRead(claim, position_branch=branch)]
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
    return [PageRead(claim)]


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


@page_reader("json_breadcrumb")
def _read_json_breadcrumb(
    entry: Entry, row: ListingRow, payload: ArchivedPayload, document: ScopedDocument,
) -> list[PageRead]:
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
            return [PageRead(_evidenced(entry, row, document, value=value,
                                           within=found_document.node))]
    return []



# ------------------------------------------------------------------ extraction

def page_entries(entries: list[Entry], page_kind: str) -> list[Entry]:
    """The entries this lane may execute against ONE archived body.

    Two conditions, and each excludes a different failure: the entry must name a reader
    this lane implements (not the payload half's — see the docstring), and it must be
    declared for the page kind the body actually is (a detail-page selector run over an
    index body is how a neighbour's address becomes the subject's)."""
    return [
        entry for entry in entries
        if entry.reader in PAGE_READERS and entry.page_kind == page_kind
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


def stamp_page_claim(
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
    # W1-c R5 (the `precision_declaration` label + blur axis) is stamped in
    # `claims_common._base`, the one funnel BOTH substrates' readers build a claim through,
    # so a page reader and a payload reader cannot answer it differently.
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

    The READER declares which branch of the page it read (`PageRead.position_branch`)
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
) -> str | None:
    """The cap, applied to one page claim. None means keep it; a string is the refusal.

    No refetch enrolment, which is where this parts company with the payload half's
    `_refuse_oversized`: a truncated `raw_json` really can be repaired by fetching the page
    again, while a stored body is immutable and content-addressed, so re-reading it yields
    the same oversized value forever. What fixes this is a narrower locator or a transform
    in the contract — a reviewed change, not a retry."""
    size = archived_claim_value_bytes(claim)
    if size <= max_value_bytes:
        return None
    LOG.warning("PAGE oversized value refused listing_id=%d source=%s "
                "claim_type=%s extractor_id=%s bytes=%d cap=%d",
                row.listing_id, row.source, claim.claim_type, claim.extractor_id,
                size, max_value_bytes)
    return f"oversized_value:{claim.claim_type}"


def extract_page(
    payload: ArchivedPayload,
    row: ListingRow,
    entries: list[Entry],
    *,
    register: ScopeRegister,
    max_value_bytes: int | None = None,
) -> IntakeResult:
    """Everything this lane knows about one stored page body. Pure — no DB, no clock, no
    network. Refusals are counted on the result, never written as rows."""
    if max_value_bytes is None:
        max_value_bytes = env_positive_int(MAX_CLAIM_VALUE_BYTES_ENV,
                                           DEFAULT_MAX_CLAIM_VALUE_BYTES)
    result = IntakeResult()
    applicable = page_entries(entries, payload.page_kind)
    if not applicable or payload.body is None:
        return result

    document = scope_html(payload.body, register=register)
    if not document.is_complete:
        # `html_scope` fails CLOSED and an incomplete result admits nothing: "the scoper
        # broke" must never read as "no zones matched, extract freely".
        result.refusals["scope_incomplete"] += len(applicable)
        return result

    for entry in applicable:
        try:
            reads = PAGE_READERS[str(entry.reader)](entry, row, payload, document)
        except SubjectNotFound as miss:
            # `on_miss: fail` — an id-matched reader looked and found no object that is this
            # listing's. Counted, not swallowed: without the tally, "the portal changed its
            # id scheme" and "this page carried no address" are the same green zero.
            #
            # DEBUG, not INFO, and counted PER SOURCE. idnes' two subject-scoped entries
            # logged 1 398 INFO lines in one run — a per-listing line for a per-portal
            # fact, which buries everything else the run said. The batch summary prints one
            # line per reason per batch, so `subject_not_found:<source>` is the readout and
            # the per-listing detail is there under `--verbose` when a portal's id scheme
            # actually moves.
            LOG.debug("PAGE subject miss listing_id=%d source=%s entry=%s %s",
                      row.listing_id, row.source, entry.entry_id, miss)
            result.refuse(f"subject_not_found:{row.source}")
            continue
        for read in reads:
            claim = stamp_page_claim(
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
                    result.refuse(reason)
                    continue
            assert_stampable(claim)
            assert_evidence_complete(claim)
            refused = _refuse_oversized_archived(
                row, claim, max_value_bytes=max_value_bytes)
            if refused is not None:
                result.refuse(refused)
                continue
            result.claims.append(claim)
    return result


# ------------------------------------------------------------------ body fetch

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
    workers: int | None = None,
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
    row with no store is an error, not a skipped page.

    ONE BAD OBJECT COSTS ONE LISTING'S PAGE ENTRIES, NEVER THE BATCH. A per-body 404,
    timeout or decode error is warned and dropped from the result; the caller simply finds
    no body for that id, leaves it UNSTAMPED, and the next run retries it. Letting it
    propagate is a wedge, not a safety property: the batch is one transaction, so a single
    missing remax object would roll back the sreality and bezrealitky PAYLOAD claims
    computed beside it, stamp the batch `failed`, leave the watermark (which reads
    `outcome='ok'` only) where it was — and the next hourly run would re-select the same
    object and die the same way, forever, for a body that is simply gone.

    The distinction kept: a spilled row with NO STORE AT ALL still raises. That is a
    misconfigured lane rather than a bad object, and mining only the database-resident rows
    would report coverage over a corpus that is almost entirely in the bucket."""
    if not payload_ids:
        return {}, 0
    bodies: dict[int, bytes] = {}
    spilled: list[tuple[int, str, str]] = []
    # The cursor is DRAINED before a single object is fetched. The batch holds one
    # transaction, so leaving the cursor open across a network fan-out would hold it there
    # too, and psycopg's cursor is not thread-safe in any case.
    cur.execute(_PAYLOAD_BODIES_SQL, {"ids": payload_ids})
    for payload_id, body, body_r2_key, content_encoding in cur.fetchall():
        encoding = content_encoding or "identity"
        if body is not None:
            try:
                bodies[int(payload_id)] = payloads.decode_body(bytes(body), encoding)
            except Exception as exc:  # noqa: BLE001 - one bad body, never the batch
                # The SAME rule the R2 path below follows, and it was missing here: a
                # truncated gzip member or a mis-stamped `content_encoding` on ONE
                # database-resident row would raise out of the fan-out, roll back every
                # portal's payload claims computed beside it, and hand the next run the
                # same immutable row to die on. Dropped from the result, so the caller
                # finds no body, leaves it UNSTAMPED, and retries it next run.
                LOG.warning("PAGE inline body decode failed payload_id=%s encoding=%s: %s",
                            payload_id, encoding, exc)
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
        spilled.append((int(payload_id), str(body_r2_key), encoding))
    if not spilled:
        return bodies, 0

    # Unreachable with store=None: a spilled row with no store raised above, per row.
    assert store is not None

    def fetch(item: tuple[int, str, str]) -> tuple[int, bytes | None]:
        """One GET, decoded. None is a body this run could not read — never an exception:
        the failure is per OBJECT and the transaction it would abort is per BATCH."""
        payload_id, key, encoding = item
        try:
            return payload_id, payloads.decode_body(store.download_bytes(key), encoding)
        except Exception as exc:  # noqa: BLE001 - one bad object must not wedge the lane
            LOG.warning("PAGE body fetch failed payload_id=%d key=%s: %s",
                        payload_id, key, exc)
            return payload_id, None

    def keep(payload_id: int, decoded: bytes | None) -> None:
        if decoded is not None:
            bodies[payload_id] = decoded

    width = payloads.body_fetch_workers() if workers is None else workers
    width = max(1, min(len(spilled), width))
    if width == 1:
        for item in spilled:
            keep(*fetch(item))
        return bodies, len(spilled)
    # ONE GET PER PAGE IS THE WHOLE COST OF A SWEEP (0.7 % of an 844 s run was the
    # database), so the fetch runs wide. Threads, not async: `download_bytes` is a blocking
    # botocore call — botocore clients are documented thread-safe — and both the socket
    # wait and zlib's decompression release the GIL. `pool.map` preserves input order and
    # `fetch` returns rather than raises, so a failure inside the pool is one None in the
    # stream instead of an exception re-raised on consumption.
    with ThreadPoolExecutor(max_workers=width,
                            thread_name_prefix="archive-body") as pool:
        for payload_id, decoded in pool.map(fetch, spilled):
            keep(payload_id, decoded)
    return bodies, len(spilled)
