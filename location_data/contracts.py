"""Portal extraction contracts as data — YAML in git, projected into the DB (D9).

Design: 02-portal-contracts.md §2.1 (the contract format), §2.1.8 (lifecycle: git is the
store of record, the DB pair is a deploy-time projection, retraction is an append),
§2.1.9 (licence_class), 00-shared-contracts.md §3 (surface / page_kind / extraction_method
are three separate axes, all three mandatory).

Two tables, one header + its immutable entries (migration 382):
  portal_contracts        (source, version, contract_sha256, git_ref, is_active, …)
  portal_contract_entries (contract_id, entry_id, surface, page_kind, locator, claim_type,
                           extraction_method, …)

`is_active` lives on the HEADER (the partial unique index is per source). A change to any
ENTRY is a new `contract_version`, never an edit, and projecting a contract whose governed
bytes changed under an already-loaded version is refused — that is the whole point of
`contract_sha256`. Two header columns are mutable, neither of them extraction: `is_active`
says which version the extractor runs, and `fetch_config` carries the two blocks the hash
does not govern (`contract_body_hash`), so its `persistence` copy tracks git rather than
freezing at whatever version first shipped it.

This module is the deploy-time/CI lane and imports PyYAML lazily. The claims extractor
(`location_data.claims_intake`) reads the DB projection and never parses YAML.

ONE KEY IS READ FROM GIT AT RUNTIME, not from the projection: `persistence.volatile_paths`
(W2a-3b). `location_data.payload_norm` parses it out of these same files with the same
parser this module validates them with, because `payload_sha256` is a PERMANENT content
address and the projection producing it must be a function of the deployed artefact alone
— never of whether the contract-load job had run yet. That is why `contracts/` is COPYed
into the image and PyYAML is a runtime dependency. Everything else here stays deploy-time.

CLI:
    python -m location_data.contracts --check                     # validate only, no DB
    python -m location_data.contracts --load --git-ref <sha>      # project + activate
    python -m location_data.contracts --retract sreality@1 [--extractor-id sr.det.gps]

RETRACTION IS A DELETE (W1-b, migration 498). A contract version that misread the portal
produced no evidence, so its claims go and their listings are re-resolved from what
survives — there is no append-only retraction ledger and no `location_claims_live` view
subtracting rows on every read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from location_data import payload_norm
from scraper import db

LOG = logging.getLogger("location_data.contracts")

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "contracts" / "portals"

# 02 §2.2 preamble — portal-unique, permanent. bezrealitky is `bzr.` and bazos is `bzs.`
# (revision 1 gave both `bz.`, which would have merged the provenance of a GraphQL portal
# and an HTML portal on ids §2.1.2 forbids renaming once stamped).
EXTRACTOR_PREFIXES: dict[str, str] = {
    "sreality": "sr.",
    "bezrealitky": "bzr.",
    "bazos": "bzs.",
    "idnes": "id.",
    "mmreality": "mm.",
    "remax": "rx.",
    "ceskereality": "cr.",
    "realitymix": "rm.",
    "maxima": "mx.",
}

# Migration 380's enums, MINUS what rule 25 retired. A Postgres enum cannot shrink in
# place, so `location_claim_surface` / `location_extraction_method` / `location_claim_type`
# keep every label they were declared with and these three sets are SUBSETS of them until
# W4 drops the rest (`test_location_schema_contracts` pins the subset relation). A literal
# that is not a member fails validation here rather than at INSERT time (01 §A.2 check 2).
#
# `legacy_column` is the one retired surface (W1-c): the lane reads `raw_json` and the
# stored page body, and a `listings` column is neither.
CLAIM_SURFACES = frozenset({
    "api_json", "graphql", "embedded_json", "html_selector", "map_config", "og_meta",
    "jsonld", "url_slug", "description", "archived_html", "registry",
    "operator_input",
})
# Defined in `payload_norm` and re-exported here, not copied: both this gate and the
# runtime profile loader validate a declared `volatile_paths` page_kind against it, and
# payload_norm is the one that must stay importable without this lane. Two copies could
# disagree, and the way they would show it is a contract that passes CI and silently
# resolves to the base profile in production.
PAGE_KINDS = payload_norm.PAGE_KINDS
EXTRACTION_METHODS = frozenset({
    "portal_structured_field", "portal_declared_quality", "html_selector_parse",
    "url_slug_parse", "breadcrumb_parse", "jsonld_parse", "map_widget_parse", "regex_text",
    "llm_text", "registry_derived", "operator_manual",
})
# ELEVEN, and that is the whole vocabulary a contract may claim (rule 25 / W1-c R1).
# Ten after W2: `precision_declaration` folds onto the pin claim when the resolver is
# rewritten. The other 29 enum labels are not "declared ahead for a later wave" — they were
# entries nothing resolved, which is the state this wave exists to end. The ones with a
# live reader but no resolver (`uncertainty_geometry`, `map_zoom`, `blur_hint`,
# `obec_code`, `portal_admin_id`, `postal_town`, …) go with them; a portal fact worth
# claiming re-enters through one of the eleven.
CLAIM_TYPES = frozenset({
    "coordinate", "precision_declaration", "country",
    "kraj_name", "okres_name", "obec_name", "cast_obce_name",
    "street_name", "house_number_cp", "house_number_co", "psc",
})
# The one type every contract must claim, with a reader. "Every active Czech listing has a
# town" is rule 25's invariant and `location_town_coverage` is red until it holds, so a
# contract that cannot state a town is not a contract this fleet can ship.
MANDATORY_CLAIM_TYPE = "obec_name"
GRANULARITIES = frozenset({
    "unknown", "country", "kraj", "okres", "obec", "cast_obce_or_quarter", "street",
    "street_segment", "parcel", "building", "address_point",
})
POSITION_SOURCES = frozenset({
    "none", "admin_centroid", "derived_geocode", "carried_forward", "portal_pin_blurred",
    "portal_pin", "registry_point",
})
BLUR_EVIDENCE = frozenset({"none", "declared", "detected", "both"})
# `match_confidence` (01 §2). It reaches the DB two ways — as the entry's `prior` for the
# resolver, and as `locator.claim_confidence`, which a legacy-column reader stamps onto
# `location_claims.claim_confidence` (a typed enum column, so a typo here would fail
# mid-batch at INSERT time instead of in CI).
MATCH_CONFIDENCES = frozenset({"low", "medium", "high", "exact"})
LICENCE_CLASSES = frozenset({
    "portal", "cc_by_ruian", "odbl", "commercial_permanent", "ephemeral_display_only",
    "operator",
})
# 02 §2.1.9: `ephemeral_display_only` is reserved for live third-party geocoder calls and
# is NEVER emitted by a contract.
CONTRACT_LICENCE_CLASSES = LICENCE_CLASSES - {"ephemeral_display_only"}

# `cardinality` / `required` / `on_conflict` were entry keys nothing enforced: the
# extractor emits whatever the reader finds, `required: always` never made a missing value
# an error anywhere, and no writer branches on `on_conflict`. They are REFUSED now rather
# than tolerated — a key that reads like a rail and is not one is worse than no key (W1-c
# R3). The `portal_contract_entries` columns keep their NOT NULL defaults until W4 drops
# them; this loader simply stops writing them.
RETIRED_ENTRY_KEYS = ("cardinality", "required", "on_conflict")

# What each reader WILL DO with an entry, as data. The contract gate refuses an entry that
# declares anything this record does not cover, because the runtime would ignore it in
# silence — a contract entry may not state something the claim never carries.
#
# W1 gated readers fleet-wide: any reader was legal on any raw_json-reachable surface and
# illegal everywhere else. That gate checked one axis of five: a `namespaced_id` entry
# without `locator.namespace` KeyError'd mid-batch; a `scalar` entry could declare
# `guards: [reject_outside_cz_bbox]` that `_read_scalar` never evaluates; a `point_pair`
# entry could declare a `transform` no coordinate reader applies. Each reader mines exactly
# ONE substrate by ONE method, addresses it through a fixed set of locator keys, and
# consults `transform` / `guards` or does not — so all four are properties of the READER.
#
# Pure DATA on purpose: this module is the deploy-time/CI lane and must not import
# `location_data.claims_intake` (the runtime extractor, which pulls in the loader and the
# DB). Every field is derived back out of the reader bodies by
# tests/location_data/test_claims_intake_contracts, so the record cannot drift from the
# call sites it describes.
_PAYLOAD_SURFACES = frozenset({"api_json", "graphql", "embedded_json"})
_STRUCTURED = frozenset({"portal_structured_field"})
_DECLARED_QUALITY = frozenset({"portal_declared_quality"})
# The DOM readers of `location_data.page_readers`. `html_selector` is the surface a
# contract DECLARES; `archived_html` is what the claim is STAMPED with when the lane runs
# it against the stored page body (C9, a runtime mapping — the entry ids stay the ones
# 02 §2.2 fixed). Both are admitted so one entry can be executed against a live parse and a
# stored body without minting a second id for the same act.
_DOM_SURFACES = frozenset({"html_selector", "archived_html", "map_config"})
_DOM_METHOD = frozenset({"html_selector_parse"})
_MAP_METHOD = frozenset({"map_widget_parse"})
# W2: the three surfaces the reader canon opened beside the DOM ones, each paired with
# `archived_html` for the same reason `_DOM_SURFACES` is — that is what the archived lane
# STAMPS on the claim (C9) while the entry keeps the `locator_kind` 02 §2.2 fixed for it,
# so one entry id serves the live parse and the archived body.
#   * embedded JSON: a JSON document carried inside the page (idnes' `data-maptiler-json`
#     script, mmreality's `:property` attribute, maxima's `JSON.parse('…')` config). A
#     portal calls the same document `map_config` when it is a map's, so both are admitted.
#   * a fact a portal publishes only inside a LINK (`url_slug`).
#   * schema.org JSON-LD (`jsonld`), where the geo chain is a breadcrumb.
_EMBEDDED_JSON_SURFACES = frozenset({"embedded_json", "map_config", "archived_html"})
_SLUG_SURFACES = _DOM_SURFACES | {"url_slug"}
_JSONLD_SURFACES = frozenset({"jsonld", "archived_html"})
# `regex_text` is EVIDENCE-BEARING (01 §4.2's `loc_claim_text_evidence`), so it is not
# folded into `_DOM_METHOD`: an entry may not silently swap a method whose claims carry no
# mandatory span for one whose claims do.
_REGEX_METHOD = frozenset({"regex_text"})
_SLUG_METHOD = frozenset({"url_slug_parse"})
_BREADCRUMB_METHOD = frozenset({"breadcrumb_parse"})


@dataclass(frozen=True, slots=True)
class ReaderContract:
    """One `claims_intake` reader's appetite: what it reads, and what it consults.

    `substrates` and `methods` are 00 §3's two separate provenance axes — a reader that
    mines one document while the entry stamps `portal_structured_field` records a
    provenance the value never had. `locator_keys` are the keys the reader indexes
    UNGUARDED (a missing one is a mid-batch `KeyError` that takes the whole intake down,
    not a no-op); `optional_keys` are the rest of what it reads. Together they are the
    reader's WHOLE appetite, and a locator key outside the union is refused — a declared
    key no reader consults is a rail that looks enforced and is not (`bzs.det.link_pin`
    shipped a `pattern` its reader ignored, so the pin was silently inert while the
    contract read as if it published one). The two `consults_*` flags say whether the
    reader ever asks about `transform` / `guards`; a declaration on a reader that does not
    is inert.
    """

    substrates: frozenset[str]
    methods: frozenset[str]
    locator_keys: frozenset[str] = frozenset()
    optional_keys: frozenset[str] = frozenset()
    consults_transforms: bool = False
    consults_guards: bool = False
    # Which HALF of the lane runs it. Data rather than a substrate test, because
    # `embedded_json` and `archived_html` are both legal on readers of either half — it is
    # the registry a reader is REGISTERED in (`PAGE_READERS` vs `READERS`) that decides, and
    # `test_claims_intake_contracts` derives this back out of those two registries.
    reads_stored_body: bool = False

    @property
    def appetite(self) -> frozenset[str]:
        """Every `locator` key this reader reads. `reader` is the locator's own
        discriminator rather than a value looked up, so it is always legal."""
        return self.locator_keys | self.optional_keys | frozenset({"reader"})


# One row per `location_data.claims_intake` reader. Payload readers address `raw_json` by
# JSON pointer -> the three surfaces whose bytes ARE the payload; the rest read the stored
# page body. W1-c deleted the three that read a `listings` column instead
# (`legacy_text_column`, `geom_column`, `coords_stamp_quality`): the `legacy_column`
# surface is gone, so an entry naming one could not be declared at all.
READER_CONTRACTS: dict[str, ReaderContract] = {
    "scalar": ReaderContract(
        substrates=_PAYLOAD_SURFACES, methods=_STRUCTURED,
        locator_keys=frozenset({"json_pointer"}),
        consults_transforms=True,
        optional_keys=frozenset({"value_kind"})),
    "namespaced_id": ReaderContract(
        substrates=_PAYLOAD_SURFACES, methods=_STRUCTURED,
        locator_keys=frozenset({"json_pointer", "namespace"}),
        consults_transforms=True),
    "point_pair": ReaderContract(
        substrates=_PAYLOAD_SURFACES, methods=_STRUCTURED,
        locator_keys=frozenset({"lat_pointer", "lon_pointer"}),
        consults_guards=True),
    "bbox_envelope": ReaderContract(
        substrates=_PAYLOAD_SURFACES, methods=_STRUCTURED,
        locator_keys=frozenset({"json_pointer"}),
        consults_guards=True),
    "declared_quality": ReaderContract(
        substrates=_PAYLOAD_SURFACES, methods=_DECLARED_QUALITY,
        locator_keys=frozenset({"json_pointer"})),
    "declared_bool_quality": ReaderContract(
        substrates=_PAYLOAD_SURFACES, methods=_DECLARED_QUALITY,
        locator_keys=frozenset({"json_pointer"}),
        optional_keys=frozenset({"labels"})),
    "conflict_signal": ReaderContract(
        substrates=_PAYLOAD_SURFACES, methods=_STRUCTURED,
        locator_keys=frozenset({"json_pointer"}),
        optional_keys=frozenset({"legacy_source_column"})),
    # --- W2-6: DOM readers. `css` is required on all three, so a selector-less entry fails
    # CI rather than matching nothing forever in production.
    "html_text": ReaderContract(
        substrates=_DOM_SURFACES, methods=_DOM_METHOD,
        locator_keys=frozenset({"css"}),
        consults_transforms=True,
        reads_stored_body=True),
    "html_attr": ReaderContract(
        substrates=_DOM_SURFACES, methods=_DOM_METHOD,
        locator_keys=frozenset({"css", "attr"}),
        consults_transforms=True,
        reads_stored_body=True),
    # `position_branch` is a REQUIRED locator key, not an optional hint: it decides the
    # coordinate's licence class (C6) and the archived ladder refuses a read without one, so
    # an entry omitting it would fail per-row at runtime instead of once at projection time.
    # `consults_guards` is FALSE, deliberately. The reader applies the CZ envelope
    # intrinsically (`parse_dms_pair` returns (None, None) outside it) and never calls
    # `guard_admits`, so recording True would let an entry declare `guards: [...]` the
    # runtime silently never evaluates — the exact defect class that made `dms_to_decimal`
    # inadmissible on this same entry. A guard this reader must genuinely honour has to be
    # implemented in the reader body first, and this flag flipped with it.
    "html_point_dms": ReaderContract(
        substrates=_DOM_SURFACES, methods=_DOM_METHOD | _MAP_METHOD,
        locator_keys=frozenset({"css", "attr", "position_branch"}),
        reads_stored_body=True),
    # A coordinate from an ordered [lat_attr, lon_attr] pair of DECIMAL attributes
    # (realitymix's `div#print-map[data-gps-lat][data-gps-lon]`). `consults_guards` is TRUE
    # here and FALSE on `html_point_dms`, and the difference is real rather than an
    # oversight: the DMS reader gets the CZ envelope for free inside `parse_dms_pair`, while
    # a decimal pair passes through no such helper, so this reader calls `guard_admits`
    # itself. Declaring True without calling it is the defect a review caught on the DMS
    # entry — a rail the contract names and the runtime ignores.
    "html_point_attrs": ReaderContract(
        substrates=_DOM_SURFACES, methods=_DOM_METHOD | _MAP_METHOD,
        locator_keys=frozenset({"css", "attr", "position_branch"}),
        consults_guards=True,
        optional_keys=frozenset({"pattern"}),
        reads_stored_body=True),
    # --- W2 reader canon, DOM family. Each one answers a DIFFERENT question about the same
    # node, and every portal-specific fact (which element, which pattern, which label) stays
    # contract data — that is the property that keeps these shared rather than nine forks.
    #
    # A subject header that NESTS chrome states its fact in its OWN text nodes; `html_text`'s
    # deep read appends the chrome's label to the value (remax's `h2.pd-header__address`
    # carries a `mapa` jump-link on 12/12 mined pages). Same substrates and method as
    # `html_text`: a different READ of the same surface, not a new surface.
    "html_own_text": ReaderContract(
        substrates=_DOM_SURFACES, methods=_DOM_METHOD,
        locator_keys=frozenset({"css"}),
        consults_transforms=True,
        reads_stored_body=True),
    # `group` is REQUIRED and never defaulted to "group 0" or "the only group": a pattern
    # may carry several (bazos' slug carries the obec and the PSČ), and picking one by
    # position would make the claim's meaning depend on the order the groups were written.
    "html_regex": ReaderContract(
        substrates=_DOM_SURFACES, methods=_REGEX_METHOD,
        locator_keys=frozenset({"css", "pattern", "group"}),
        consults_transforms=True,
        reads_stored_body=True),
    # The same read over an ATTRIBUTE, and the reader that carries a fact published only in
    # a link. It scans EVERY matching node and lets the PATTERN discriminate, because "the
    # first node matching the selector" is the wrong node about as often as the right one
    # when a cell holds two anchors.
    "html_attr_regex": ReaderContract(
        substrates=_SLUG_SURFACES, methods=_SLUG_METHOD | _REGEX_METHOD,
        locator_keys=frozenset({"css", "attr", "pattern", "group"}),
        consults_transforms=True,
        optional_keys=frozenset({"decode"}),
        reads_stored_body=True),
    # A presence detector: the claim's VALUE is the label the CONTRACT gives the marker and
    # its EVIDENCE is the portal's own text or attribute. `consults_transforms` is FALSE
    # deliberately — normalising a label the contract itself wrote is a no-op with a failure
    # mode, since blur is decided by that label's membership of `precision_cap.blurred_labels`.
    "html_marker": ReaderContract(
        substrates=_DOM_SURFACES, methods=_DECLARED_QUALITY,
        locator_keys=frozenset({"css", "value_label"}),
        optional_keys=frozenset({"attr", "contains"}),
        reads_stored_body=True),
    # --- W2 reader canon, embedded-JSON family: ONE acquisition layer (css + optional attr
    # + optional decode + optional subject match) and five extractors over it. `css` is
    # required on all of them, so a selector-less entry fails CI rather than matching nothing
    # forever; `match` / `exclude_where` / `reject_points` are optional because the same
    # reader serves a plain pointer read and a subject-scoped one, and the direction that can
    # hurt (declaring a match on a reader that narrows nothing) is impossible here — every
    # member of this family resolves its subject through the one shared selector.
    "json_scalar": ReaderContract(
        substrates=_EMBEDDED_JSON_SURFACES,
        methods=_STRUCTURED | _MAP_METHOD | _DECLARED_QUALITY,
        locator_keys=frozenset({"css", "json_pointer"}),
        consults_transforms=True,
        optional_keys=frozenset(
            {"attr", "decode", "exclude_where", "match", "script_match", "then", "value_kind"}),
        reads_stored_body=True),
    "json_regex": ReaderContract(
        substrates=_EMBEDDED_JSON_SURFACES, methods=_REGEX_METHOD,
        locator_keys=frozenset({"css", "json_pointer", "pattern", "group"}),
        consults_transforms=True,
        optional_keys=frozenset(
            {"attr", "decode", "exclude_where", "match", "script_match", "then"}),
        reads_stored_body=True),
    "json_bool": ReaderContract(
        substrates=_EMBEDDED_JSON_SURFACES, methods=_DECLARED_QUALITY,
        locator_keys=frozenset({"css", "json_pointer", "labels"}),
        optional_keys=frozenset(
            {"attr", "decode", "exclude_where", "match", "script_match", "then"}),
        reads_stored_body=True),
    # `position_branch` is a required key for the same reason it is on `html_point_dms`: it
    # decides the coordinate's licence class (C6) and the archived ladder refuses a read
    # without one, so an entry omitting it must fail at projection time rather than per row.
    "json_point": ReaderContract(
        substrates=_EMBEDDED_JSON_SURFACES, methods=_STRUCTURED | _MAP_METHOD,
        locator_keys=frozenset({"css", "position_branch"}),
        consults_guards=True,
        optional_keys=frozenset(
            {"attr", "decode", "exclude_where", "feature", "lat_pointer", "lon_pointer", "match", "reject_points", "script_match", "then"}),
        reads_stored_body=True),
    # The feature TYPE is the declared precision (Point -> a pin, LineString -> a segment,
    # Circle -> a centre plus a declared radius), so ONE reader serves the coordinate entry
    # and the uncertainty-geometry entry over the same feature; `position_branch` is checked
    # in the coordinate arm rather than declared required here, because the shape entry has
    # no position to license.
    "json_geometry": ReaderContract(
        substrates=_EMBEDDED_JSON_SURFACES, methods=_MAP_METHOD,
        locator_keys=frozenset({"css", "then"}),
        consults_guards=True,
        optional_keys=frozenset(
            {"attr", "decode", "exclude_where", "geometry_reader", "match", "position_branch", "reject_zoom_at_or_below", "script_match", "zoom_pointer"}),
        reads_stored_body=True),
    # One level of a schema.org BreadcrumbList geo chain, anchored on a contract-declared
    # kraj slug rather than an absolute position — the offset moves with the category path,
    # so `positions: [5,6,7,8]` is wrong on any two-level category.
    "json_breadcrumb": ReaderContract(
        substrates=_JSONLD_SURFACES, methods=_BREADCRUMB_METHOD,
        locator_keys=frozenset({"css", "type", "anchor_slugs", "level"}),
        consults_transforms=True,
        optional_keys=frozenset({"attr", "decode", "script_match"}),
        reads_stored_body=True),
}

# The substrate axis on its own — what `claims_intake`'s module docstring points at, and
# the property the fleet-wide W1 gate used to assert directly.
READER_SUBSTRATES: dict[str, frozenset[str]] = {
    name: spec.substrates for name, spec in READER_CONTRACTS.items()
}
_TRANSFORM_READERS = ", ".join(
    sorted(r for r, spec in READER_CONTRACTS.items() if spec.consults_transforms))
_GUARD_READERS = ", ".join(
    sorted(r for r, spec in READER_CONTRACTS.items() if spec.consults_guards))

# The `transform` / `guards` vocabularies the extractor actually IMPLEMENTS
# (`claims_intake.TRANSFORMS` / `.GUARDS`; a transform is named before its `:arg`). 02
# §2.1.2's vocabulary is deliberately larger — entries are declared ahead of the wave that
# will run them — and an unimplemented name is a silent no-op wherever it is executed:
# `guards: [reject_outside_cz_bbox]` misspelled once would drop the CZ bbox check with no
# error anywhere. So the names are enum-checked on an EXECUTABLE entry (one naming a
# reader) and left free on an inert one, which nothing executes. Implementedness is only
# half the question: the entry's own reader has to CONSULT the axis
# (`READER_CONTRACTS[...].consults_transforms` / `.consults_guards`), or an implemented
# name is exactly as inert as a misspelt one.
IMPLEMENTED_TRANSFORMS = frozenset({
    "sentinel_drop", "psc_normalise", "split_cp_co", "strip_prefix",
    # W2: selecting a typed part out of a whole address the page states in ONE string. They
    # are transforms rather than readers because the READ is unchanged (`html_text` /
    # `html_attr` over one node) and only the part this entry claims differs — one reader
    # per admin level would be nine forks of the same act.
    "address_part_street", "address_part_obec", "address_part_okres",
    "address_part_house_number", "split_paren_okres", "comma_segment",
    # W1-c: a numbered/hyphenated městský obvod is never the town (R4), and the trailing
    # segment of an address is sometimes a country rather than an obec (R1's `country`).
    "statutory_city_obec", "address_part_country",
    # idnes@4: the same `country` type off a STRUCTURED alpha-2 field instead of an address
    # tail — no name table to fall outside of, and CZ dropped rather than claimed.
    "foreign_country_code",
})
IMPLEMENTED_GUARDS = frozenset({"reject_outside_cz_bbox"})


class ContractError(RuntimeError):
    """A contract file is invalid, or a load would violate the append-only lifecycle."""


@dataclass(frozen=True, slots=True)
class ContractEntry:
    entry_id: str
    surface: str
    page_kind: str
    locator: dict[str, Any]
    claim_type: str
    extraction_method: str
    subject_scope: dict[str, Any]
    transform: list[str]
    precision_map: dict[str, Any]
    default_granularity: str | None
    default_position_source: str | None
    default_blur_evidence: str
    default_licence_class: str
    guards: list[str]
    notes: str | None

    @property
    def reader(self) -> str:
        return str(self.locator["reader"])


@dataclass(frozen=True, slots=True)
class PortalContract:
    source: str
    version: int
    sha256: bytes
    exclusion_zones: list[dict[str, Any]]
    # The `portal_contracts.fetch_config` projection: the two unhashed blocks, verbatim, so
    # an operator reads in psql exactly what the deployed file says. The column keeps its
    # name until W4 renames or drops it.
    fetch_config: dict[str, Any]
    entries: list[ContractEntry] = field(default_factory=list)
    # `persistence.volatile_paths`, parsed: {page_kind: profile}. 02 §2.3.2 P1 —
    # `payload_sha256` addresses a NORMALISED body, and this is what normalises it, so
    # a change to it moves a PERMANENT content address. It is modelled here rather than
    # left inside the `fetch_config` blob so the CI `--check` gate refuses a bad
    # selector (see `payload_norm.parse_profile_block`) instead of projecting it and
    # letting the silent normaliser no-op it in production.
    volatile_profiles: dict[str, payload_norm.VolatileProfile] = field(default_factory=dict)
    path: Path | None = None


# ------------------------------------------------------------------ parsing

def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ContractError(f"{where}: missing required key '{key}'")
    return mapping[key]


def _member(value: Any, allowed: frozenset[str], where: str, key: str) -> str:
    text = str(value)
    if text not in allowed:
        raise ContractError(
            f"{where}: {key}='{text}' is not a member of the enum "
            f"({', '.join(sorted(allowed))})")
    return text


# The ONE page kind whose bodies are stored. `claims_intake._BODY_JOIN` selects
# `p.page_kind = 'detail'` and nothing else: index bodies are never archived (their keys are
# week-stamped, so the storage cap bounds detail and not index — the 2026-08-16 operator
# decision), and no scraper writes a map, archive, snapshot or gazetteer body at all. A page
# entry declared for any other kind is therefore unreachable by construction, which is not a
# shape a contract may describe: `ceskereality@6` shipped a `page_kind: map` entry reading a
# `/mapa/` marker set that exists in no payload row and in no scraper, and it read as a live
# precision signal for the portal. Widen this the day a lane stores that kind, never before.
STORED_PAGE_KIND = "detail"


def _check_executable(
    reader: str,
    *,
    surface: str,
    method: str,
    page_kind: str,
    locator: dict[str, Any],
    transforms: list[str],
    guards: list[str],
    where: str,
) -> None:
    """What an entry may say — checked at projection time.

    Every entry names a reader now (W1-c), so every entry is executed by `claims_intake` on
    every listing of its portal and every clause below is load-bearing rather than advisory.
    Each one is one thing the reader in `READER_CONTRACTS` actually does with the entry:
    declaring past it is either a silent no-op or a provenance the claim will not carry.
    """
    spec = READER_CONTRACTS.get(reader)
    if spec is None:
        raise ContractError(
            f"{where}: locator.reader='{reader}' is not a registered reader "
            f"({', '.join(sorted(READER_CONTRACTS))})")
    if surface not in spec.substrates:
        raise ContractError(
            f"{where}: reader '{reader}' reads {', '.join(sorted(spec.substrates))}; "
            f"'{surface}' is not one of its substrates")
    if method not in spec.methods:
        raise ContractError(
            f"{where}: reader '{reader}' extracts by {', '.join(sorted(spec.methods))}; "
            f"extraction_method='{method}' would stamp every claim with a provenance the "
            f"reader does not perform (00 §3)")
    if spec.reads_stored_body and page_kind != STORED_PAGE_KIND:
        raise ContractError(
            f"{where}: reader '{reader}' reads a STORED PAGE BODY and the lane stores only "
            f"page_kind='{STORED_PAGE_KIND}' bodies, so page_kind='{page_kind}' can never "
            f"be executed — `page_entries` would never select it and no run would count "
            f"the miss")
    for key in sorted(spec.locator_keys):
        if not locator.get(key):
            raise ContractError(
                f"{where}: reader '{reader}' addresses its value through "
                f"locator.{key}, which this entry does not name; the extractor indexes it "
                f"unguarded and would KeyError on the first row of this portal")
    # The other direction, and it is the one that shipped a defect: a key the reader never
    # looks up reads as a declared rail and is a no-op. `bzs.det.link_pin` named a
    # `pattern` that `html_point_attrs` did not consult, so the entry described a pin the
    # lane could not mint and nothing said so — the contract, the projection and the tests
    # all agreed it was live. A reader's appetite is data on `READER_CONTRACTS`; a locator
    # that says more than the reader hears is refused here, before it can be believed.
    unread = sorted(set(locator) - spec.appetite)
    if unread:
        raise ContractError(
            f"{where}: reader '{reader}' never reads locator."
            f"{', locator.'.join(unread)}; it consults "
            f"{', '.join(sorted(spec.appetite))}, so the declaration is inert — either the "
            f"reader widens or the key goes")

    for transform_spec in transforms:
        name = transform_spec.partition(":")[0]
        if not spec.consults_transforms:
            raise ContractError(
                f"{where}: reader '{reader}' never applies transforms, so '{name}' would "
                f"silently not run; only {_TRANSFORM_READERS} normalise their value")
        if name not in IMPLEMENTED_TRANSFORMS:
            raise ContractError(
                f"{where}: transform '{name}' is not implemented by the extractor "
                f"({', '.join(sorted(IMPLEMENTED_TRANSFORMS))}); an executable entry may "
                f"not declare a normaliser that would silently not run")
    for guard in guards:
        if not spec.consults_guards:
            raise ContractError(
                f"{where}: reader '{reader}' never evaluates guards, so '{guard}' would "
                f"silently not reject; only {_GUARD_READERS} admit a point through one")
        if guard not in IMPLEMENTED_GUARDS:
            raise ContractError(
                f"{where}: guard '{guard}' is not implemented by the extractor "
                f"({', '.join(sorted(IMPLEMENTED_GUARDS))}); an executable entry may not "
                f"declare a reject rule that would silently not reject")


def parse_entry(raw: dict[str, Any], *, source: str, index: int) -> ContractEntry:
    where = f"{source} entry #{index}"
    entry_id = str(_require(raw, "id", where))
    prefix = EXTRACTOR_PREFIXES[source]
    if not entry_id.startswith(prefix):
        raise ContractError(
            f"{where}: extractor id '{entry_id}' must carry this portal's permanent "
            f"prefix '{prefix}' (02 §2.2 preamble)")
    where = f"{source}:{entry_id}"

    retired = [key for key in RETIRED_ENTRY_KEYS if key in raw]
    if retired:
        raise ContractError(
            f"{where}: {', '.join(retired)} — nothing enforces these, so the entry states a "
            f"rail the runtime does not run; delete the key (W1-c R3)")
    if "legacy_column" in (raw.get("locator_kind"), raw.get("extraction_method")):
        raise ContractError(
            f"{where}: the legacy_column surface is retired — the intake reads raw_json and "
            f"the stored page body, and a `listings` column is neither (rule 25)")

    # 02 §2.1.2 rule 4: locator_kind IS the surface; extraction_method is a separate,
    # mandatory axis and is never derived from it.
    surface = _member(_require(raw, "locator_kind", where), CLAIM_SURFACES, where, "locator_kind")
    method = _member(
        _require(raw, "extraction_method", where), EXTRACTION_METHODS, where, "extraction_method")
    page_kind = _member(_require(raw, "page_kind", where), PAGE_KINDS, where, "page_kind")
    claim_type = _member(_require(raw, "claim_type", where), CLAIM_TYPES, where, "claim_type")

    locator = dict(_require(raw, "locator", where))
    if not locator:
        raise ContractError(f"{where}: locator must address something (02 §2.1.3)")

    licence = _member(
        raw.get("licence_class", "portal"), CONTRACT_LICENCE_CLASSES, where, "licence_class")
    blur = _member(raw.get("blur_evidence", "none"), BLUR_EVIDENCE, where, "blur_evidence")
    if blur in {"detected", "both"}:
        raise ContractError(
            f"{where}: blur_evidence='{blur}' — the collision detector is the only writer "
            f"of 'detected' (02 §2.1.2)")

    precision_cap = dict(raw.get("precision_cap") or {})
    prior = dict(raw.get("prior") or {})
    if "granularity" in raw or "position_source" in raw:
        raise ContractError(
            f"{where}: a contract entry never ASSIGNS an axis — it emits precision_cap + "
            f"prior and the resolver assigns (02 §2.1.2 rule 2)")
    # 02 §2.1.2 rule 1: no coordinate without a cap and a licence class.
    if claim_type == "coordinate" and not precision_cap:
        raise ContractError(
            f"{where}: a coordinate entry must declare precision_cap (02 §2.1.2 rule 1)")

    granularity = prior.get("granularity")
    if granularity is not None:
        granularity = _member(granularity, GRANULARITIES, where, "prior.granularity")
    position_source = prior.get("position_source")
    if position_source is not None:
        position_source = _member(
            position_source, POSITION_SOURCES, where, "prior.position_source")
    if prior.get("match_confidence") is not None:
        _member(prior["match_confidence"], MATCH_CONFIDENCES, where,
                "prior.match_confidence")
    if locator.get("claim_confidence") is not None:
        # 06 §6.1.1: a class-B legacy column is capped at `medium`. The cap is contract
        # data (the reader never invents one), so it is validated here.
        _member(locator["claim_confidence"], MATCH_CONFIDENCES, where,
                "locator.claim_confidence")

    transforms = [str(t) for t in (raw.get("transform") or [])]
    guards = [str(g) for g in (raw.get("guards") or [])]

    # EVERY entry names a reader (W1-c R1). "Declared ahead of the wave that will run it"
    # was how a contract grew entries nothing executed — projected, counted in every census,
    # extracting nothing — so the ahead-declaration is gone and with it the two-tier
    # validation it forced: an entry the runtime cannot execute is refused here.
    reader = locator.get("reader")
    if not reader:
        raise ContractError(
            f"{where}: entry names no locator.reader; every entry is executed, so an entry "
            f"nothing can run is refused rather than projected "
            f"({', '.join(sorted(READER_CONTRACTS))})")
    _check_executable(str(reader), surface=surface, method=method, page_kind=page_kind,
                      locator=locator, transforms=transforms, guards=guards, where=where)

    precision_map: dict[str, Any] = {}
    if precision_cap:
        precision_map["precision_cap"] = precision_cap
    if prior:
        precision_map["prior"] = prior
    # Hoisted so the declared-quality reader can read the blurred-label set without
    # knowing where in the cap object it was declared. It is the calibration set for the
    # collision detector (00 §1.3), so it is data on the contract, never a code constant.
    if precision_cap.get("blurred_labels"):
        labels = [str(x) for x in precision_cap["blurred_labels"]]
        if claim_type != "precision_declaration":
            raise ContractError(
                f"{where}: blurred_labels only belongs on a precision_declaration entry "
                f"(00 §2.2) — `blur_hint` went with the eleven-type vocabulary")
        precision_map["blurred_labels"] = labels

    return ContractEntry(
        entry_id=entry_id,
        surface=surface,
        page_kind=page_kind,
        locator=locator,
        claim_type=claim_type,
        extraction_method=method,
        subject_scope=dict(raw.get("subject_scope") or {}),
        transform=transforms,
        precision_map=precision_map,
        default_granularity=granularity,
        default_position_source=position_source,
        default_blur_evidence=blur,
        default_licence_class=licence,
        guards=guards,
        notes=raw.get("notes"),
    )


# Every top-level key `parse_contract` understands. An unknown key is a REFUSAL, not a
# shrug: every key fails open the same way (a typo'd `extractoins:` projects a header with
# no entries and a silent hash change). Adding a key to the format means adding it here —
# CI's `--check` run is the gate.
#
# SIX KEYS (W1-c R2). The eight that went — identity_ladder, precision_caps,
# precision_priors, extractor_runtime, fetch, payload_schema_detector,
# pin_collision_semantics, contract_sha256 — were read by nothing: a ladder no resolver
# consulted, caps and priors superseded by `location_field_policy` + the entry's own
# `prior:`, a fetch block only a deleted audit script read, a self-declared hash
# (`contract_body_hash` is taken over the bytes, so a file can never carry its own), and
# two blocks that were documentation. `portal_contracts.identity_ladder` /
# `.precision_priors` keep their NOT NULL defaults until W4 drops the columns; this loader
# stops writing them.
_TOP_LEVEL_KEYS = frozenset({
    "portal", "contract_version", "persistence", "exclusion_zones", "regressions",
    "extractions",
})

# A HASH COVERS WHAT IT GOVERNS. `contract_sha256` governs the EXTRACTION half of this
# file — it is the immutability gate on `portal_contract_entries`, and `contract_version`,
# the thing a mismatch demands you bump, is what `extractor_version` and every claim's
# `contract_entry_id` name. One top-level key is not extraction and is therefore not
# hashed:
#
# `persistence` is ARCHIVE configuration (W2a-3e): `volatile_paths` decides the projection
# `payload_sha256` is taken over, and `version_cap` is retention. Neither reaches a claim.
# Hashed, they made an archive edit re-version the extractor, and re-versioning the
# extractor re-inserts the claims corpus: `location_claim_fingerprint` (migration 386)
# takes `extractor_version` and `contract_entry_id`, and its UNIQUE index (migration 382)
# is what dedupes an incremental re-walk. In August 2026 that was 5.1M rows / 2.6 GB of
# `location_claims`, duplicated for a selector edit, in a subsystem with ~4 GB of
# allowance left — and once per future tweak. The profiles keep their own identity
# instead: `payload_norm.profile_digest`, which moves iff the projection moves.
#
# A top-level key is never indented, so this anchored filter cannot reach a nested
# `persistence:` inside an extraction. The block filter takes the key's line plus every
# line under it that is indented or blank, which is exactly YAML's own block extent — the
# narrative comments live inside the block and travel with it.
#
# CHANGING WHAT IS EXCLUDED RE-DIALECTS EVERY STORED HASH. Rows projected under the old
# definition no longer match, and `project()` refuses them by design. Migration 408 is
# that one-time restatement for the nine contracts live when `persistence` was excluded;
# a further exclusion needs the same treatment.
_PERSISTENCE_BLOCK = re.compile(
    rb"^persistence[ \t]*:.*(?:\r?\n|$)(?:(?:[ \t][^\n]*)?(?:\r?\n|$))*", re.MULTILINE)


def contract_body_hash(body: bytes) -> bytes:
    """The bytes `contract_sha256` is taken over: the file, minus the one block that is not
    extraction — its `persistence:` block.

    The `shadow:` line used to be subtracted here too (W1-b deleted the flag). No contract
    file ever carried one, so the governed bytes — and every stored hash — are unchanged;
    `contracts.lock.json` is the assertion of that.
    """
    governed = _PERSISTENCE_BLOCK.sub(b"", body)
    return hashlib.sha256(governed).digest()


# An unindented line ends the block for `_PERSISTENCE_BLOCK`, and a comment at column 0
# is unindented — so a note written flush-left inside `persistence:` would silently
# re-govern everything below it. The next selector edit would then move the hash,
# `project()` would refuse it, and the refusal says `persistence:` is excluded and
# therefore cannot be what moved it. That sentence would be false in exactly this state,
# and the operator following it bumps `contract_version` — spending the 2.6 GB of
# duplicated `location_claims` this exclusion exists to save. Refuse the shape instead;
# an indented comment is the normal one and still travels with the block.
_COL0_COMMENT = re.compile(rb"^#")
_TOP_LEVEL_KEY = re.compile(rb"^[A-Za-z_]")


def _refuse_unindented_comment_in_persistence(path: Path, body: bytes) -> None:
    """`_PERSISTENCE_BLOCK` ends at the first unindented line, and a comment at column 0
    is unindented — so a note written flush-left inside `persistence:` truncates the
    exclusion and silently re-governs everything below it. The next selector edit then
    moves `contract_sha256`, `project()` refuses it, and the refusal says `persistence:`
    is excluded and so cannot be what moved it. That sentence is false in exactly this
    state, and an operator following it bumps `contract_version` — spending the 2.6 GB of
    duplicated `location_claims` this exclusion exists to save. Refuse the shape; an
    indented comment is the normal one and travels with the block."""
    lines = body.split(b"\n")
    for i, line in enumerate(lines):
        if not re.match(rb"^persistence[ \t]*:", line):
            continue
        for follower in lines[i + 1:]:
            if _COL0_COMMENT.match(follower):
                raise ContractError(
                    f"{path}: a comment at column 0 inside `persistence:` ends the block, "
                    f"so everything after it silently re-enters contract_sha256 — and the "
                    f"refusal you would then get claims `persistence:` cannot be what "
                    f"moved the hash. Indent it to keep it inside the block.")
            if _TOP_LEVEL_KEY.match(follower):
                break


def _check_shape(source: str, entries: list[ContractEntry], *, where: str) -> None:
    """ONE entry per claim type, and a town entry among them (rule 25, W1-c R1).

    Both rails are about the RESOLVER, not about tidiness. Two entries of one type make the
    contract a vote the survivorship policy never asked for: the two claims reach S7 with
    the same (source, extraction_method), so which one wins is the order the DB happened to
    return them in — "the portal says X" becomes "one of the portal's two readers says X".
    A contract with no town entry cannot satisfy the invariant the whole programme is
    measured by, and a contract whose town entry names no reader satisfies it on paper only
    — which is how nine portals shipped with a town-coverage hole nobody could see.
    """
    by_type: dict[str, list[str]] = {}
    for entry in entries:
        by_type.setdefault(entry.claim_type, []).append(entry.entry_id)
    duplicated = {t: ids for t, ids in by_type.items() if len(ids) > 1}
    if duplicated:
        detail = "; ".join(f"{t}: {', '.join(ids)}" for t, ids in sorted(duplicated.items()))
        raise ContractError(
            f"{where}: {source} declares more than one entry per claim type ({detail}); "
            f"a claim type has exactly one carrier per portal")
    if MANDATORY_CLAIM_TYPE not in by_type:
        raise ContractError(
            f"{where}: {source} declares no {MANDATORY_CLAIM_TYPE} entry; every contract "
            f"states the town (rule 25 — every active Czech listing has one)")


def parse_contract(path: Path) -> PortalContract:
    import yaml  # dev/CI-only dependency; see the module docstring.

    body = path.read_bytes()
    _refuse_unindented_comment_in_persistence(path, body)
    doc = yaml.safe_load(body.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ContractError(f"{path}: not a YAML mapping")
    unknown = sorted(set(doc) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ContractError(
            f"{path}: unknown top-level key(s) {', '.join(unknown)}; known keys are "
            + ", ".join(sorted(_TOP_LEVEL_KEYS))
        )
    source = str(_require(doc, "portal", str(path)))
    if source not in EXTRACTOR_PREFIXES:
        raise ContractError(f"{path}: unknown portal '{source}'")
    version = int(_require(doc, "contract_version", str(path)))
    if version < 1:
        raise ContractError(f"{path}: contract_version must be >= 1")

    persistence = dict(doc.get("persistence") or {})
    # The load-time gate a selector out of YAML needs, run HERE because this is the
    # last place refusing is allowed: `payload_norm.normalise` is silent by contract
    # (a malformed selector raises inside selectolax and `:contains()` SEGFAULTS it —
    # exit 139, uncatchable), so a typo that reaches it does not fail, it quietly
    # stops stripping and the portal's measured change rate moves for no reason
    # anybody can see. The test suite parses every contract through here, so a typo
    # fails `test.yml` on the push that introduces it; `--check` is the same gate on
    # demand, and `--load` runs it again at deploy time.
    try:
        volatile_profiles = payload_norm.parse_volatile_paths(
            persistence.get("volatile_paths"),
            where=f"{path.name}:persistence.volatile_paths",
            page_kinds=PAGE_KINDS,
        )
    except payload_norm.ProfileError as exc:
        raise ContractError(str(exc)) from exc

    entries = [
        parse_entry(raw, source=source, index=i)
        for i, raw in enumerate(_require(doc, "extractions", str(path)))
    ]
    seen: set[str] = set()
    for entry in entries:
        if entry.entry_id in seen:
            raise ContractError(f"{path}: duplicate extractor id '{entry.entry_id}'")
        seen.add(entry.entry_id)
    _check_shape(source, entries, where=str(path))

    return PortalContract(
        source=source,
        version=version,
        # The file's `contract_sha256` field is documentation only — a file cannot carry
        # its own hash. The projection hashes the bytes on disk, which is what makes the
        # git artefact and the DB row provably identical (02 §2.1.8 mechanism 1) — minus
        # the two blocks that are not extraction (`contract_body_hash`).
        sha256=contract_body_hash(body),
        exclusion_zones=list(doc.get("exclusion_zones") or []),
        fetch_config={
            # Projected VERBATIM, as the file writes it: the DB pair is a projection of
            # the git artefact (02 §2.1.8), so a normalised-on-the-way-in copy would be a
            # second dialect of the same fact. What the runtime applies is
            # `volatile_profiles`, parsed from exactly these bytes by exactly this parser
            # — never from this projection, which exists to be read in psql.
            # `contract_sha256` does NOT cover this block (`contract_body_hash`), so the
            # projection is refreshed in place by `project()` rather than being pinned to
            # the version that first carried it.
            "persistence": persistence,
            "regressions": doc.get("regressions") or [],
        },
        entries=entries,
        volatile_profiles=volatile_profiles,
        path=path,
    )


def load_all(directory: Path = CONTRACT_DIR) -> list[PortalContract]:
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise ContractError(f"no contract files under {directory}")
    contracts = [parse_contract(p) for p in paths]
    # ONE file per portal. `portal_contracts` has `unique (source, version)`, so two files
    # naming one portal at different versions would BOTH project and the last one to
    # activate would win silently; at the same version the second would be refused for a
    # hash mismatch and read as an unexplained drift. Downstream of this parse the same
    # duplicate makes `payload_norm.load_contract_profiles` pair one file's rules with
    # another file's provenance (it refuses too, for that reason).
    seen: dict[str, Path] = {}
    for contract in contracts:
        if contract.source in seen and contract.path is not None:
            raise ContractError(
                f"{contract.path}: portal '{contract.source}' is already declared by "
                f"{seen[contract.source].name} — one contract file per portal")
        seen[contract.source] = contract.path if contract.path is not None else directory
    return contracts


def extractor_version(contract: PortalContract | str, version: int | None = None) -> str:
    """02 §2.1.8: every claim carries `contract:<portal>@<version>`."""
    if isinstance(contract, PortalContract):
        return f"contract:{contract.source}@{contract.version}"
    return f"contract:{contract}@{version}"


# ------------------------------------------------------------------ projection

_HEADER_SELECT_SQL = """
    SELECT id, encode(contract_sha256, 'hex'), is_active, fetch_config
    FROM portal_contracts
    WHERE source = %(source)s AND version = %(version)s
"""

# The one column `project()` may rewrite on an already-loaded version, and only because
# `contract_sha256` no longer covers `persistence` (`contract_body_hash`): an edit there
# is deliberately not a version bump, so without this the psql-readable copy of a portal's
# `volatile_paths` would silently freeze at whatever the version first shipped with while
# the scrape applied the file. Everything else in `fetch_config` IS hashed, so on this
# path it is byte-identical by construction and the write cannot smuggle it.
_FETCH_CONFIG_UPDATE_SQL = """
    UPDATE portal_contracts SET fetch_config = %(fetch_config)s WHERE id = %(id)s
"""

# `identity_ladder` / `precision_priors` are omitted deliberately: both columns are
# `not null default '{}'`, nothing reads them, and W1-c stopped parsing the keys. W4 drops
# the columns; until then the defaults write the empty value the rows would carry anyway.
_HEADER_INSERT_SQL = """
    INSERT INTO portal_contracts
        (source, version, contract_sha256, git_ref, exclusion_zones, fetch_config,
         is_active)
    VALUES (%(source)s, %(version)s, decode(%(sha256)s, 'hex'), %(git_ref)s,
            %(exclusion_zones)s, %(fetch_config)s, false)
    RETURNING id
"""

# `cardinality` / `required` / `on_conflict` are omitted for the same reason the header
# omits two of its own columns: `not null default`, read by nothing, key refused since
# W1-c. W4 drops them.
_ENTRY_INSERT_SQL = """
    INSERT INTO portal_contract_entries
        (contract_id, entry_id, surface, page_kind, locator, claim_type, extraction_method,
         subject_scope, transform, precision_map, default_granularity,
         default_position_source, default_blur_evidence, default_licence_class,
         guards, notes)
    VALUES (%(contract_id)s, %(entry_id)s, %(surface)s, %(page_kind)s, %(locator)s,
            %(claim_type)s, %(extraction_method)s, %(subject_scope)s, %(transform)s,
            %(precision_map)s, %(default_granularity)s, %(default_position_source)s,
            %(default_blur_evidence)s, %(default_licence_class)s, %(guards)s, %(notes)s)
    ON CONFLICT (contract_id, entry_id) DO NOTHING
"""

_ENTRY_IDS_SQL = "SELECT entry_id FROM portal_contract_entries WHERE contract_id = %(id)s"

_DEACTIVATE_SQL = """
    UPDATE portal_contracts SET is_active = false, retired_at = coalesce(retired_at, now())
    WHERE source = %(source)s AND is_active AND id <> %(keep)s
"""

_ACTIVATE_SQL = """
    UPDATE portal_contracts SET is_active = true, retired_at = NULL WHERE id = %(id)s
"""

# RETRACTION IS A DELETE (W1-b, migrations 497 + 498). The append-only ledger and the
# `location_claims_live` view that subtracted its rows are gone: a contract version that
# misread the portal produced no evidence, and teaching every present and future reader to
# subtract it cost three views and a correlated NOT EXISTS on the resolver's hot read.
#
# THE TARGET IS RESOLVED FIRST, and an empty resolution is an ERROR. A typo'd portal, a
# version that was never projected or an `--extractor-id` that names no entry used to be
# indistinguishable from "that version had no claims": both printed `deleted=0` and exited 0,
# which reads as "done". An operator retracting the wrong thing must be told so.
_RETRACT_ENTRIES_SQL = """
    SELECT pce.id
      FROM portal_contract_entries pce
      JOIN portal_contracts pc ON pc.id = pce.contract_id
     WHERE pc.source = %(source)s AND pc.version = %(version)s
       AND (%(extractor_id)s::text IS NULL OR pce.entry_id = %(extractor_id)s)
"""

# BOUNDED BATCHES, each its own transaction. "The contract's claims" is every listing the
# portal has ever had — 5 M rows on sreality — and one atomic DELETE of that size is a
# statement that spends its whole timeout and then rolls back, doing nothing, forever. A
# partial retraction is the right failure mode here: the rows that went are gone, their
# listings are queued, and re-running finishes the job (the predicate is the same).
#
# `ctid` is the bound: LIMIT inside a DELETE needs a subquery, and the physical row id is the
# cheapest key that survives one. Safe because nothing UPDATEs `location_claims` — the intake
# only ever INSERTs — so a row's ctid cannot move under the statement.
#
# One statement per batch, so the delete and its re-resolve enqueue cannot separate.
# `claim_insert` is reused deliberately: the queue's reason is a diagnostic label and the
# drain rebuilds the whole projection row whatever it says, so a retraction-only value would
# be a vocabulary entry nothing branches on.
# The enqueue BUMPS (W2-a2): a retraction is evidence changing under a listing that may
# already be queued, and the drain's delete is bounded by the `enqueued_at` its slice
# claimed, so a bump is what keeps the row queued until it is resolved WITHOUT these claims.
_RETRACT_BATCH_SQL = """
    WITH victims AS (
        SELECT ctid FROM location_claims
         WHERE contract_entry_id = ANY(%(entry_ids)s)
         LIMIT %(batch_size)s
    ), deleted AS (
        DELETE FROM location_claims c
         USING victims v
         WHERE c.ctid = v.ctid
        RETURNING c.listing_id
    ), enqueued AS (
        INSERT INTO dirty_locations (listing_id, reason)
        SELECT DISTINCT listing_id, 'claim_insert' FROM deleted
        ON CONFLICT (listing_id) DO UPDATE
           SET enqueued_at = now(), reason = EXCLUDED.reason,
               attempts = 0, next_eligible_at = now()
        RETURNING listing_id
    )
    SELECT (SELECT count(*) FROM deleted), (SELECT count(*) FROM enqueued)
"""

# Bounds ONE batch, not the retraction: the loop below may run for as long as the corpus
# needs, but no single statement may hang a pooler backend.
_RETRACT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '300s'"

RETRACT_BATCH_ROWS = 50_000

_RETIRE_SQL = """
    UPDATE portal_contracts SET is_active = false, retired_at = now()
    WHERE source = %(source)s AND version = %(version)s
"""

# `location_claims` + `dirty_locations` are here because `retract` writes the second from
# the first: a pre-flight that only checked the contract tables would let a retraction fail
# halfway with a bare UndefinedTable instead of the schema-not-applied message.
_RELATIONS = ("portal_contracts", "portal_contract_entries",
              "location_claims", "dirty_locations")
_REGCLASS_SQL = "SELECT to_regclass(%(name)s)"


def missing_relations(conn: psycopg.Connection) -> list[str]:
    missing: list[str] = []
    with conn.cursor() as cur:
        for name in _RELATIONS:
            cur.execute(_REGCLASS_SQL, {"name": name})
            if cur.fetchone()[0] is None:
                missing.append(name)
    return missing


def project(
    conn: psycopg.Connection,
    contract: PortalContract,
    *,
    git_ref: str,
    activate: bool = True,
) -> tuple[int, int]:
    """Idempotent per (source, contract_version). Returns (contract_id, entries_inserted).

    Re-running with the same bytes is a no-op; re-running with different GOVERNED bytes
    under the same version raises — entries are immutable and a change is a new version
    (02 §2.1.8). Bytes outside the hash (`contract_body_hash`: the `persistence:` block)
    are the exception by design: a `persistence` edit is not a version bump, so the row's
    `fetch_config` is refreshed in place to keep the psql-readable projection equal to the
    file the scrape is actually applying.
    """
    sha_hex = contract.sha256.hex()
    inserted = 0
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(_HEADER_SELECT_SQL, {"source": contract.source,
                                             "version": contract.version})
            row = cur.fetchone()
            if row is None:
                cur.execute(_HEADER_INSERT_SQL, {
                    "source": contract.source,
                    "version": contract.version,
                    "sha256": sha_hex,
                    "git_ref": git_ref,
                    "exclusion_zones": psycopg.types.json.Jsonb(contract.exclusion_zones),
                    "fetch_config": psycopg.types.json.Jsonb(contract.fetch_config),
                })
                contract_id = int(cur.fetchone()[0])
            else:
                contract_id, stored_sha, _is_active, stored_config = (
                    int(row[0]), row[1], row[2], row[3])
                if stored_sha != sha_hex:
                    raise ContractError(
                        f"{contract.source}@{contract.version} is already loaded with a "
                        f"different sha256 ({stored_sha} on record, {sha_hex} on disk). "
                        f"Contract entries are immutable: bump contract_version "
                        f"(02 §2.1.8). NOTE: the `persistence:` block is excluded from "
                        f"this hash (W2a-3e), so an edit to it is NOT what moved it — "
                        f"but a row projected before that exclusion holds a hash over "
                        f"the whole file and needs migration 408's one-time "
                        f"restatement.")
                if stored_config != contract.fetch_config:
                    cur.execute(_FETCH_CONFIG_UPDATE_SQL, {
                        "id": contract_id,
                        "fetch_config": psycopg.types.json.Jsonb(contract.fetch_config),
                    })
                    LOG.info("CONTRACT persistence refreshed %s@%d id=%d",
                             contract.source, contract.version, contract_id)

            cur.execute(_ENTRY_IDS_SQL, {"id": contract_id})
            known = {r[0] for r in cur.fetchall()}
            for entry in contract.entries:
                if entry.entry_id in known:
                    continue
                cur.execute(_ENTRY_INSERT_SQL, {
                    "contract_id": contract_id,
                    "entry_id": entry.entry_id,
                    "surface": entry.surface,
                    "page_kind": entry.page_kind,
                    "locator": psycopg.types.json.Jsonb(entry.locator),
                    "claim_type": entry.claim_type,
                    "extraction_method": entry.extraction_method,
                    "subject_scope": psycopg.types.json.Jsonb(entry.subject_scope),
                    "transform": psycopg.types.json.Jsonb(entry.transform),
                    "precision_map": psycopg.types.json.Jsonb(entry.precision_map),
                    "default_granularity": entry.default_granularity,
                    "default_position_source": entry.default_position_source,
                    "default_blur_evidence": entry.default_blur_evidence,
                    "default_licence_class": entry.default_licence_class,
                    "guards": entry.guards,
                    "notes": entry.notes,
                })
                inserted += 1

            if activate:
                # Order matters: the partial unique index allows exactly one active
                # header per source, so the incumbent is stood down first.
                cur.execute(_DEACTIVATE_SQL, {"source": contract.source, "keep": contract_id})
                cur.execute(_ACTIVATE_SQL, {"id": contract_id})
    return contract_id, inserted


@dataclass(frozen=True, slots=True)
class Retraction:
    """What `retract` did: claim rows deleted, listings that newly queued for
    re-resolution (one the queue already holds is not counted twice — it is going to be
    rebuilt either way), and how many bounded batches it took."""

    deleted: int
    enqueued: int
    batches: int


def retract(
    conn: psycopg.Connection,
    *,
    source: str,
    version: int,
    extractor_id: str | None = None,
    retire_header: bool = True,
    batch_size: int = RETRACT_BATCH_ROWS,
) -> Retraction:
    """02 §2.1.8 mechanism 2, as W1-b restates it — retraction DELETES the version's claims
    and re-resolves their listings.

    A contract version that misread the portal produced no evidence, so there is nothing to
    keep on disk and nothing for a view to subtract on every resolver read. The listings go
    into `dirty_locations` so the `*/15` drain mints their projections from what survives;
    the header is stood down so the next deploy activates a corrected version.

    NOT one transaction, deliberately. The delete is BATCHED, each batch atomic with its own
    enqueue: a version can hold millions of claims, and a single atomic DELETE of that size
    burns its statement_timeout and rolls back, leaving the operator exactly where they
    started with no way to make progress. Interrupted here, the batches that committed are
    real and re-running resumes — the predicate does not move.

    Raises if the target resolves to no contract entry: a typo'd portal, an unprojected
    version and an `--extractor-id` that names nothing all used to be indistinguishable from
    a version that simply had no claims.
    """
    with conn.cursor() as cur:
        cur.execute(_RETRACT_ENTRIES_SQL, {
            "source": source, "version": version, "extractor_id": extractor_id,
        })
        entry_ids = [int(row[0]) for row in cur.fetchall()]
    if not entry_ids:
        target = f"{source}@{version}" + (f" entry {extractor_id}" if extractor_id else "")
        raise ContractError(f"no such contract version: {target} matches no projected entry")

    deleted = enqueued = batches = 0
    while True:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(_RETRACT_TIMEOUT_SQL)
                cur.execute(_RETRACT_BATCH_SQL,
                            {"entry_ids": entry_ids, "batch_size": batch_size})
                batch_deleted, batch_enqueued = (int(x) for x in cur.fetchone())
        if batch_deleted == 0:
            break
        deleted += batch_deleted
        enqueued += batch_enqueued
        batches += 1
        LOG.info("CONTRACT retract %s@%s batch=%d deleted=%d enqueued=%d",
                 source, version, batches, batch_deleted, batch_enqueued)

    if retire_header and extractor_id is None:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(_RETIRE_SQL, {"source": source, "version": version})
    return Retraction(deleted=deleted, enqueued=enqueued, batches=batches)


# ------------------------------------------------------------------ CLI

def _parse_target(target: str) -> tuple[str, int]:
    if "@" not in target:
        raise ContractError(f"expected <portal>@<version>, got '{target}'")
    source, _, version = target.partition("@")
    if source not in EXTRACTOR_PREFIXES:
        raise ContractError(f"unknown portal '{source}'")
    return source, int(version)


def _summarise(contracts: Iterable[PortalContract]) -> str:
    # `sha256` is printed because it is no longer `sha256sum <file>` — it is taken over
    # the governed bytes only (`contract_body_hash`) — so this log line is where an
    # operator reconciling a `project()` refusal, or writing a restatement migration,
    # reads the value the code will actually compare.
    return json.dumps(
        {c.source: {"version": c.version, "entries": len(c.entries),
                    "claim_types": sorted({e.claim_type for e in c.entries}),
                    "volatile_surfaces": sorted(c.volatile_profiles),
                    "profile_digests": {
                        page_kind: payload_norm.profile_digest(profile)[
                            :payload_norm.PROFILE_DIGEST_CHARS]
                        for page_kind, profile in sorted(c.volatile_profiles.items())},
                    "sha256": c.sha256.hex()}
         for c in contracts},
        ensure_ascii=False, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=CONTRACT_DIR)
    parser.add_argument("--check", action="store_true",
                        help="Parse and validate every contract; touch no database.")
    parser.add_argument("--load", action="store_true",
                        help="Project the contracts into portal_contracts(+entries).")
    parser.add_argument("--git-ref", default=os.environ.get("GITHUB_SHA", "local"))
    parser.add_argument("--no-activate", action="store_true")
    parser.add_argument("--retract", metavar="PORTAL@VERSION",
                        help="Delete this version's claims and re-resolve their listings.")
    parser.add_argument("--extractor-id", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.retract:
        source, version = _parse_target(args.retract)
        with db.connect() as conn:
            missing = missing_relations(conn)
            if missing:
                print(f"ERROR: schema not applied; missing {', '.join(missing)}",
                      file=sys.stderr)
                return 2
            try:
                done = retract(conn, source=source, version=version,
                               extractor_id=args.extractor_id)
            except ContractError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
        LOG.info("CONTRACT retracted %s@%s entry=%s deleted=%d enqueued=%d batches=%d",
                 source, version, args.extractor_id or "*", done.deleted, done.enqueued,
                 done.batches)
        return 0

    contracts = load_all(args.dir)
    LOG.info("CONTRACT parsed %s", _summarise(contracts))
    if args.check or not args.load:
        return 0

    with db.connect() as conn:
        missing = missing_relations(conn)
        if missing:
            print(f"ERROR: schema not applied; missing {', '.join(missing)}", file=sys.stderr)
            return 2
        for contract in contracts:
            contract_id, inserted = project(
                conn, contract, git_ref=args.git_ref, activate=not args.no_activate)
            LOG.info("CONTRACT projected %s@%d id=%d new_entries=%d",
                     contract.source, contract.version, contract_id, inserted)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
