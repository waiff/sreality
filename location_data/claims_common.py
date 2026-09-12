"""The vocabulary every location claim producer shares — value objects, the licence
ladder, the transform/guard registries, and the pure helpers the readers are built from.

It exists so the ONE claim lane (`location_data.claims_intake`) can read both of its
substrates without a cycle: the page readers (`location_data.page_readers`) need `Claim`,
`Entry`, `ListingRow` and the coordinate ladder, and the lane that calls them needs the
page readers. Nothing here touches a database, a clock or the network.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from location_data import loader_db
from scraper import street


SOURCES = (
    "sreality", "bezrealitky", "bazos", "idnes", "mmreality", "remax", "ceskereality",
    "realitymix", "maxima",
)

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


@dataclass(frozen=True, slots=True)
class CoordinateRule:
    """Where a portal's coordinate legitimately comes from, as data (06 §6.2.1 + §6.1.2).

    `substrate`:
      payload      - the portal published the coordinate in the body we still hold; the
                     value is re-derived from `raw_json` and is first-party (class A).
      geom_column  - the portal published no first-party coordinate in the payload we
                     hold (the six slim-dict portals never wrote lat/lon into raw_json),
                     so on this arm the label answers ONE question — was a coordinate
                     this portal has WITHHELD, and why (the refusal counter in
                     `extract_listing`) — and the value itself is re-read from the
                     archived page body under `ARCHIVED_COORDINATE_RULES`. The literal is
                     kept for the legacy column it was named after, `listings.geom`,
                     which W1-c stopped reading and W4-c dropped.
      none         - the portal ships no admissible coordinate at all.
    """
    substrate: str
    first_party_sources: frozenset[str] = frozenset()


COORDINATE_RULES: dict[str, CoordinateRule] = {
    # Post-cutover `locality.gps_lat/gps_lon`; the retired shape yields no coordinate.
    "sreality": CoordinateRule("payload"),
    # `advert.gps{lat,lng}` — 97.4% unique, the cleanest pin of the fleet [live-A §2.5].
    "bezrealitky": CoordinateRule("payload"),
    # The Vue prop's `point{latitude,longitude}` — first-party [06 §6.2.1].
    "mmreality": CoordinateRule("payload"),
    # Only `link` is first-party on bazos: the CZ-guarded maps anchor inside the ad.
    "bazos": CoordinateRule("geom_column", frozenset({"link"})),
    "idnes": CoordinateRule("geom_column", frozenset({"page"})),
    "ceskereality": CoordinateRule("geom_column", frozenset({"page"})),
    "realitymix": CoordinateRule("geom_column", frozenset({"page"})),
    "maxima": CoordinateRule("geom_column", frozenset({"page"})),
    # remax stamped NO `coords` key until 2026-09-11, so every stored remax coordinate read
    # as unestablished provenance and the portal was the fleet's only `"none"` rule — while
    # 7,932 of its 8,009 unstamped active rows were the page's own `#printMap[data-gps]`
    # pin (audit 2026-09-11). `scraper.remax_parser` now stamps the subject-map pin `page`;
    # rows drained before that carry no stamp and stay refused
    # (`coordinate_provenance_unestablished`) until their next 6 h drain rewrites raw_json.
    "remax": CoordinateRule("geom_column", frozenset({"page"})),
}

# The two substrates the ladder can be asked about. `COORDINATE_RULES` above describes the
# first one ONLY — the payload we already hold plus the class-B `listings` columns — which
# is every W1/W3 caller. W2's archived body is a different question with different answers
# (remax publishes NO first-party coordinate in `raw_json` and DOES publish one in
# `#printMap[data-gps]`), so it gets its own table rather than a substrate flag smuggled
# into the existing rows.
SUBSTRATE_PAYLOAD = "raw_json"
SUBSTRATE_ARCHIVED_HTML = "archived_html"


@dataclass(frozen=True, slots=True)
class ArchivedCoordinateRule:
    """Where a portal's coordinate legitimately comes from on the ARCHIVED body (C6).

    `entry_id` is the ONE contract entry whose locator addresses the portal's own detail
    map. Naming it — rather than admitting any coordinate-typed entry — is what stops a
    later per-portal PR from licensing a second, unruled locator by simply declaring
    `claim_type: coordinate`: an unrecognised entry gets no coordinate, and the PR that
    wants one has to add a row here and argue for it.

    `geocoded_licence_class` is realitymix's second branch, and it is the whole of C6:
    `/build/maps.913b4199.js` falls back to `nominatim.openstreetmap.org` when `data-gps-*`
    is absent and labels the pin *"Pozice na mapě je pouze orientační"* [live-C §2.3]. ODbL
    follows the geometry, not the republisher (00 §6.2), so that branch is `'odbl'` — never
    `'portal'`, and never the retired `portal_osm_derived` spelling. A portal with no such
    branch leaves it None, and a coordinate recovered without the portal's own pin is
    refused rather than guessed at.
    """
    entry_id: str
    licence_class: str
    geocoded_licence_class: str | None = None
ARCHIVED_COORDINATE_RULES: dict[str, ArchivedCoordinateRule] = {
    # `#printMap[data-gps], #listingMap[data-gps]`, scoped by element id — never "the first
    # data-gps in the document", which is the neighbour carousel [live-B §3.5.1].
    "remax": ArchivedCoordinateRule("rx.det.gps", "portal"),
    # The ad's own `google.com/maps/place/<lat>,<lon>` anchor, titled "Přibližná lokalita"
    # (W1-c R6). Same shape as remax's row and for the same reason: the PAGE publishes the
    # pin, so it is first-party. The pin is permanently approximate — the entry's
    # `precision_cap` is what says so, not a missing
    # row here, which only ever said "bazos has no coordinate at all".
    # `bzs.det.link_pin` is 02 §2.2.3's reserved id for exactly this act.
    "bazos": ArchivedCoordinateRule("bzs.det.link_pin", "portal"),
    "realitymix": ArchivedCoordinateRule("rm.det.gps", "portal",
                                         geocoded_licence_class="odbl"),
    # A typed {coordinates,address} pair keyed by the listing id, inside the MapTiler blob.
    "idnes": ArchivedCoordinateRule("id.det.subject_feature", "portal"),
    # The same Vue `point{}` W1 reads out of raw_json, re-read from the archived body.
    "mmreality": ArchivedCoordinateRule("mm.det.point", "portal"),
    # The OpenLayers config's `/features/0`.
    "maxima": ArchivedCoordinateRule("mx.det.map_features", "portal"),
    # The decimal pair `input#driving_calculator_from` stamps, which the page's own
    # Google embed echoes as `q=lat,lng` — first-party, and since rule 25 deletes the
    # `listings.geom` reader it is the ONLY pin this portal has (W1-c R10).
    "ceskereality": ArchivedCoordinateRule("cr.det.page_pin", "portal"),
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
    observed_at: datetime


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
    # D7 evidence (01 §4.2's `loc_claim_text_evidence` + `loc_claim_evidence_payload`).
    # NULL on every claim mined from `listings.raw_json`: that substrate is latest-wins
    # JSON, and a span into a document nobody archived is a one-shot check. The PAGE
    # readers fill them — the stored body is content-addressed and immutable — and for a
    # `regex_text` claim the DB REQUIRES the whole set: quote, both offsets,
    # `payload_scope_version`, `subject_scoped`, plus `payload_sha256` whenever there is a
    # quote at all. `payload_sha256` is hex TEXT here, not `bytes`: the write path carries
    # rows through `jsonb_to_recordset`, which has no bytea literal, so the SQL decodes it.
    payload_id: int | None = None
    payload_sha256: str | None = None
    evidence_quote: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    payload_scope_version: str | None = None
    # `loc_claim_llm_model` forces both non-null on an `llm_text` claim. No lane emits
    # one (rule 25: no model in the claim lane) and the columns stay, because the CHECK
    # does: a `Claim` that can be spelled but not written is a trap that takes a whole
    # batch down at the constraint, once, in production.
    model: str | None = None
    prompt_version: str | None = None
    # NULL on every claim the one lane writes: both its substrates are latest-wins (the
    # listing's `raw_json`, the listing's newest stored body), so there is no snapshot to
    # anchor to. The column stays because `loc_claim_anchor` (01 §4.2) pairs it with
    # `snapshot_anchor` and rows written by the deleted snapshot re-mine still carry it.
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
            "payload_id": self.payload_id,
            "payload_sha256": self.payload_sha256,
            "evidence_quote": self.evidence_quote,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "payload_scope_version": self.payload_scope_version,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }
        return row



@dataclass(slots=True)
class IntakeResult:
    """What one listing (or one page body) yielded: claims, and a tally of refusals.

    Refusals are COUNTED, not recorded. Rule 25 shrank the store to one answer table plus
    the append-only claims: `location_claim_absences` / `location_enrichment_state` were
    written by every lane and read by none (dropped by migration 498), so a refused
    coordinate is one log line per reason per batch with a count, which is what the
    operator actually looks at.
    """
    claims: list[Claim] = field(default_factory=list)
    refusals: Counter[str] = field(default_factory=Counter)

    def refuse(self, reason: str) -> None:
        self.refusals[reason] += 1

    def extend(self, other: IntakeResult) -> None:
        self.claims.extend(other.claims)
        self.refusals.update(other.refusals)



@dataclass(frozen=True, slots=True)
class CoordinateVerdict:
    admitted: bool
    licence_class: str | None
    reason: str


# ------------------------------------------------------------------ the licence ladder

def coordinate_verdict(
    source: str, coords_source: str | None, *,
    substrate: str = SUBSTRATE_PAYLOAD, entry_id: str | None = None,
    portal_pin_present: bool = True,
) -> CoordinateVerdict:
    """06 §6.1.2, applied to the INPUT. On the payload substrate it never returns a
    non-`portal` licence class: a class-E coordinate produces no claim at all (§6.6 rule 6).

    A coordinate is admissible only when the portal itself published it — in the payload we
    hold, or at the one archived locator its rule names. There is no third source: the
    geocoder that made the others is gone (W4-b), so `geocode`/`street`/`locality` and the
    `carry_forward` stamp it laundered through refetches license nothing.

    `substrate` defaults to the payload one; `SUBSTRATE_ARCHIVED_HTML` asks the same ladder
    about a body W2 re-mines. `entry_id` / `portal_pin_present` are only consulted on the
    archived arm.
    """
    if substrate == SUBSTRATE_ARCHIVED_HTML:
        return _archived_coordinate_verdict(
            source, entry_id=entry_id, portal_pin_present=portal_pin_present)
    rule = COORDINATE_RULES.get(source)
    if rule is None:
        return CoordinateVerdict(False, None, "unknown_source")
    if rule.substrate == "none":
        return CoordinateVerdict(False, None, "no_first_party_coordinate_on_this_portal")
    if rule.substrate == "payload":
        return CoordinateVerdict(True, "portal", "portal_published_payload_coordinate")
    # geom_column: the provenance stamp is the only thing that can license the value.
    if coords_source is None:
        return CoordinateVerdict(False, None, "coordinate_provenance_unestablished")
    if coords_source in rule.first_party_sources:
        return CoordinateVerdict(True, "portal", f"first_party_{coords_source}")
    return CoordinateVerdict(False, None, "unrecognised_coordinate_provenance")


def _archived_coordinate_verdict(
    source: str, *, entry_id: str | None, portal_pin_present: bool,
) -> CoordinateVerdict:
    """The archived-body arm of the ladder: the ONE locator the portal's rule names, and
    nothing else."""
    rule = ARCHIVED_COORDINATE_RULES.get(source)
    if rule is None:
        return CoordinateVerdict(False, None, "no_archived_coordinate_locator_on_this_portal")
    if entry_id != rule.entry_id:
        return CoordinateVerdict(False, None, "unrecognised_archived_coordinate_locator")
    if portal_pin_present:
        return CoordinateVerdict(True, rule.licence_class, f"archived_{rule.entry_id}")
    if rule.geocoded_licence_class is None:
        return CoordinateVerdict(False, None, "coordinate_provenance_unestablished")
    return CoordinateVerdict(
        True, rule.geocoded_licence_class, f"archived_{rule.entry_id}_portal_geocoded")


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


# ---- W2: the comma-address vocabulary.
#
# Czech portals publish a whole address in ONE string — realitymix's
# `data-address="Křimická, Plzeň 3, Plzeň, okres Plzeň-město"`, maxima's
# `div.locality` "Praha 3, Žižkov, Jeseniova" — and the reader that lifts it is a plain
# `html_text` / `html_attr` read. Selecting a typed part is therefore a NORMALISER on that
# one value, not a family of per-portal readers: the reader states what the page said, the
# transform states which part of it this entry claims, and both stay portal-agnostic.
#
# Every one of them REFUSES rather than guesses. A comma path whose meaning depends on how
# many levels it has is the normal case ("Kostelec nad Černými Lesy" is one segment and its
# only token is an obec), and a positional read with no arity condition types an obec as a
# městský obvod on every short line. Emitting nothing is a miss; a wrong admin level is a
# wrong answer that reads as a right one.
# `okres X` and `okr. X`, and the boundary is spelled per spelling: after the dotted
# form there is no word boundary to require (a `.` and a space are both non-word), so
# `^okr(?:es|\.)\b` silently never matches the abbreviation.
_OKRES_QUALIFIER_RE = re.compile(r"(?i)^okr(?:es\b|\.)")
_TRAILING_HOUSE_NUMBER_RE = re.compile(r"\s+(\d{1,4}[a-z]?(?:/\d{1,4}[a-z]?)?)$", re.I)
_COMMA_SEGMENT_RE = re.compile(r"^(?P<index>-?[1-9]\d*)@(?P<arity>\*|[1-9]\d*\+?)$")


def _address_segments(value: str) -> list[str]:
    return [part for part in (raw.strip() for raw in value.split(",")) if part]


def _obec_index(segments: list[str]) -> int:
    """Where the obec sits: last, or second-to-last behind an `okres …` qualifier."""
    return len(segments) - 2 if _OKRES_QUALIFIER_RE.match(segments[-1]) else len(segments) - 1


@transform("address_part_okres")
def _address_part_okres(value: str, arg: str) -> str | None:
    """The `okres X` tail of a comma address, without its qualifier word.

    Keyed on the QUALIFIER, never on position: a line that does not end in `okres …` /
    `okr. …` has no okres, and `strip_prefix:"okres "` on the same segment would silently
    publish `okr. Karlovy Vary` as an okres name on the abbreviated spelling."""
    segments = _address_segments(value)
    if not segments or not _OKRES_QUALIFIER_RE.match(segments[-1]):
        return None
    return _OKRES_QUALIFIER_RE.sub("", segments[-1]).strip(" .,") or None


# The eight statutory cities whose OBVOD names are published where a town name belongs.
# Two shapes, and the split is the portals' own: a NUMBER or a Roman numeral is always an
# obvod ("Praha 8", "Plzeň 3", "Pardubice II"), while a hyphenated name is an obvod only in
# the five cities whose obvody are named that way — "Brno-Židenice" is a Brno obvod,
# "Kostelec nad Černými Lesy" is a town, and stripping after a hyphen everywhere would turn
# "Frýdek-Místek" into "Frýdek". The obvod itself is not lost: a portal that publishes one
# claims it as `cast_obce_name`, which is the type the resolver binds momc / spravní obvod /
# část obce names under.
# The ordinal arm carries an OPTIONAL trailing name, because the portals write the obvod both
# ways: "Praha 8" and "Praha 10 - Vršovice" are the same obvod, and "Liberec XXV-Vesec" is how
# RÚIAN itself spells the Roman-numeral ones. Without the tail the longer spelling fell
# through unchanged and published a town no gazetteer has.
_STATUTORY_CITY_ORDINAL_RE = re.compile(
    r"^(Praha|Plzeň|Brno|Ostrava|Pardubice|Opava|Liberec|Ústí nad Labem)"
    r"\s*[-–]?\s*(?:\d+|[IVX]+)(?:\s*[-–]\s*\S.*)?$")
_STATUTORY_CITY_HYPHEN_RE = re.compile(
    r"^(Brno|Ostrava|Opava|Liberec|Ústí nad Labem)\s*[-–]\s*\S.*$")

# Every OKRES whose RÚIAN name is a statutory city plus a hyphen. They are spelled exactly
# like an obvod and they are not one: folding "Brno-venkov" to "Brno" claims the second-
# largest city in the country as the town of any village in its hinterland, and the value
# arrives here routinely because `address_part_obec` runs on lines that carry an okres
# segment. An okres is never a town, so it is returned untouched and the okres entry — which
# is what states this fact — keeps it.
#
# The list is closed because the Czech okres set is (76 + Praha, unchanged since 2007); these
# are all of them containing a hyphen. Every other hyphenated name reaching this transform is
# either an obvod of one of the five cities above or an ordinary two-part town
# ("Frýdek-Místek", "Kostelec nad Černými Lesy"), and the arms already tell those apart.
_HYPHENATED_OKRES_NAMES = frozenset({
    "Brno-město", "Brno-venkov",
    "Ostrava-město",
    "Plzeň-město", "Plzeň-sever", "Plzeň-jih",
    "Praha-východ", "Praha-západ",
    "Frýdek-Místek",
})


@transform("statutory_city_obec")
def _statutory_city_obec(value: str, arg: str) -> str | None:
    """A městský obvod -> its city; anything else unchanged.

    RÚIAN has no obec called "Praha 8" — the obec is "Praha" and the obvod is a child of it
    — so a town claim carrying the obvod resolves to nothing at all, which is a town-coverage
    hole (rule 25) that looks like a portal that publishes no town. Refusing to guess is not
    an option here the way it is in `address_part_street`: the city IS stated, in the same
    string, by name.
    """
    stripped = value.strip()
    if stripped in _HYPHENATED_OKRES_NAMES:
        return value
    for pattern in (_STATUTORY_CITY_ORDINAL_RE, _STATUTORY_CITY_HYPHEN_RE):
        found = pattern.match(stripped)
        if found:
            return found.group(1)
    return value


@transform("address_part_obec")
def _address_part_obec(value: str, arg: str) -> str | None:
    """The obec segment of a comma address, with a statutory-city obvod folded to its city.

    The fold is implicit rather than a transform the entry has to remember to chain: every
    portal that states an address states it the same way, and an entry that forgot the chain
    would publish "Praha 4" as a town on the biggest city in the corpus."""
    segments = _address_segments(value)
    if not segments:
        return None
    index = _obec_index(segments)
    if index < 0:
        return None
    return _statutory_city_obec(segments[index], "")


@transform("address_part_street")
def _address_part_street(value: str, arg: str) -> str | None:
    """The LEADING segment, but only when it survives the street tests.

    `data-address` segment[0] is not reliably a street — on realitymix's pinned archived
    body it is `Stráň`, a část obce the same page also states as `data-form-address` and as
    the breadcrumb tail — so typing it `street_name` blindly fabricates. The three gates are
    the shared scraper ones (`clean_street`, `reject_as_town` against this line's OWN other
    segments, `looks_like_czech_street`), not a second copy: the extractor and the live
    parser must agree on what a Czech street looks like. `arg='loose'` drops only the
    morphology test, for a portal whose leading segment is unambiguously a street."""
    segments = _address_segments(value)
    if len(segments) < 1:
        return None
    index = _obec_index(segments)
    if index < 1:
        return None
    cleaned = street.clean_street(segments[0])
    if cleaned is None:
        return None
    if street.reject_as_town(cleaned, geo_names=segments[1:]):
        return None
    if arg != "loose" and not street.looks_like_czech_street(cleaned):
        return None
    return cleaned


@transform("address_part_house_number")
def _address_part_house_number(value: str, arg: str) -> str | None:
    """The house number glued to the street segment (`Křimická 655/31` -> `655/31`).

    Gated on the SAME street tests as `address_part_street`, so a number can never be
    claimed off a leading segment this vocabulary refuses to call a street. The čp/čo pair
    is not split here — `split_cp_co:cp` / `:co` is the shared splitter that owns that, and
    an entry chains it after this one."""
    segments = _address_segments(value)
    if _address_part_street(value, arg) is None:
        return None
    found = _TRAILING_HOUSE_NUMBER_RE.search(segments[0])
    return found.group(1) if found else None


# ISO-3166 alpha-2 for the countries this corpus actually contains, keyed by every
# spelling a Czech portal writes them in (Czech, English, the country's own). A CLOSED
# table on purpose: the trailing segment of an address is an obec far more often than it is
# a country, so "looks like a country" has to mean "is one of these" — anything else is
# left to the obec claim rather than typed as a country on a guess. The claim VALUE is the
# code, never the spelling: `country` is the one claim type the resolver compares across
# portals, and "Slovensko" / "Slovakia" / "Slovenská republika" are one country.
_COUNTRY_CODES: dict[str, str] = {}
for _code, _names in {
    "CZ": ("Česká republika", "Česko", "Czech Republic", "Czechia"),
    "SK": ("Slovensko", "Slovenská republika", "Slovakia"),
    "DE": ("Německo", "Deutschland", "Germany"),
    "AT": ("Rakousko", "Österreich", "Austria"),
    "PL": ("Polsko", "Polska", "Poland"),
    "HR": ("Chorvatsko", "Hrvatska", "Croatia"),
    "IT": ("Itálie", "Italia", "Italy"),
    "ES": ("Španělsko", "España", "Spain"),
    "BG": ("Bulharsko", "Bulgaria"),
    "GR": ("Řecko", "Greece"),
    "CY": ("Kypr", "Cyprus"),
    "TR": ("Turecko", "Türkiye", "Turkey"),
    "AE": ("Spojené arabské emiráty", "United Arab Emirates"),
    "FR": ("Francie", "France"),
    "PT": ("Portugalsko", "Portugal"),
    "SI": ("Slovinsko", "Slovenija", "Slovenia"),
    "ME": ("Černá Hora", "Crna Gora", "Montenegro"),
    "HU": ("Maďarsko", "Magyarország", "Hungary"),
    "CH": ("Švýcarsko", "Schweiz", "Suisse", "Switzerland"),
    "AL": ("Albánie", "Albania"),
    "RS": ("Srbsko", "Serbia"),
    "US": ("Spojené státy americké", "United States"),
    "GB": ("Velká Británie", "United Kingdom"),
}.items():
    for _name in _names:
        _COUNTRY_CODES[_name] = _code
del _code, _names, _name


def _fold(value: str) -> str:
    """Diacritic- and case-free key. The portals are inconsistent about both (`SLOVENSKO`,
    `Nemecko`), and a table keyed on one exact spelling silently matches nothing."""
    import unicodedata

    stripped = "".join(ch for ch in unicodedata.normalize("NFKD", value)
                       if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


_COUNTRY_BY_FOLDED: dict[str, str] = {_fold(name): code
                                      for name, code in _COUNTRY_CODES.items()}


@transform("address_part_country")
def _address_part_country(value: str, arg: str) -> str | None:
    """The trailing segment as an ISO-3166 alpha-2 code, or nothing.

    "foreign is a determination, never a default" (rule 25): the country claim exists so a
    listing outside CZ is STATED to be outside CZ rather than inferred from a Czech obec
    that did not resolve. So this answers only where the portal named a country, and a
    trailing segment that is not in `_COUNTRY_CODES` yields no claim at all.
    """
    segments = _address_segments(value)
    if not segments:
        return None
    return _COUNTRY_BY_FOLDED.get(_fold(segments[-1]))


@transform("split_paren_okres")
def _split_paren_okres(value: str, arg: str) -> str | None:
    """`Ostrov (okres Karlovy Vary)` — one string carrying an obec and its okres.

    Keyed on the literal `(okres `, so a different parenthetical is left alone and visible
    instead of being silently truncated. `arg` picks the half: the default is the obec,
    `okres` is the qualifier's own value."""
    head, separator, tail = value.partition("(okres ")
    if arg == "okres":
        return tail.rstrip().rstrip(")").strip() or None if separator else None
    return head.strip() or None


@transform("comma_segment")
def _comma_segment(value: str, arg: str) -> str | None:
    """`<index>@<arity>` — one segment of a comma address, under an arity condition.

    The index is 1-based from the left or negative from the right; the arity is `k`
    (exactly k segments), `k+` (at least k) or `*` (any). The arity is not decoration:
    maxima's `div.locality` is `Praha 3, Žižkov, Jeseniova` on one row and
    `Kostelec nad Černými Lesy` on the next, and taking segment 1 unconditionally types an
    obec as a městský obvod on every village. A malformed arg yields no claim rather than
    raising — `apply_transforms` is rollback-safe by design, and the rail that catches a
    typo is `contracts._check_executable` plus the fixture gate."""
    spec = _COMMA_SEGMENT_RE.match(arg)
    if spec is None:
        return None
    segments = _address_segments(value)
    arity = spec.group("arity")
    if arity != "*":
        want = int(arity.rstrip("+"))
        if len(segments) < want or (not arity.endswith("+") and len(segments) != want):
            return None
    index = int(spec.group("index"))
    position = index - 1 if index > 0 else len(segments) + index
    if position < 0 or position >= len(segments):
        return None
    return segments[position] or None


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


# ------------------------------------------------------------------ claim stamping

def _base(entry: Entry, row: ListingRow, **overrides: Any) -> Claim:
    """Every claim is stamped identically: contract identity, anchor, blur, licence.

    `blur_evidence` and `licence_class` are always passed explicitly — 06 §6.6 rule 7:
    letting the column default fire stamps "no blur observed" onto the rows that carry a
    portal blur flag, and in an append-only table that is unrecoverable.
    """
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
        # Both substrates are latest-wins, so there is one anchor. `unanchored_legacy` went
        # with the `listings`-column readers (W1-c).
        "snapshot_anchor": "unanchored_latest_fetch",
        "first_observed_at": row.observed_at,
        "blur_evidence": entry.default_blur_evidence,
        "licence_class": entry.default_licence_class,
        "history_completeness": HISTORY_COMPLETENESS[row.source],
        "subject_scoped": entry.subject_scope.get("subject_scoped", True),
    }
    fields.update(overrides)
    if entry.claim_type == "precision_declaration":
        # W1-c R5, once for every reader on both substrates, because this is the only place
        # every claim passes through. Two halves, and they are owned by different parties:
        #
        #  * the LABEL is the reader's. A reader that already decided it keeps it
        #    (`json_bool` maps a boolean to the label the CONTRACT names, `json_geometry`
        #    types a Circle); one that did not gets the portal's own value, so a
        #    `json_scalar` / `json_regex` / `html_regex` / `scalar` entry carries a portal's
        #    exact/approximate signal instead of NULL.
        #  * the BLUR AXIS is the CONTRACT's, never the reader's or the entry default's.
        #    `blur_evidence` on a declaration means exactly "this label is one of the ones
        #    `precision_cap.blurred_labels` calls blurred" — a pure function of the label,
        #    so a reader (or a hard-coded entry default) that answers it separately can only
        #    disagree with the calibration. maxima@2 did: `blur_evidence: declared` on the
        #    feature-type entry made a PRECISE Point resolve as portal-declared-blurred.
        label = fields.get("declared_precision_label") or fields.get("value_text")
        blurred = {str(x) for x in (entry.precision_map.get("blurred_labels") or [])}
        fields["declared_precision_label"] = label
        fields["blur_evidence"] = "declared" if label in blurred else "none"
    return Claim(**fields)



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
