"""The contract layer — format validation and the deploy-time projection (02 §2.1).

The nine YAML files in `contracts/portals/` are the store of record; `portal_contracts` +
`portal_contract_entries` are their projection. These tests are the CI half of §2.1.8's
lifecycle: a contract that would write nonsense into an append-only table must fail here,
not at INSERT time, and never at resolution time.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from location_data import claims_intake, claims_remine_archive, contracts
from location_data.claims_intake import GUARDS, LEGACY_COLUMNS, READERS, SOURCES, TRANSFORMS
from location_data.claims_remine_archive import ARCHIVE_READERS
from location_data.contracts import (
    CLAIM_TYPES,
    EXTRACTION_METHODS,
    EXTRACTOR_PREFIXES,
    GRANDFATHERED_INERT_GUARDS,
    IMPLEMENTED_GUARDS,
    IMPLEMENTED_TRANSFORMS,
    READER_CONTRACTS,
    READER_SUBSTRATES,
    ContractError,
    parse_entry,
)

ALL = {c.source: c for c in contracts.load_all()}

MINIMAL = {
    "id": "sr.det.thing",
    "locator_kind": "api_json",
    "extraction_method": "portal_structured_field",
    "page_kind": "detail",
    "locator": {"json_pointer": "/locality/street"},
    "claim_type": "street_name",
}
COORDINATE = {
    "claim_type": "coordinate",
    "precision_cap": {"granularity_max": {"_default": "address_point"}},
}
POINT_PAIR = {"reader": "point_pair", "lat_pointer": "/lat", "lon_pointer": "/lon"}


def _entry(**overrides):
    raw = dict(MINIMAL)
    raw.update(overrides)
    return parse_entry(raw, source="sreality", index=0)


# ------------------------------------------------------------ the reader bodies, as data

_INTAKE_AST = ast.parse(Path(claims_intake.__file__).read_text(encoding="utf-8"))
# The ARCHIVE lane's readers live in their own module behind `@archive_reader`, so the
# `_INTAKE_AST` scan below cannot see them. W2-6 registered three DOM readers in
# READER_CONTRACTS that no body-vs-contract check introspected at all — an adversarial
# review caught `html_point_dms` declaring `consults_guards=True` while never calling
# `guard_admits`, i.e. exactly the misdeclaration this file's gate exists to make
# impossible, surviving because the gate could not see the reader.
_ARCHIVE_AST = ast.parse(
    Path(claims_remine_archive.__file__).read_text(encoding="utf-8"))


def _reader_bodies(
    tree: ast.Module = _INTAKE_AST, decorator: str = "reader",
) -> dict[str, ast.FunctionDef]:
    """Every `@<decorator>("name")`-decorated function in `tree`, keyed by its name."""
    bodies: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name)
                    and dec.func.id == decorator and dec.args
                    and isinstance(dec.args[0], ast.Constant)):
                bodies[str(dec.args[0].value)] = node
    return bodies


def _called_names(fn: ast.FunctionDef) -> set[str]:
    return {node.func.id for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}


def _indexed_locator_keys(fn: ast.FunctionDef) -> set[str]:
    """`entry.locator["key"]` — the UNGUARDED reads, i.e. the ones that raise KeyError.
    A `.get()` is a different thing and is deliberately not collected."""
    return {str(node.slice.value) for node in ast.walk(fn)
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
            and isinstance(node.value, ast.Attribute) and node.value.attr == "locator"}


def _constant_legacy_stamps(fn: ast.FunctionDef) -> set[str]:
    """`legacy_source_column="…"` passed as a literal, i.e. a provenance the READER fixes
    rather than one the entry supplies."""
    return {str(kw.value.value) for node in ast.walk(fn) if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "legacy_source_column" and isinstance(kw.value, ast.Constant)}


# ------------------------------------------------------------------ the nine contracts

def test_every_portal_has_exactly_one_contract_file():
    assert set(ALL) == set(SOURCES)
    assert len(list((Path(contracts.CONTRACT_DIR)).glob("*.yaml"))) == 9


def test_extractor_id_prefixes_are_portal_unique_and_permanent():
    """Revision 1 gave bezrealitky AND bazos the prefix `bz.`, which would have merged the
    provenance of a GraphQL portal and an HTML portal on ids that may never be renamed."""
    assert EXTRACTOR_PREFIXES["bezrealitky"] == "bzr."
    assert EXTRACTOR_PREFIXES["bazos"] == "bzs."
    assert len(set(EXTRACTOR_PREFIXES.values())) == 9
    for source, contract in ALL.items():
        for entry in contract.entries:
            assert entry.entry_id.startswith(EXTRACTOR_PREFIXES[source])


def test_every_entry_states_both_axes_and_a_canonical_claim_type():
    """00 §3.2: `locator_kind` IS the surface and `extraction_method` is a separate,
    mandatory field — an html_selector locator can be html_selector_parse, breadcrumb_parse
    or map_widget_parse."""
    for contract in ALL.values():
        for entry in contract.entries:
            assert entry.claim_type in CLAIM_TYPES
            assert entry.extraction_method in EXTRACTION_METHODS
            assert entry.surface != "portal_json"


def test_every_reader_named_in_a_contract_exists_in_the_registry():
    for contract in ALL.values():
        for entry in contract.entries:
            if entry.reader:
                # Either registry: W1 payload readers or the archived lane's DOM readers.
                # `_check_executable` already refuses a name in neither.
                assert entry.reader in set(READERS) | set(ARCHIVE_READERS), entry.entry_id


def test_every_executable_entry_matches_its_readers_contract():
    """Legality is per READER on every axis, not fleet-wide: `geom_column` reads
    `listings.geom` and `legacy_text_column` reads a class-B `listings` column, so neither
    can sit on a payload surface or claim a portal extraction method even though the old
    W1 gate allowed both; and every locator key its reader indexes must be there, because
    the extractor indexes them unguarded."""
    for contract in ALL.values():
        for entry in contract.entries:
            if not entry.reader:
                continue
            spec = READER_CONTRACTS[entry.reader]
            assert entry.surface in spec.substrates, entry.entry_id
            assert entry.extraction_method in spec.methods, entry.entry_id
            assert spec.locator_keys <= set(entry.locator), entry.entry_id
            if spec.stamps_legacy_column is not None:
                declared = entry.locator.get("legacy_source_column")
                assert declared in (None, spec.stamps_legacy_column), entry.entry_id
            if entry.transform:
                assert spec.consults_transforms, entry.entry_id
            inert = GRANDFATHERED_INERT_GUARDS.get(entry.entry_id, frozenset())
            if set(entry.guards) - inert:
                assert spec.consults_guards, entry.entry_id


def test_reader_substrates_stay_in_sync_with_the_runtime_registry():
    """`contracts.READER_CONTRACTS` is pure data — the deploy-time lane must not import
    the extractor — so a reader added to `claims_intake` without a record (or one left
    behind after a reader is deleted) is caught HERE, by the one test that imports both.
    Otherwise the projection would reject every entry naming the new reader."""
    # The name-only mirror W1 uses to SKIP a DOM entry must equal the real archive registry.
    # A reader added to `ARCHIVE_READERS` and not to `ARCHIVE_ONLY_READERS` stops being
    # skipped by the hourly W1 intake and starts being REFUSED by it — taking that portal's
    # intake down the moment a contract naming it loads. Pinned, not left to review.
    assert claims_intake.ARCHIVE_ONLY_READERS == set(ARCHIVE_READERS)
    # TWO runtime registries since W2-6, deliberately separate objects (a name in one must
    # not silently resolve in the other) with ONE deploy-time record covering both.
    assert set(READER_CONTRACTS) == set(READERS) | set(ARCHIVE_READERS)
    assert not set(READERS) & set(ARCHIVE_READERS)
    assert READER_SUBSTRATES == {n: s.substrates for n, s in READER_CONTRACTS.items()}
    surfaces = {s for legal in READER_SUBSTRATES.values() for s in legal}
    assert surfaces <= contracts.CLAIM_SURFACES
    # W2-6 opened the DOM surfaces; W2's reader canon opened the three that carry a fact the
    # DOM readers cannot reach — a JSON document embedded in the page, a fact published only
    # inside a link, and a schema.org JSON-LD block. Written as "no reader may be declared on
    # a W2 surface until W2 gives it one" and updated here deliberately — still an exact set,
    # so a further surface cannot arrive unreviewed.
    assert surfaces == {
        "api_json", "graphql", "embedded_json", "legacy_column",
        "html_selector", "archived_html", "map_config", "url_slug", "jsonld",
    }
    methods = {m for spec in READER_CONTRACTS.values() for m in spec.methods}
    assert methods <= EXTRACTION_METHODS
    # `regex_text` arrives with the canon and is the one that changes what a claim MUST
    # carry: 01 §4.2 makes it evidence-bearing, so every claim from a reader declaring it
    # needs the quote-plus-span set. It is admissible only because W2a filled the
    # content-addressed body store the span indexes into — W1's `listings.raw_json` is not
    # retrievable, which is why `test_w1_executes_no_evidence_bearing_method` still holds.
    assert methods == {
        "portal_structured_field", "portal_declared_quality", "legacy_column",
        "html_selector_parse", "map_widget_parse", "url_slug_parse", "regex_text",
        "breadcrumb_parse",
    }


def test_the_reader_contracts_state_exactly_what_the_reader_bodies_do():
    """The half of the sync that a set-equality cannot reach: knowing a reader EXISTS says
    nothing about whether it consults what an entry declares. Only three readers call
    `apply_transforms` and three call `guard_admits`, so a `transform` on `point_pair` or a
    `guard` on `scalar` is inert — validating the NAME while the entry's own reader never
    asks for it is exactly the silent no-op this gate exists to stop. So every consulted
    axis is read back out of the reader bodies rather than asserted by hand."""
    bodies = _reader_bodies()
    assert set(bodies) == set(READERS)
    for name, fn in bodies.items():
        spec = READER_CONTRACTS[name]
        calls = _called_names(fn)
        assert spec.consults_transforms == ("apply_transforms" in calls), name
        assert spec.consults_guards == ("guard_admits" in calls), name
        assert spec.locator_keys == _indexed_locator_keys(fn), name
        stamped = _constant_legacy_stamps(fn)
        assert stamped == ({spec.stamps_legacy_column} if spec.stamps_legacy_column
                           else set()), name
    # The scan reads each reader's OWN body, so a helper that applied transforms or
    # evaluated guards on a reader's behalf would let the table lie about it. There is no
    # such helper: the two entry points are called from reader bodies and nowhere else.
    reader_names = {fn.name for fn in bodies.values()} | {"apply_transforms", "guard_admits"}
    for node in _INTAKE_AST.body:
        if isinstance(node, ast.FunctionDef) and node.name not in reader_names:
            assert not ({"apply_transforms", "guard_admits"} & _called_names(node)), node.name


def test_the_archive_reader_contracts_state_exactly_what_those_bodies_do():
    """The same body-vs-contract gate, extended to the ARCHIVE lane's DOM readers.

    W2-6 put three readers into `READER_CONTRACTS` that the scan above cannot reach: they
    are decorated `@archive_reader` and live in `claims_remine_archive`, while
    `_reader_bodies()` reads `@reader` out of `claims_intake`. The consequence was not
    hypothetical — `html_point_dms` shipped declaring `consults_guards=True` while never
    calling `guard_admits`, which would have let any entry naming it declare a guard the
    runtime silently ignored. Review caught it; this makes review unnecessary.

    Kept as a SEPARATE test rather than folded into the one above because the two registries
    are deliberately separate objects and a single test asserting over both would go green
    if one of them vanished.

    NARROWER than the W1 gate, deliberately and disclosed rather than implied: it checks the
    two `consults_*` flags but NOT `locator_keys`, because the DOM readers address their
    locator through `.get()` plus an explicit refusal (`_entry_css`, the attr-pair check)
    rather than the unguarded `entry.locator["key"]` indexing `_indexed_locator_keys` looks
    for. Asserting equality there would compare a set against an empty one. Closing it
    properly needs the scan to recognise the refusal helpers; until then this is a known
    half, not an assumed whole."""
    bodies = _reader_bodies(_ARCHIVE_AST, "archive_reader")
    assert set(bodies) == set(ARCHIVE_READERS)
    for name, fn in bodies.items():
        spec = READER_CONTRACTS[name]
        calls = _called_names(fn)
        assert spec.consults_transforms == ("apply_transforms" in calls), name
        assert spec.consults_guards == ("guard_admits" in calls), name
    # The same no-helper rule as the W1 scan, for the same reason: a helper evaluating
    # guards on a reader's behalf would let the table lie. `_evidenced` and `_entry_css` are
    # shared, and neither may touch the two entry points.
    reader_names = {fn.name for fn in bodies.values()} | {"apply_transforms", "guard_admits"}
    for node in _ARCHIVE_AST.body:
        if isinstance(node, ast.FunctionDef) and node.name not in reader_names:
            assert not ({"apply_transforms", "guard_admits"} & _called_names(node)), node.name


def test_the_transform_and_guard_vocabularies_stay_in_sync_with_the_runtime():
    """Same pure-data contract as the reader table, for the two smaller vocabularies. A
    transform implemented but not listed would be refused on every entry that names it;
    one listed but not implemented would be a silent no-op — the thing the check exists
    to stop."""
    assert IMPLEMENTED_TRANSFORMS == frozenset(TRANSFORMS)
    assert IMPLEMENTED_GUARDS == frozenset(GUARDS)


def test_the_executable_and_inert_split_is_exactly_what_w1_ran():
    """Per-reader substrates replaced a fleet-wide gate, and a refactor of a validator is
    only safe if the set of entries the extractor RUNS does not move. 69 executable / 70
    declared-ahead, per portal, as of the W1 gate outcomes."""
    split = {source: (sum(1 for e in c.entries if e.reader),
                      sum(1 for e in c.entries if not e.reader))
             for source, c in ALL.items()}
    assert split == {
        "bazos": (4, 8),
        "bezrealitky": (11, 6),
        "ceskereality": (5, 11),
        "idnes": (3, 11),
        "maxima": (3, 7),
        "mmreality": (11, 4),
        "realitymix": (5, 8),
        "remax": (4, 9),
        "sreality": (23, 6),
    }
    assert sum(e for e, _ in split.values()) == 69
    assert sum(i for _, i in split.values()) == 70
    # The same 69 entries seen down the other axis, so a swap could not preserve both.
    per_reader = Counter(e.reader for c in ALL.values() for e in c.entries if e.reader)
    assert per_reader == Counter({
        "scalar": 36, "namespaced_id": 9, "geom_column": 6, "coords_stamp_quality": 5,
        "legacy_text_column": 5, "point_pair": 3, "declared_quality": 2,
        "bbox_envelope": 1, "conflict_signal": 1, "declared_bool_quality": 1,
    })


def test_w1_executes_no_evidence_bearing_method():
    """`regex_text` / `llm_text` need a span into a retrievable document. W2a filled the
    content-addressed body store (01 §4.2), so an evidence-bearing entry may now name a
    reader — but ONLY one of `claims_remine_archive`'s, which W1 skips
    (`ARCHIVE_ONLY_READERS`) because `listings.raw_json` is not content-addressed and a span
    into it cannot be re-checked. `llm_text` still names none: no LLM reader is registered in
    any registry, and `assert_evidence_complete` refuses a model-less llm claim anyway.

    Narrowed, never deleted: without it a future `regex_text` entry could land on the W1
    lane, where the span it asserts is unverifiable by construction."""
    for contract in ALL.values():
        for entry in contract.entries:
            if entry.extraction_method == "llm_text":
                assert entry.reader is None, entry.entry_id
            elif entry.extraction_method == "regex_text" and entry.reader is not None:
                assert entry.reader in claims_intake.ARCHIVE_ONLY_READERS, entry.entry_id


def test_the_w2_surfaces_are_still_declared():
    """02 §2.2 declares the full contract, not just what W1 can run — "a signal that exists
    on the wire and has no contract entry is a diff, not an archaeology project"."""
    declared = {e.surface for c in ALL.values() for e in c.entries}
    assert {"html_selector", "map_config", "url_slug", "og_meta", "jsonld",
            "description"} <= declared
    # The named misses of 02 §2.2 that production still drops.
    ids = {e.entry_id for c in ALL.values() for e in c.entries}
    for missing_today in ("sr.idx.geohash", "bzr.det.ruian_id", "mm.det.municipality_id",
                          "rx.idx.display_address", "cr.map.exact",
                          "rm.det.breadcrumb_geo", "mx.det.map_features",
                          "bzs.det.obec_slug", "id.det.subject_feature"):
        assert missing_today in ids


def test_every_legacy_column_entry_names_its_column():
    """01 §4.2's `loc_claim_legacy` CHECK — "an anonymous legacy claim is rejected by the
    database rather than by convention" (06 §6.6 rule 3). Caught in CI, not mid-batch."""
    legacy = [e for c in ALL.values() for e in c.entries
              if e.extraction_method == "legacy_column"]
    assert len(legacy) >= 15
    for entry in legacy:
        assert entry.locator.get("legacy_source_column"), entry.entry_id
    with pytest.raises(ContractError, match="loc_claim_legacy"):
        _entry(locator_kind="legacy_column", extraction_method="legacy_column",
               page_kind="none", locator={"reader": "scalar", "json_pointer": "/x"})


def test_a_legacy_entry_never_burns_a_permanent_html_extractor_id():
    """02 §2.2.3 fixes an id per portal SURFACE, permanently — `bzs.det.psc` is the
    Lokalita-cell HTML parse and `bzs.det.link_pin` is the maps-anchor HTML parse, both
    W2 work. Spending those ids on the W1 legacy mirrors of the same facts would mean
    either that W2 cannot ship its own entry or that one extractor_id names two different
    acts of extraction with two different provenances — and `location_claims.extractor_id`
    is how a claim's origin is read back forever. The legacy mirrors take `legacy_`-marked
    ids of their own (the pattern idnes set with `id.det.legacy_pin`)."""
    legacy_ids = {e.entry_id for c in ALL.values() for e in c.entries
                  if e.extraction_method == "legacy_column"}
    for reserved in ("bzs.det.psc", "bzs.det.link_pin", "id.det.pin", "sr.det.pin",
                     # 02 §2.2.6 fixes both of these on remax HTML surfaces: the detail
                     # header parse and the index card's data-display-address. The W1
                     # raw_json mirror of the same string is a different act.
                     "rx.det.header_address", "rx.idx.display_address"):
        assert reserved not in legacy_ids, (
            f"{reserved} is 02 §2.2.3's permanent id for an HTML surface; the legacy "
            f"entry must mint its own")
    assert {"bzs.det.legacy_psc", "bzs.det.legacy_link_pin", "id.det.legacy_pin",
            "rx.det.legacy_display_address"} <= legacy_ids
    # The W2 entries whose ids the legacy mirrors deliberately did not spend are still
    # declared — a mirror that quietly replaced its HTML counterpart would be a regression.
    html_ids = {e.entry_id for c in ALL.values() for e in c.entries
                if e.surface == "html_selector"}
    assert {"rx.det.header_address", "rx.idx.display_address"} <= html_ids


def test_the_class_b_legacy_columns_are_capped_and_flagged():
    """06 §6.1.1: a class-B column becomes a claim with `extraction_method='legacy_column'`,
    `licence_class='portal'`, blur written explicitly and confidence capped at `medium`.
    The cap is CONTRACT data — `legacy_text_column` stamps whatever the entry declares —
    so a future entry that forgets it would silently mint a full-confidence claim out of a
    column with no provenance at all."""
    entries = [(c.source, e) for c in ALL.values() for e in c.entries
               if e.reader == "legacy_text_column"]
    assert {source for source, _ in entries} == {"remax", "ceskereality", "realitymix"}
    for source, entry in entries:
        assert entry.locator["legacy_source_column"].startswith("listings."), entry.entry_id
        assert entry.extraction_method == "legacy_column", entry.entry_id
        assert entry.surface == "legacy_column", entry.entry_id
        assert entry.page_kind == "none", entry.entry_id
        assert entry.default_licence_class == "portal", entry.entry_id
        assert entry.default_blur_evidence == "none", entry.entry_id
        assert entry.locator["claim_confidence"] == "medium", entry.entry_id
        assert entry.precision_map["prior"]["match_confidence"] == "medium", entry.entry_id
        # §6.6 rule 3 is about whether the WRITER can be named, and a provenance guard is
        # how it gets named: an unguarded legacy column cannot say who wrote it, a guarded
        # one admits only the writer it names. So the two flags are each other's inverse,
        # and an entry that guards AND claims the write path is unknown is incoherent.
        assert entry.locator["write_path_unknown"] is (
            entry.locator.get("require_column_equals") is None), entry.entry_id


def test_the_street_entries_are_guarded_onto_the_class_b_provenance_only():
    """06 §6.1.3 classes `listings.street` per WRITER, not per column: `parser` is class B
    (portal-derived text), while `resolver` (a RÚIAN address-point inference) and NULL (the
    unattributable legacy writes) are class D — quarantine, never a claim. The split is the
    entry's own predicate, so a portal that needs a different one is a version bump and
    never a branch in the extractor."""
    guarded = {e.entry_id: e for c in ALL.values() for e in c.entries
               if e.locator.get("require_column_equals")}
    assert set(guarded) == {"cr.det.legacy_street", "rm.det.legacy_street"}
    for entry_id, entry in guarded.items():
        assert entry.locator["legacy_source_column"] == "listings.street", entry_id
        assert entry.locator["require_column_equals"] == {
            "listings.street_source": "parser"}, entry_id
        assert entry.claim_type == "street_name", entry_id
        assert entry.extraction_method == "legacy_column", entry_id
        assert entry.default_licence_class == "portal", entry_id


def test_every_legacy_column_a_contract_names_is_one_the_intake_scan_selects():
    """The columns are read positionally off the batch queries, and a name the scan does
    not select is refused at extraction time — so a contract naming one would take a whole
    run down. Both spellings count: the column a legacy entry READS and the column its
    guard TESTS."""
    named = {
        str(e.locator["legacy_source_column"])
        for c in ALL.values() for e in c.entries if e.reader == "legacy_text_column"
    } | {
        str(column)
        for c in ALL.values() for e in c.entries
        for column in (e.locator.get("require_column_equals") or {})
    }
    assert named <= set(LEGACY_COLUMNS), sorted(named - set(LEGACY_COLUMNS))
    assert "listings.street_source" in LEGACY_COLUMNS


def test_the_bumped_contracts_appended_entries_and_kept_the_earlier_ones():
    """02 §2.1.8: entries are immutable per `contract_version`, so closing a measured
    coverage gap is a VERSION BUMP that appends. Every earlier id must still be there — an
    entry that disappeared would orphan every claim already stamped with it."""
    # Untouched by W2a-3e, which is the point of that change: moving every portal's
    # `persistence.volatile_paths` into these files bumped nothing, because
    # `contract_sha256` no longer covers `persistence` (mig 408). A version bump here
    # re-stamps extractor_version and contract_entry_id on every claim the next
    # incremental scan re-walks, and archive configuration must not be able to spend
    # that. What versions these ARE is the record of extraction changes only.
    #
    # ceskereality@4 is the ONE bump in this census that appended nothing, and it is not
    # an exception to the rule above — it is the rest of it. The governed hash is the file
    # minus `persistence:` and `shadow:`, so PROSE is hashed exactly like a selector: PR
    # #1209 rewrote `fetch.robots_note` and one entry's `notes:` without a bump, v3's hash
    # moved under a row already on record, and `project()` refused the fleet's whole
    # projection for 14 consecutive hourly runs. The prose was the accurate one, entries
    # are immutable, so the bump is the remedy doctrine names. Its claim set is
    # byte-identical to v3's (`golden/ceskereality@{3,4}.json` differ in one field), and
    # `test_contract_immutability` is what now catches the unbumped edit before merge.
    assert {s: c.version for s, c in ALL.items()} == {
        "remax": 2, "ceskereality": 4, "realitymix": 3,
        "sreality": 1, "bezrealitky": 1, "bazos": 1, "idnes": 1, "mmreality": 1,
        "maxima": 1,
    }
    for source, new_ids, earlier_ids in (
        ("remax", {"rx.det.legacy_display_address", "rx.det.legacy_locality"},
         {"rx.det.raw_address_conflict", "rx.det.legacy_pin"}),
        ("ceskereality", {"cr.det.legacy_street"},
         {"cr.det.locality_text", "cr.det.legacy_pin", "cr.det.coords_stamp",
          "cr.det.legacy_locality"}),
        ("realitymix", {"rm.det.legacy_street"},
         {"rm.det.locality_text", "rm.det.legacy_pin", "rm.det.coords_block",
          "rm.det.legacy_locality"}),
    ):
        ids = {e.entry_id for e in ALL[source].entries}
        assert new_ids <= ids, source
        assert earlier_ids <= ids, source


def test_coordinate_entries_carry_a_cap_and_a_licence_class():
    for contract in ALL.values():
        for entry in contract.entries:
            if entry.claim_type == "coordinate":
                assert entry.precision_map.get("precision_cap"), entry.entry_id
                assert entry.default_licence_class, entry.entry_id


def test_blurred_label_sets_ride_on_the_contract_not_in_code():
    blurred = {
        e.entry_id: e.precision_map["blurred_labels"]
        for c in ALL.values() for e in c.entries if e.precision_map.get("blurred_labels")
    }
    assert blurred["sr.det.inaccuracy_type"] == ["street", "ward", "quarter", "municipality"]
    assert blurred["mm.det.accurate"] == ["not_accurate"]
    assert blurred["cr.map.exact"] == ["exact_false"]


def test_exclusion_zones_name_every_portals_decoy():
    """Every portal ships at least one fully-formed address-shaped decoy (02 §2.5)."""
    for source, contract in ALL.items():
        assert contract.exclusion_zones, source
    sreality_zones = str(ALL["sreality"].exclusion_zones)
    assert "/premise" in sreality_zones
    assert "area-listings__item" in str(ALL["remax"].exclusion_zones)


def test_contract_sha256_is_taken_from_the_governed_bytes_on_disk():
    """The bytes on disk minus the two blocks that are not extraction (mig 404, 408) —
    so the hash covers exactly what a bump of `contract_version` would re-stamp."""
    contract = ALL["maxima"]
    assert contract.path is not None
    import hashlib
    body = contract.path.read_bytes()
    assert contract.sha256 == contracts.contract_body_hash(body)
    assert contract.sha256 != hashlib.sha256(body).digest(), (
        "maxima declares persistence.volatile_paths, so the governed hash must differ "
        "from a whole-file hash — otherwise this test proves nothing")
    assert contracts.extractor_version(contract) == "contract:maxima@1"


# ------------------------------------------------------------------ format validation

def test_an_entry_may_not_assign_an_axis():
    with pytest.raises(ContractError, match="never ASSIGNS"):
        _entry(granularity="address_point")


def test_a_coordinate_entry_without_a_cap_is_rejected():
    with pytest.raises(ContractError, match="precision_cap"):
        _entry(claim_type="coordinate", locator={"reader": "point_pair"})


def test_a_non_enum_literal_is_rejected():
    with pytest.raises(ContractError, match="claim_type"):
        _entry(claim_type="street")          # the retired pre-review spelling
    with pytest.raises(ContractError, match="locator_kind"):
        _entry(locator_kind="portal_json")   # forbidden literal (01 §A.2 check 4)


def test_the_forbidden_licence_class_and_detected_blur_are_rejected():
    with pytest.raises(ContractError, match="licence_class"):
        _entry(licence_class="ephemeral_display_only")
    with pytest.raises(ContractError, match="collision detector"):
        _entry(blur_evidence="detected")


def test_a_non_enum_confidence_is_rejected_on_both_of_its_spellings():
    """`claim_confidence` lands in a typed `match_confidence` column, so a typo caught here
    is a CI failure instead of a mid-batch INSERT error that takes a whole run down."""
    with pytest.raises(ContractError, match="prior.match_confidence"):
        _entry(prior={"match_confidence": "certain"})
    with pytest.raises(ContractError, match="locator.claim_confidence"):
        _entry(locator={"reader": "scalar", "json_pointer": "/x",
                        "claim_confidence": "very-high"})


def test_a_malformed_provenance_guard_is_rejected():
    """The guard decides whether a class-D value becomes a claim, so every way of writing
    it wrong fails in CI: on a non-legacy method (where nothing would ever read it), with a
    column spelled differently from `legacy_source_column` (the extractor looks both up in
    one dict, so an unqualified name would just never match), and with a non-scalar
    right-hand side (one equality against a provenance stamp, not a predicate language)."""
    legacy = {
        "locator_kind": "legacy_column",
        "extraction_method": "legacy_column",
        "page_kind": "none",
    }
    with pytest.raises(ContractError, match="require_column_equals"):
        _entry(locator={"reader": "scalar", "json_pointer": "/x",
                        "require_column_equals": {"listings.street_source": "parser"}})
    with pytest.raises(ContractError, match="non-empty"):
        _entry(**legacy, locator={"reader": "legacy_text_column",
                                  "legacy_source_column": "listings.street",
                                  "require_column_equals": {}})
    with pytest.raises(ContractError, match="listings.<column>"):
        _entry(**legacy, locator={"reader": "legacy_text_column",
                                  "legacy_source_column": "listings.street",
                                  "require_column_equals": {"street_source": "parser"}})
    with pytest.raises(ContractError, match="scalar"):
        _entry(**legacy, locator={"reader": "legacy_text_column",
                                  "legacy_source_column": "listings.street",
                                  "require_column_equals": {
                                      "listings.street_source": ["parser", "resolver"]}})


def test_a_wrong_prefix_is_rejected():
    with pytest.raises(ContractError, match="permanent"):
        parse_entry(dict(MINIMAL, id="bz.det.thing"), source="sreality", index=0)


def test_a_reader_outside_its_registered_substrates_is_rejected():
    """The reader IS the substrate declaration. An HTML surface has no reader at all yet;
    a payload reader on a legacy column (or the reverse) reads the wrong thing while
    stamping the claim's provenance as the other one."""
    with pytest.raises(ContractError, match="not one of its substrates"):
        _entry(locator_kind="html_selector", extraction_method="html_selector_parse",
               locator={"reader": "scalar", "css": "h1"})
    # `legacy_text_column` reads `row.legacy_columns`, never the payload — the old
    # fleet-wide gate accepted this and the extractor would KeyError on the first row.
    with pytest.raises(ContractError, match="not one of its substrates"):
        _entry(locator={"reader": "legacy_text_column",
                        "legacy_source_column": "listings.locality"})
    # `geom_column` reads `listings.geom` whatever the entry says its surface is.
    with pytest.raises(ContractError, match="not one of its substrates"):
        _entry(claim_type="coordinate", precision_cap={"granularity_max": {"_default": "obec"}},
               locator={"reader": "geom_column"})
    # And a payload reader stays legal on the payload surfaces it is registered for.
    assert _entry(locator={"reader": "scalar", "json_pointer": "/x"}).reader == "scalar"


def test_a_reader_that_does_not_exist_is_rejected():
    # `html_text` was the example here BECAUSE it did not exist; W2-6 registered it, so the
    # example moves to a name no wave has claimed rather than the test quietly becoming a
    # check that a real reader is accepted.
    with pytest.raises(ContractError, match="not a registered reader"):
        _entry(locator={"reader": "no_such_reader", "json_pointer": "/x"})


def test_an_executable_entry_may_not_name_an_unimplemented_transform_or_guard():
    """02 §2.1.2's vocabularies are larger than what W1 implements, and an unimplemented
    name does nothing — silently. On an entry the extractor RUNS that is the difference
    between a coordinate checked against the CZ bbox and one that never was."""
    with pytest.raises(ContractError, match="transform 'dms_to_decimal' is not implemented"):
        _entry(locator={"reader": "scalar", "json_pointer": "/x"},
               transform=["dms_to_decimal"])
    with pytest.raises(ContractError, match="guard 'reject_empty_geometry' is not implemented"):
        _entry(**COORDINATE, locator=POINT_PAIR, guards=["reject_empty_geometry"])
    # A misspelling of an implemented name is the case that motivates the check.
    with pytest.raises(ContractError, match="not implemented"):
        _entry(**COORDINATE, locator=POINT_PAIR, guards=["reject_outside_cz_bbo"])


def test_an_executable_entry_may_not_declare_what_its_own_reader_never_consults():
    """Being implemented is not enough — the entry's OWN reader has to ask. `_read_scalar`
    never calls `guard_admits` and `_read_point_pair` never calls `apply_transforms`, so
    either declaration passes an implementedness check and then does nothing: a coordinate
    entry that reads as bbox-checked and never was, which is the whole defect class."""
    with pytest.raises(ContractError, match="reader 'scalar' never evaluates guards"):
        _entry(locator={"reader": "scalar", "json_pointer": "/x"},
               guards=["reject_outside_cz_bbox"])
    with pytest.raises(ContractError, match="reader 'point_pair' never applies transforms"):
        _entry(**COORDINATE, locator=POINT_PAIR, transform=["psc_normalise"])
    # And the two readers that DO consult them keep taking an implemented name.
    assert _entry(**COORDINATE, locator=POINT_PAIR,
                  guards=["reject_outside_cz_bbox"]).guards == ["reject_outside_cz_bbox"]
    assert _entry(locator={"reader": "scalar", "json_pointer": "/x"},
                  transform=["psc_normalise"]).transform == ["psc_normalise"]


def test_a_reader_may_not_be_declared_with_an_extraction_method_it_does_not_perform():
    """Surface and method are separate axes (00 §3) and the entry states both, but a reader
    performs exactly one act: `legacy_text_column` reads a `listings` column whatever the
    entry says, and `extraction_method='portal_structured_field'` would stamp every one of
    its claims as portal-published — while `_base` keys `legacy_source_column` off the
    METHOD and would leave the column NULL, so 01 §4.2's CHECK never sees it either."""
    with pytest.raises(ContractError, match="reader 'legacy_text_column' extracts by"):
        _entry(locator_kind="legacy_column", extraction_method="portal_structured_field",
               page_kind="none",
               locator={"reader": "legacy_text_column",
                        "legacy_source_column": "listings.locality"})
    with pytest.raises(ContractError, match="reader 'declared_quality' extracts by"):
        _entry(claim_type="precision_declaration",
               locator={"reader": "declared_quality", "json_pointer": "/x"})


def test_an_executable_entry_must_name_every_locator_key_its_reader_indexes():
    """The readers index their locator keys unguarded, so a missing one is not a no-op: it
    is a bare KeyError out of `extract_listing`, which has no per-entry try/except, on the
    first row of that portal — one bad entry aborting a whole intake batch."""
    with pytest.raises(ContractError, match="locator.namespace"):
        _entry(locator={"reader": "namespaced_id", "json_pointer": "/x"})
    with pytest.raises(ContractError, match="locator.lon_pointer"):
        _entry(**COORDINATE, locator={"reader": "point_pair", "lat_pointer": "/lat"})
    with pytest.raises(ContractError, match="locator.json_pointer"):
        _entry(locator={"reader": "scalar"})


def test_a_reader_that_stamps_its_own_provenance_refuses_a_contradicting_entry():
    """`_read_geom_column` overrides `legacy_source_column` with `listings.geom` and
    `_read_coords_stamp_quality` with `raw_json.coords`, so an entry naming a different
    column states one provenance while the claim rows record another."""
    legacy = {"locator_kind": "legacy_column", "extraction_method": "legacy_column",
              "page_kind": "none"}
    with pytest.raises(ContractError, match="stamps legacy_source_column='listings.geom'"):
        _entry(**legacy, **COORDINATE,
               locator={"reader": "geom_column", "legacy_source_column": "listings.locality"})
    with pytest.raises(ContractError, match="stamps legacy_source_column='raw_json.coords'"):
        _entry(**legacy, claim_type="precision_declaration",
               locator={"reader": "coords_stamp_quality",
                        "legacy_source_column": "listings.geom"})
    assert _entry(**legacy, **COORDINATE,
                  locator={"reader": "geom_column",
                           "legacy_source_column": "listings.geom"}).reader == "geom_column"


def test_a_declared_ahead_entry_may_name_a_guard_the_runtime_has_not_implemented():
    """02 §2.2 declares the full contract, not just what today's wave can run — "a signal
    that exists on the wire and has no contract entry is a diff, not an archaeology
    project". An entry with no reader executes nowhere, so its transforms and guards are a
    specification for the wave that will implement them, not a silent no-op."""
    entry = _entry(locator_kind="html_selector", extraction_method="html_selector_parse",
                   locator={"css": ".lokalita"},
                   transform=["dms_to_decimal"],
                   guards=["require_czech_street_morphology", "reject_empty_geometry"])
    assert entry.reader is None
    assert entry.transform == ["dms_to_decimal"]
    assert entry.guards == ["require_czech_street_morphology", "reject_empty_geometry"]


def test_the_grandfathered_guards_are_inert_by_reader_and_shrink_only():
    """Two live entries named a guard from before the check existed. Neither is "pending":
    each sits on a reader that never evaluates guards at all, so implementing the name
    would not make it run — the exemption is enumerated against THAT property, not against
    implementedness. (Keying it on implementedness would force the row out the day someone
    adds `reject_sentinel` to `GUARDS`, certifying as resolved a guard that still never
    executes.) The table may only shrink, and only by a contract version bump."""
    assert set(GRANDFATHERED_INERT_GUARDS) == {"sr.det.inaccuracy_type", "sr.det.zip"}
    by_id = {e.entry_id: e for c in ALL.values() for e in c.entries}
    for entry_id, inert in GRANDFATHERED_INERT_GUARDS.items():
        entry = by_id[entry_id]
        assert entry.reader, entry_id          # an inert entry needs no exemption
        assert inert <= set(entry.guards), entry_id
        assert not READER_CONTRACTS[entry.reader].consults_guards, entry_id
    # Why tolerating them is safe: sr.det.zip's `reject_sentinel` duplicates its own
    # transform, and sr.det.inaccuracy_type's `reject_if_in_excluded_zone` asks about
    # HTML/description blocks that a `/locality/inaccuracy_type` read never touches.
    assert "sentinel_drop:-1" in by_id["sr.det.zip"].transform
    assert by_id["sr.det.inaccuracy_type"].surface == "api_json"


# ------------------------------------------------------------------ the projection SQL

def test_projection_is_idempotent_per_version_and_refuses_a_changed_body():
    """Entries are IMMUTABLE once loaded; a change is a new contract_version (02 §2.1.8)."""
    contract = ALL["maxima"]
    conn = _FakeConn(existing_sha="00" * 32)
    with pytest.raises(ContractError, match="bump contract_version"):
        contracts.project(conn, contract, git_ref="deadbeef")


def test_a_persistence_edit_refreshes_the_row_instead_of_demanding_a_version_bump():
    """`persistence` is outside `contract_sha256` (mig 408) precisely so an archive-config
    edit is not a version bump — the bump would re-stamp every claim. The psql-readable
    copy in `fetch_config` therefore has to be brought forward by the next load, or it
    would freeze at whatever the version first shipped while the scrape applied the file.
    Nothing else in `fetch_config` can ride along: the rest IS hashed, so on this path it
    is byte-identical by construction."""
    contract = ALL["maxima"]
    import copy

    stale = copy.deepcopy(contract.fetch_config)
    stale["persistence"] = {"volatile_paths": {}, "version_cap": 20}
    conn = _FakeConn(existing_sha=contract.sha256.hex(), fetch_config=stale)

    contracts.project(conn, contract, git_ref="deadbeef")

    refreshed = [p for s, p in conn.executed if "SET fetch_config" in s]
    assert len(refreshed) == 1 and refreshed[0]["id"] == 7

    # …and an unchanged projection writes nothing at all.
    quiet = _FakeConn(existing_sha=contract.sha256.hex(),
                      fetch_config=contract.fetch_config)
    contracts.project(quiet, contract, git_ref="deadbeef")
    assert not [s for s, _ in quiet.executed if "SET fetch_config" in s]


def test_projection_stands_the_incumbent_down_before_activating():
    """The partial unique index allows exactly one active header per source, so the order
    of the two UPDATEs is load-bearing."""
    contract = ALL["maxima"]
    conn = _FakeConn(existing_sha=contract.sha256.hex(),
                     fetch_config=contract.fetch_config)
    contracts.project(conn, contract, git_ref="deadbeef")
    statements = [s for s, _ in conn.executed if "portal_contracts SET is_active" in s]
    assert "is_active = false" in statements[0]
    assert "is_active = true" in statements[1]


def test_retraction_is_an_append_and_names_a_reason():
    conn = _FakeConn(existing_sha="")
    contracts.retract(conn, source="remax", version=1, reason="contract_misread",
                      retracted_by="operator")
    inserts = [s for s, _ in conn.executed if "location_claim_retractions" in s]
    assert inserts
    assert not any("delete" in s.lower() for s, _ in conn.executed)
    with pytest.raises(ContractError, match="unknown retraction reason"):
        contracts.retract(conn, source="remax", version=1, reason="because",
                          retracted_by="operator")


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        self._sql = sql

    def fetchone(self):
        sql = " ".join(self._sql.split())
        if "FROM portal_contracts WHERE source" in sql or "encode(contract_sha256" in sql:
            return ((7, self._conn.existing_sha, False, self._conn.fetch_config)
                    if self._conn.existing_sha else None)
        return (7,)

    def fetchall(self):
        return []


class _FakeConn:
    """Enough psycopg surface to assert on statement ORDER. It cannot catch a CHECK or a
    UNIQUE violation — those belong to the migration's own tests."""

    def __init__(self, existing_sha: str, fetch_config: object = None):
        self.existing_sha = existing_sha
        self.fetch_config = fetch_config
        self.executed: list[tuple[str, object]] = []

    def cursor(self):
        return _FakeCursor(self)

    def transaction(self):
        return _FakeCursor(self)


def _column_types_from_382() -> dict[str, dict[str, str]]:
    """Map table -> column -> type for the two contract tables, parsed from migration 382
    (the DDL source of truth), so a new jsonb column cannot dodge the bind check below."""
    sql = (Path(__file__).resolve().parents[2]
           / "migrations" / "382_location_w1_claims.sql").read_text()
    out: dict[str, dict[str, str]] = {}
    for table in ("portal_contracts", "portal_contract_entries"):
        start = sql.index(f"create table {table} (")
        body = sql[start:sql.index("\n);", start)]
        cols: dict[str, str] = {}
        for line in body.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("--") or line.split()[0] in (
                    "unique", "check", "primary", "foreign", "constraint"):
                continue
            name, _, rest = line.partition(" ")
            cols[name] = rest.strip().split()[0].rstrip(",")
        out[table] = cols
    return out


def test_every_jsonb_column_param_is_bound_as_jsonb():
    """psycopg adapts a bare Python list as a Postgres ARRAY literal ('{x,y}'), which is
    invalid input syntax for a jsonb column. The first production projection crashed on
    exactly this: portal_contract_entries.transform (list[str]) reached jsonb unwrapped
    (run 31428625090, Token "psc_normalise"). Assert every param bound to a jsonb column
    is a psycopg Jsonb wrapper, and every text[] column gets a plain list, across ALL
    nine real contracts."""
    import psycopg.types.json

    types = _column_types_from_382()
    checked_jsonb = 0
    saw_nonempty_transform = False
    for contract in ALL.values():
        conn = _FakeConn(existing_sha="")
        contracts.project(conn, contract, git_ref="deadbeef")
        for sql, params in conn.executed:
            if not isinstance(params, dict):
                continue
            if "INSERT INTO portal_contract_entries" in sql:
                cols = types["portal_contract_entries"]
            elif "INSERT INTO portal_contracts" in sql:
                cols = types["portal_contracts"]
            else:
                continue
            for key, value in params.items():
                decl = cols.get(key, "")
                if decl.startswith("jsonb"):
                    assert isinstance(value, psycopg.types.json.Jsonb), (
                        f"{key} targets a jsonb column but was bound as "
                        f"{type(value).__name__} — psycopg would send an array/text "
                        f"literal that jsonb rejects")
                    checked_jsonb += 1
                    if key == "transform" and value.obj:
                        saw_nonempty_transform = True
                elif decl.startswith("text[]"):
                    assert isinstance(value, list), (
                        f"{key} targets text[] and must stay a plain list, not "
                        f"{type(value).__name__}")
    assert checked_jsonb > 0
    assert saw_nonempty_transform, (
        "no contract exercised a non-empty transform — the regression case "
        "(bazos/sreality psc_normalise) has gone missing")
