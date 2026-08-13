"""Portal extraction contracts as data — YAML in git, projected into the DB (D9).

Design: 02-portal-contracts.md §2.1 (the contract format), §2.1.8 (lifecycle: git is the
store of record, the DB pair is a deploy-time projection, retraction is an append),
§2.1.9 (licence_class), 00-shared-contracts.md §3 (surface / page_kind / extraction_method
are three separate axes, all three mandatory).

Two tables, one header + its immutable entries (migration 382):
  portal_contracts        (source, version, contract_sha256, git_ref, is_active, …)
  portal_contract_entries (contract_id, entry_id, surface, page_kind, locator, claim_type,
                           extraction_method, …)

`is_active` lives on the HEADER (the partial unique index is per source), and it is the
only mutable column: a change to any entry is a new `contract_version`, never an edit.
Projecting a contract whose bytes changed under an already-loaded version is refused —
that is the whole point of `contract_sha256`.

PyYAML is a DEV/CI dependency (pyproject `[dev]`, already present for
scripts/generate_workflow_docs.py) and is imported lazily: this module is a deploy-time
lane, exactly like the workflow-docs codegen. The claims extractor
(`location_data.claims_intake`) reads the DB projection and never parses YAML, so the
scraper/API runtime images stay untouched.

CLI:
    python -m location_data.contracts --check                     # validate only, no DB
    python -m location_data.contracts --load --git-ref <sha>      # project + activate
    python -m location_data.contracts --retract sreality@1 --reason contract_misread \\
        --by operator [--extractor-id sr.det.gps]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

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

# Migration 380's enums, verbatim. A literal that is not a member fails validation here
# rather than at INSERT time (01 §A.2 check 2).
CLAIM_SURFACES = frozenset({
    "api_json", "graphql", "embedded_json", "html_selector", "map_config", "og_meta",
    "jsonld", "url_slug", "description", "archived_html", "legacy_column", "registry",
    "operator_input",
})
PAGE_KINDS = frozenset({"index", "detail", "map", "gazetteer", "snapshot", "archive", "none"})
EXTRACTION_METHODS = frozenset({
    "portal_structured_field", "portal_declared_quality", "html_selector_parse",
    "url_slug_parse", "breadcrumb_parse", "jsonld_parse", "map_widget_parse", "regex_text",
    "llm_text", "legacy_column", "registry_derived", "operator_manual",
})
CLAIM_TYPES = frozenset({
    "coordinate", "uncertainty_geometry", "precision_declaration", "blur_hint", "map_zoom",
    "geohash", "admin_polygon",
    "address_point_id", "building_id", "obec_code", "portal_admin_id", "portal_street_id",
    "osm_relation_id", "cadastral_territory_name", "cadastral_territory_code",
    "parcel_number",
    "street_name", "house_number_cp", "house_number_co", "evidencni", "house_unit", "psc",
    "postal_town", "obec_name", "cast_obce_name", "quarter_name", "mestsky_obvod_name",
    "okres_name", "orp_name", "kraj_name", "country", "homonym_qualifier",
    "address_line_verbatim",
    "development_name", "landmark", "relative_distance", "poi_distance", "micro_position",
    "neighbour_listing_ref", "foreign_indicator",
})
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

CARDINALITIES = frozenset({"one", "many"})
REQUIRED_MODES = frozenset({"always", "when_present", "best_effort"})
RETRACTION_REASONS = frozenset({
    "extractor_bug", "contract_misread", "fabrication", "licence_withdrawal",
    "superseded_backfill", "operator_judgement",
})

# Surfaces reachable from `listings.raw_json` — the only substrate W1 mines (06 §6.2.1).
# A `reader` may only be declared on one of these; every other entry is declared for W2
# and carries no reader, so `claims_intake` cannot execute it by accident.
W1_SUBSTRATE_SURFACES = frozenset({"api_json", "graphql", "embedded_json", "legacy_column"})


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
    cardinality: str
    required: str
    on_conflict: str
    guards: list[str]
    notes: str | None

    @property
    def reader(self) -> str | None:
        value = self.locator.get("reader")
        return str(value) if value else None


@dataclass(frozen=True, slots=True)
class PortalContract:
    source: str
    version: int
    sha256: bytes
    identity_ladder: list[str]
    exclusion_zones: list[dict[str, Any]]
    precision_priors: dict[str, Any]
    fetch_config: dict[str, Any]
    entries: list[ContractEntry] = field(default_factory=list)
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


def parse_entry(raw: dict[str, Any], *, source: str, index: int) -> ContractEntry:
    where = f"{source} entry #{index}"
    entry_id = str(_require(raw, "id", where))
    prefix = EXTRACTOR_PREFIXES[source]
    if not entry_id.startswith(prefix):
        raise ContractError(
            f"{where}: extractor id '{entry_id}' must carry this portal's permanent "
            f"prefix '{prefix}' (02 §2.2 preamble)")
    where = f"{source}:{entry_id}"

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

    cardinality = _member(raw.get("cardinality", "one"), CARDINALITIES, where, "cardinality")
    required = _member(raw.get("required", "when_present"), REQUIRED_MODES, where, "required")

    # 01 §4.2's `loc_claim_legacy` CHECK forces `legacy_source_column` non-null whenever
    # extraction_method='legacy_column' — "an anonymous legacy claim is rejected by the
    # database rather than by convention" (06 §6.6 rule 3). Required here too, so the
    # rejection lands in CI instead of mid-batch.
    if method == "legacy_column" and not locator.get("legacy_source_column"):
        raise ContractError(
            f"{where}: a legacy_column entry must name its column in "
            f"locator.legacy_source_column (01 §4.2 loc_claim_legacy)")

    # 06 §6.1.3 classes some legacy columns per WRITER: `listings.street` is class B where
    # `street_source='parser'` and class D (quarantine, never a claim) otherwise. The split
    # is contract data — one equality against a provenance stamp — so the shape is
    # validated here rather than discovered as "this entry silently claims nothing".
    guard = locator.get("require_column_equals")
    if guard is not None:
        if method != "legacy_column":
            raise ContractError(
                f"{where}: locator.require_column_equals guards a legacy COLUMN read and "
                f"is only legal on extraction_method='legacy_column' (06 §6.1.3)")
        if not isinstance(guard, dict) or not guard:
            raise ContractError(
                f"{where}: locator.require_column_equals must be a non-empty "
                f"{{column: value}} mapping (06 §6.1.3)")
        for column, expected in guard.items():
            if not str(column).startswith("listings."):
                raise ContractError(
                    f"{where}: locator.require_column_equals names '{column}'; a guard "
                    f"column is spelled exactly like locator.legacy_source_column "
                    f"('listings.<column>'), because the extractor looks both up in the "
                    f"same per-row dict")
            if expected is None or isinstance(expected, (dict, list)):
                raise ContractError(
                    f"{where}: locator.require_column_equals['{column}'] must be a "
                    f"scalar — the guard is one equality against a provenance stamp, not "
                    f"a predicate language")

    reader = locator.get("reader")
    if reader and surface not in W1_SUBSTRATE_SURFACES:
        raise ContractError(
            f"{where}: locator.reader is only legal on a raw_json-reachable surface "
            f"({', '.join(sorted(W1_SUBSTRATE_SURFACES))}); '{surface}' is a W2 surface")

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
        if claim_type not in ("precision_declaration", "blur_hint"):
            raise ContractError(
                f"{where}: blurred_labels only belongs on a precision_declaration or "
                f"blur_hint entry (00 §2.2)")
        precision_map["blurred_labels"] = labels

    return ContractEntry(
        entry_id=entry_id,
        surface=surface,
        page_kind=page_kind,
        locator=locator,
        claim_type=claim_type,
        extraction_method=method,
        subject_scope=dict(raw.get("subject_scope") or {}),
        transform=[str(t) for t in (raw.get("transform") or [])],
        precision_map=precision_map,
        default_granularity=granularity,
        default_position_source=position_source,
        default_blur_evidence=blur,
        default_licence_class=licence,
        cardinality=cardinality,
        required=required,
        on_conflict=str(raw.get("on_conflict", "emit_both")),
        guards=[str(g) for g in (raw.get("guards") or [])],
        notes=raw.get("notes"),
    )


def parse_contract(path: Path) -> PortalContract:
    import yaml  # dev/CI-only dependency; see the module docstring.

    body = path.read_bytes()
    doc = yaml.safe_load(body.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ContractError(f"{path}: not a YAML mapping")
    source = str(_require(doc, "portal", str(path)))
    if source not in EXTRACTOR_PREFIXES:
        raise ContractError(f"{path}: unknown portal '{source}'")
    version = int(_require(doc, "contract_version", str(path)))
    if version < 1:
        raise ContractError(f"{path}: contract_version must be >= 1")

    entries = [
        parse_entry(raw, source=source, index=i)
        for i, raw in enumerate(_require(doc, "extractions", str(path)))
    ]
    seen: set[str] = set()
    for entry in entries:
        if entry.entry_id in seen:
            raise ContractError(f"{path}: duplicate extractor id '{entry.entry_id}'")
        seen.add(entry.entry_id)

    return PortalContract(
        source=source,
        version=version,
        # The file's `contract_sha256` field is documentation only — a file cannot carry
        # its own hash. The projection hashes the bytes on disk, which is what makes the
        # git artefact and the DB row provably identical (02 §2.1.8 mechanism 1).
        sha256=hashlib.sha256(body).digest(),
        identity_ladder=[str(x) for x in (doc.get("identity_ladder") or [])],
        exclusion_zones=list(doc.get("exclusion_zones") or []),
        precision_priors=dict(doc.get("precision_priors") or {}),
        fetch_config={
            "fetch": doc.get("fetch") or {},
            "persistence": doc.get("persistence") or {},
            "precision_caps": doc.get("precision_caps") or {},
            "regressions": doc.get("regressions") or [],
            "extractor_runtime": doc.get("extractor_runtime"),
        },
        entries=entries,
        path=path,
    )


def load_all(directory: Path = CONTRACT_DIR) -> list[PortalContract]:
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise ContractError(f"no contract files under {directory}")
    return [parse_contract(p) for p in paths]


def extractor_version(contract: PortalContract | str, version: int | None = None) -> str:
    """02 §2.1.8: every claim carries `contract:<portal>@<version>`."""
    if isinstance(contract, PortalContract):
        return f"contract:{contract.source}@{contract.version}"
    return f"contract:{contract}@{version}"


# ------------------------------------------------------------------ projection

_HEADER_SELECT_SQL = """
    SELECT id, encode(contract_sha256, 'hex'), is_active
    FROM portal_contracts
    WHERE source = %(source)s AND version = %(version)s
"""

_HEADER_INSERT_SQL = """
    INSERT INTO portal_contracts
        (source, version, contract_sha256, git_ref, identity_ladder, exclusion_zones,
         precision_priors, fetch_config, is_active)
    VALUES (%(source)s, %(version)s, decode(%(sha256)s, 'hex'), %(git_ref)s,
            %(identity_ladder)s, %(exclusion_zones)s, %(precision_priors)s,
            %(fetch_config)s, false)
    RETURNING id
"""

_ENTRY_INSERT_SQL = """
    INSERT INTO portal_contract_entries
        (contract_id, entry_id, surface, page_kind, locator, claim_type, extraction_method,
         subject_scope, transform, precision_map, default_granularity,
         default_position_source, default_blur_evidence, default_licence_class,
         cardinality, required, on_conflict, guards, notes)
    VALUES (%(contract_id)s, %(entry_id)s, %(surface)s, %(page_kind)s, %(locator)s,
            %(claim_type)s, %(extraction_method)s, %(subject_scope)s, %(transform)s,
            %(precision_map)s, %(default_granularity)s, %(default_position_source)s,
            %(default_blur_evidence)s, %(default_licence_class)s, %(cardinality)s,
            %(required)s, %(on_conflict)s, %(guards)s, %(notes)s)
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

_RETRACT_SQL = """
    INSERT INTO location_claim_retractions
        (scope, contract_source, contract_version, extractor_id, reason, note, retracted_by)
    VALUES (%(scope)s, %(source)s, %(version)s, %(extractor_id)s, %(reason)s, %(note)s,
            %(retracted_by)s)
    RETURNING id
"""

_RETIRE_SQL = """
    UPDATE portal_contracts SET is_active = false, retired_at = now()
    WHERE source = %(source)s AND version = %(version)s
"""

_RELATIONS = ("portal_contracts", "portal_contract_entries", "location_claim_retractions")
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

    Re-running with the same bytes is a no-op; re-running with DIFFERENT bytes under the
    same version raises — entries are immutable and a change is a new version (02 §2.1.8).
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
                    "identity_ladder": contract.identity_ladder,
                    "exclusion_zones": psycopg.types.json.Jsonb(contract.exclusion_zones),
                    "precision_priors": psycopg.types.json.Jsonb(contract.precision_priors),
                    "fetch_config": psycopg.types.json.Jsonb(contract.fetch_config),
                })
                contract_id = int(cur.fetchone()[0])
            else:
                contract_id, stored_sha, _is_active = int(row[0]), row[1], row[2]
                if stored_sha != sha_hex:
                    raise ContractError(
                        f"{contract.source}@{contract.version} is already loaded with a "
                        f"different sha256 ({stored_sha} on record, {sha_hex} on disk). "
                        f"Contract entries are immutable: bump contract_version "
                        f"(02 §2.1.8).")

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
                    "cardinality": entry.cardinality,
                    "required": entry.required,
                    "on_conflict": entry.on_conflict,
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


def retract(
    conn: psycopg.Connection,
    *,
    source: str,
    version: int,
    reason: str,
    retracted_by: str,
    extractor_id: str | None = None,
    note: str | None = None,
    retire_header: bool = True,
) -> int:
    """02 §2.1.8 mechanism 2 — retraction is an append, never a delete.

    Claims stay on disk and stop being resolver inputs (they drop out of
    `location_claims_live`); the header is stood down so the next deploy activates a
    corrected version.
    """
    if reason not in RETRACTION_REASONS:
        raise ContractError(f"unknown retraction reason '{reason}'")
    scope = "extractor_entry" if extractor_id else "contract_version"
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(_RETRACT_SQL, {
                "scope": scope, "source": source, "version": version,
                "extractor_id": extractor_id, "reason": reason, "note": note,
                "retracted_by": retracted_by,
            })
            retraction_id = int(cur.fetchone()[0])
            if retire_header and extractor_id is None:
                cur.execute(_RETIRE_SQL, {"source": source, "version": version})
    return retraction_id


# ------------------------------------------------------------------ CLI

def _parse_target(target: str) -> tuple[str, int]:
    if "@" not in target:
        raise ContractError(f"expected <portal>@<version>, got '{target}'")
    source, _, version = target.partition("@")
    if source not in EXTRACTOR_PREFIXES:
        raise ContractError(f"unknown portal '{source}'")
    return source, int(version)


def _summarise(contracts: Iterable[PortalContract]) -> str:
    return json.dumps(
        {c.source: {"version": c.version, "entries": len(c.entries),
                    "w1_readers": sum(1 for e in c.entries if e.reader)}
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
    parser.add_argument("--retract", metavar="PORTAL@VERSION")
    parser.add_argument("--extractor-id", default=None)
    parser.add_argument("--reason", default="operator_judgement")
    parser.add_argument("--note", default=None)
    parser.add_argument("--by", default=os.environ.get("GITHUB_ACTOR", "operator"))
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
            retraction_id = retract(
                conn, source=source, version=version, reason=args.reason,
                retracted_by=args.by, extractor_id=args.extractor_id, note=args.note)
        LOG.info("CONTRACT retracted %s@%s entry=%s id=%d",
                 source, version, args.extractor_id or "*", retraction_id)
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
