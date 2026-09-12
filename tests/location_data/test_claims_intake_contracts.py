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

from location_data import claims_intake, contracts, page_readers
from location_data.claims_intake import GUARDS, READERS, SOURCES, TRANSFORMS
from location_data.page_readers import PAGE_READERS
from location_data.contracts import (
    CLAIM_TYPES,
    EXTRACTION_METHODS,
    EXTRACTOR_PREFIXES,
    IMPLEMENTED_GUARDS,
    IMPLEMENTED_TRANSFORMS,
    MANDATORY_CLAIM_TYPE,
    READER_CONTRACTS,
    READER_SUBSTRATES,
    RETIRED_ENTRY_KEYS,
    ContractError,
    parse_contract,
    parse_entry,
)

_ALL: dict[str, contracts.PortalContract] = {}


def _all() -> dict[str, contracts.PortalContract]:
    """The shipped contracts, loaded on FIRST USE rather than at import.

    The loader refuses a contract that breaks the shape rules, and at import time that
    refusal is a COLLECTION error: the whole module — including the refusal tests that
    prove the rules — goes unrun, and the one thing CI reports is that it could not look.
    A census reds; a unit test of the parser does not."""
    if not _ALL:
        _ALL.update({c.source: c for c in contracts.load_all()})
    return _ALL

MINIMAL = {
    "id": "sr.det.thing",
    "locator_kind": "api_json",
    "extraction_method": "portal_structured_field",
    "page_kind": "detail",
    "locator": {"reader": "scalar", "json_pointer": "/locality/street"},
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
# The PAGE readers live in their own module behind `@page_reader`, so the `_INTAKE_AST`
# scan below cannot see them. W2-6 registered three DOM readers in READER_CONTRACTS that no
# body-vs-contract check introspected at all — an adversarial review caught
# `html_point_dms` declaring `consults_guards=True` while never calling `guard_admits`,
# i.e. exactly the misdeclaration this file's gate exists to make impossible, surviving
# because the gate could not see the reader.
_ARCHIVE_AST = ast.parse(
    Path(page_readers.__file__).read_text(encoding="utf-8"))


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


# ------------------------------------------------------------------ the nine contracts

def test_every_portal_has_exactly_one_contract_file():
    assert set(_all()) == set(SOURCES)
    assert len(list((Path(contracts.CONTRACT_DIR)).glob("*.yaml"))) == 9


def test_extractor_id_prefixes_are_portal_unique_and_permanent():
    """Revision 1 gave bezrealitky AND bazos the prefix `bz.`, which would have merged the
    provenance of a GraphQL portal and an HTML portal on ids that may never be renamed."""
    assert EXTRACTOR_PREFIXES["bezrealitky"] == "bzr."
    assert EXTRACTOR_PREFIXES["bazos"] == "bzs."
    assert len(set(EXTRACTOR_PREFIXES.values())) == 9
    for source, contract in _all().items():
        for entry in contract.entries:
            assert entry.entry_id.startswith(EXTRACTOR_PREFIXES[source])


def test_every_entry_states_both_axes_and_a_canonical_claim_type():
    """00 §3.2: `locator_kind` IS the surface and `extraction_method` is a separate,
    mandatory field — an html_selector locator can be html_selector_parse, breadcrumb_parse
    or map_widget_parse. The claim type is one of the ELEVEN (rule 25 / W1-c): a type
    outside them is a claim no resolver reads."""
    assert len(CLAIM_TYPES) == 11
    for contract in _all().values():
        for entry in contract.entries:
            assert entry.claim_type in CLAIM_TYPES, entry.entry_id
            assert entry.extraction_method in EXTRACTION_METHODS, entry.entry_id
            assert entry.surface != "portal_json"
            assert entry.surface != "legacy_column", entry.entry_id


def test_every_contract_states_one_carrier_per_type_and_a_town():
    """The three rule-25 shape rails, asserted over the shipped fleet rather than only in
    the parser's unit tests: one entry per claim type, every entry executable, and a town
    entry on every portal. A portal that cannot state a town cannot satisfy the invariant
    the programme is measured by (`location_town_coverage`)."""
    for source, contract in _all().items():
        types = [e.claim_type for e in contract.entries]
        assert len(types) == len(set(types)), (
            source, sorted(t for t in types if types.count(t) > 1))
        assert MANDATORY_CLAIM_TYPE in types, source
        town = next(e for e in contract.entries if e.claim_type == MANDATORY_CLAIM_TYPE)
        assert town.reader in READERS, source


def test_the_contract_files_carry_only_the_allowed_top_level_keys():
    """SIX keys (W1-c R2). Read off the FILES rather than off the parsed object: the parser
    drops what it does not model, so a key it silently ignored would be invisible to every
    assertion made on a `PortalContract`."""
    import yaml

    for path in sorted(Path(contracts.CONTRACT_DIR).glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(doc) <= contracts._TOP_LEVEL_KEYS, (path.name, sorted(doc))
        assert {"portal", "contract_version", "extractions"} <= set(doc), path.name


def test_every_reader_named_in_a_contract_exists_in_the_registry():
    for contract in _all().values():
        for entry in contract.entries:
            # ONE registry (rule 25): the payload readers and the fourteen page readers,
            # both executed by the same lane. `_check_executable` already refuses a name
            # outside it — and refuses an entry that names none at all.
            assert entry.reader in READERS, entry.entry_id


def test_every_executable_entry_matches_its_readers_contract():
    """Legality is per READER on every axis, not fleet-wide: a reader mines one substrate
    by one method, and every locator key it indexes must be there, because the extractor
    indexes them unguarded."""
    for contract in _all().values():
        for entry in contract.entries:
            spec = READER_CONTRACTS[entry.reader]
            assert entry.surface in spec.substrates, entry.entry_id
            assert entry.extraction_method in spec.methods, entry.entry_id
            assert spec.locator_keys <= set(entry.locator), entry.entry_id
            if entry.transform:
                assert spec.consults_transforms, entry.entry_id
            if entry.guards:
                assert spec.consults_guards, entry.entry_id


def test_reader_substrates_stay_in_sync_with_the_runtime_registry():
    """`contracts.READER_CONTRACTS` is pure data — the deploy-time lane must not import
    the extractor — so a reader added to `claims_intake` without a record (or one left
    behind after a reader is deleted) is caught HERE, by the one test that imports both.
    Otherwise the projection would reject every entry naming the new reader."""
    # ONE runtime registry, folded at import from the page-reader module, with ONE
    # deploy-time record. The three name-only mirrors are gone: a name that is not in
    # `READERS` is a deploy error again, which is the question the preflight asks.
    assert set(READER_CONTRACTS) == set(READERS)
    assert set(PAGE_READERS) <= set(READERS)
    assert {n for n, r in READERS.items()
            if r.substrate == claims_intake.SUBSTRATE_ARCHIVED_HTML} == set(PAGE_READERS)
    assert READER_SUBSTRATES == {n: s.substrates for n, s in READER_CONTRACTS.items()}
    surfaces = {s for legal in READER_SUBSTRATES.values() for s in legal}
    assert surfaces <= contracts.CLAIM_SURFACES
    # W2-6 opened the DOM surfaces; W2's reader canon opened the three that carry a fact the
    # DOM readers cannot reach — a JSON document embedded in the page, a fact published only
    # inside a link, and a schema.org JSON-LD block. Written as "no reader may be declared on
    # a W2 surface until W2 gives it one" and updated here deliberately — still an exact set,
    # so a further surface cannot arrive unreviewed.
    assert surfaces == {
        "api_json", "graphql", "embedded_json",
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
        "portal_structured_field", "portal_declared_quality",
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
    assert set(bodies) == {n for n, r in READERS.items()
                           if r.substrate == claims_intake.SUBSTRATE_PAYLOAD}
    for name, fn in bodies.items():
        spec = READER_CONTRACTS[name]
        calls = _called_names(fn)
        assert spec.consults_transforms == ("apply_transforms" in calls), name
        assert spec.consults_guards == ("guard_admits" in calls), name
        assert spec.locator_keys == _indexed_locator_keys(fn), name
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
    are decorated `@page_reader` and live in `page_readers`, while `_reader_bodies()` reads
    `@reader` out of `claims_intake`. The consequence was not
    hypothetical — `html_point_dms` shipped declaring `consults_guards=True` while never
    calling `guard_admits`, which would have let any entry naming it declare a guard the
    runtime silently ignored. Review caught it; this makes review unnecessary.

    Kept as a SEPARATE test rather than folded into the one above because the two reader
    FAMILIES live in two modules behind two decorators, and a single test asserting over
    both would go green if one of them vanished.

    NARROWER than the W1 gate, deliberately and disclosed rather than implied: it checks the
    two `consults_*` flags but NOT `locator_keys`, because the DOM readers address their
    locator through `.get()` plus an explicit refusal (`_entry_css`, the attr-pair check)
    rather than the unguarded `entry.locator["key"]` indexing `_indexed_locator_keys` looks
    for. Asserting equality there would compare a set against an empty one. Closing it
    properly needs the scan to recognise the refusal helpers; until then this is a known
    half, not an assumed whole."""
    bodies = _reader_bodies(_ARCHIVE_AST, "page_reader")
    assert set(bodies) == set(PAGE_READERS)
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


def test_the_payload_half_executes_no_evidence_bearing_method():
    """`regex_text` / `llm_text` need a span into a RETRIEVABLE document, and
    `listings.raw_json` is latest-wins JSON nobody archived (01 §4.2). The stored page body
    IS retrievable (content-addressed, immutable), so the page half may carry them — which
    is the whole reason the two substrates stay distinguishable inside one registry."""
    for contract in _all().values():
        for entry in contract.entries:
            if entry.extraction_method in ("regex_text", "llm_text"):
                assert (READERS[entry.reader].substrate
                        == claims_intake.SUBSTRATE_ARCHIVED_HTML), entry.entry_id


def test_coordinate_entries_carry_a_cap_and_a_licence_class():
    for contract in _all().values():
        for entry in contract.entries:
            if entry.claim_type == "coordinate":
                assert entry.precision_map.get("precision_cap"), entry.entry_id
                assert entry.default_licence_class, entry.entry_id


def test_blurred_label_sets_ride_on_the_contract_not_in_code():
    """WHICH labels mean "this pin is blurred" is a portal fact, so it is data on the entry
    — re-calibrating it is a version bump, never a code change. Which portals declare which
    labels is asserted in the per-portal contract tests; the fleet rail is that a set only
    ever rides on the entry that carries the portal's own precision signal."""
    blurred = {
        e.entry_id: (e.claim_type, e.precision_map["blurred_labels"])
        for c in _all().values() for e in c.entries if e.precision_map.get("blurred_labels")
    }
    assert blurred, "no portal declares a blurred-label set — the calibration is in code"
    for entry_id, (claim_type, labels) in blurred.items():
        assert claim_type == "precision_declaration", entry_id
        assert labels and all(isinstance(x, str) and x for x in labels), entry_id


def test_exclusion_zones_name_every_portals_decoy():
    """Every portal ships at least one fully-formed address-shaped decoy (02 §2.5)."""
    for source, contract in _all().items():
        assert contract.exclusion_zones, source
    sreality_zones = str(_all()["sreality"].exclusion_zones)
    assert "/premise" in sreality_zones
    assert "area-listings__item" in str(_all()["remax"].exclusion_zones)


def test_contract_sha256_is_taken_from_the_governed_bytes_on_disk():
    """The bytes on disk minus the two blocks that are not extraction (mig 404, 408) —
    so the hash covers exactly what a bump of `contract_version` would re-stamp."""
    contract = _all()["maxima"]
    assert contract.path is not None
    import hashlib
    body = contract.path.read_bytes()
    assert contract.sha256 == contracts.contract_body_hash(body)
    assert contract.sha256 != hashlib.sha256(body).digest(), (
        "maxima declares persistence.volatile_paths, so the governed hash must differ "
        "from a whole-file hash — otherwise this test proves nothing")
    assert contracts.extractor_version(contract) == f"contract:maxima@{contract.version}"


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


def test_a_wrong_prefix_is_rejected():
    with pytest.raises(ContractError, match="permanent"):
        parse_entry(dict(MINIMAL, id="bz.det.thing"), source="sreality", index=0)


def test_a_reader_outside_its_registered_substrates_is_rejected():
    """The reader IS the substrate declaration: a payload reader on a DOM surface (or the
    reverse) reads the wrong document while stamping the claim's provenance as the other
    one."""
    with pytest.raises(ContractError, match="not one of its substrates"):
        _entry(locator_kind="html_selector", extraction_method="html_selector_parse",
               locator={"reader": "scalar", "css": "h1"})
    # ... and a page reader on the payload surface, which has no document to scope at all.
    with pytest.raises(ContractError, match="not one of its substrates"):
        _entry(locator={"reader": "html_text", "css": "h1"})
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
    performs exactly one act. `regex_text` is the one that matters most: 01 §4.2 makes it
    evidence-bearing, so declaring it on a reader that lifts a whole node would stamp a
    provenance whose mandatory quote-plus-span the claim does not carry."""
    with pytest.raises(ContractError, match="reader 'scalar' extracts by"):
        _entry(extraction_method="regex_text",
               locator={"reader": "scalar", "json_pointer": "/x"})
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


def test_an_entry_that_names_no_reader_is_refused():
    """The ahead-declaration is gone (W1-c R1). "Declared for the wave that will run it" is
    how a contract grows entries nothing executes: projected, counted in every census,
    extracting nothing — and the fleet carried 47 of them. A contract states what it reads
    today; the next wave's entry arrives with the wave, in a version bump."""
    with pytest.raises(ContractError, match="names no locator.reader"):
        _entry(locator_kind="html_selector", extraction_method="html_selector_parse",
               locator={"css": ".lokalita"})


def test_a_retired_entry_key_is_refused():
    """`cardinality` / `required` / `on_conflict` read like rails and were enforced by
    nothing — `required: always` never made a missing value an error anywhere (W1-c R3)."""
    for key, value in (("cardinality", "many"), ("required", "always"),
                       ("on_conflict", "emit_both")):
        assert key in RETIRED_ENTRY_KEYS
        with pytest.raises(ContractError, match=key):
            _entry(**{key: value})


def test_a_legacy_column_entry_is_refused_on_either_axis():
    """The lane reads `raw_json` and the stored page body. A `listings` column is neither,
    and the three readers that mined one went with the surface."""
    with pytest.raises(ContractError, match="legacy_column surface is retired"):
        _entry(locator_kind="legacy_column", extraction_method="legacy_column",
               page_kind="none", locator={"reader": "scalar", "json_pointer": "/x"})
    with pytest.raises(ContractError, match="legacy_column surface is retired"):
        _entry(extraction_method="legacy_column")
    for gone in ("legacy_text_column", "geom_column", "coords_stamp_quality"):
        assert gone not in READER_CONTRACTS
        assert gone not in READERS


def test_a_claim_type_outside_the_eleven_is_refused():
    """The other 29 `location_claim_type` labels are still enum members — a Postgres enum
    cannot shrink in place — and the loader is what keeps them out of a contract until W4
    drops them."""
    for retired in ("uncertainty_geometry", "map_zoom", "blur_hint", "postal_town",
                    "obec_code", "address_line_verbatim"):
        with pytest.raises(ContractError, match="claim_type"):
            _entry(claim_type=retired)


# ------------------------------------------------------- the per-CONTRACT shape rules

def _contract_file(tmp_path, *, extractions, portal="sreality", version=1, **extra):
    import yaml

    doc = {"portal": portal, "contract_version": version,
           "exclusion_zones": [{"locator_kind": "description",
                                "locator": {"pattern": "x"}, "reason": "decoy"}],
           "extractions": extractions, **extra}
    path = tmp_path / f"{portal}.yaml"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return path


_TOWN = {
    "id": "sr.det.city", "locator_kind": "api_json",
    "extraction_method": "portal_structured_field", "page_kind": "detail",
    "locator": {"reader": "scalar", "json_pointer": "/locality/city"},
    "claim_type": "obec_name",
}
_STREET = dict(MINIMAL)


def test_a_contract_with_a_town_entry_parses(tmp_path):
    contract = parse_contract(_contract_file(tmp_path, extractions=[_TOWN, _STREET]))
    assert [e.claim_type for e in contract.entries] == ["obec_name", "street_name"]
    assert contract.fetch_config == {"persistence": {}, "regressions": []}


def test_a_second_entry_of_one_claim_type_is_refused(tmp_path):
    """Two entries of a type make the contract a vote the survivorship policy never asked
    for: both claims reach S7 with the same (source, extraction_method), so which one wins
    is the order the DB happened to return them in."""
    twin = dict(_STREET, id="sr.det.street2",
                locator={"reader": "scalar", "json_pointer": "/locality/street2"})
    with pytest.raises(ContractError, match="more than one entry per claim type"):
        parse_contract(_contract_file(tmp_path, extractions=[_TOWN, _STREET, twin]))


def test_a_contract_with_no_town_entry_is_refused(tmp_path):
    """Rule 25's invariant is measured per portal, so the portal is where it is enforced."""
    with pytest.raises(ContractError, match="declares no obec_name entry"):
        parse_contract(_contract_file(tmp_path, extractions=[_STREET]))


def test_a_retired_top_level_key_is_refused(tmp_path):
    """The eight keys W1-c dropped were read by nothing, and an unknown key has always
    failed open the same way: a typo'd `extractoins:` projects a header with no entries."""
    for key in ("identity_ladder", "precision_caps", "precision_priors",
                "extractor_runtime", "fetch", "payload_schema_detector",
                "pin_collision_semantics", "contract_sha256"):
        with pytest.raises(ContractError, match="unknown top-level key"):
            parse_contract(_contract_file(tmp_path, extractions=[_TOWN], **{key: {}}))


# ------------------------------------------------------------------ the projection SQL

def test_projection_is_idempotent_per_version_and_refuses_a_changed_body():
    """Entries are IMMUTABLE once loaded; a change is a new contract_version (02 §2.1.8)."""
    contract = _all()["maxima"]
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
    contract = _all()["maxima"]
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
    contract = _all()["maxima"]
    conn = _FakeConn(existing_sha=contract.sha256.hex(),
                     fetch_config=contract.fetch_config)
    contracts.project(conn, contract, git_ref="deadbeef")
    statements = [s for s, _ in conn.executed if "portal_contracts SET is_active" in s]
    assert "is_active = false" in statements[0]
    assert "is_active = true" in statements[1]


def test_retraction_deletes_the_versions_claims_and_enqueues_their_listings():
    """W1-b: retraction stopped being an append. A contract version that misread the
    portal produced no evidence, so its claims are DELETED and their listings go into
    `dirty_locations` — one statement per batch, so the delete and the re-resolve queue
    cannot separate, and no ledger row survives for a view to subtract on every read."""
    conn = _FakeConn(existing_sha="", claim_batches=[7, 5, 0])
    done = contracts.retract(conn, source="remax", version=1)
    assert (done.deleted, done.enqueued, done.batches) == (12, 12, 2)

    statements = [s for s, _ in conn.executed]
    assert not any("location_claim_retractions" in s for s in statements)
    # The target is RESOLVED first — an empty resolution is an error, not deleted=0.
    assert any("FROM portal_contract_entries pce" in s for s in statements)
    deletes = [s for s in statements if "DELETE FROM location_claims" in s]
    # Batched: it drained until a batch came back empty, and each batch is one statement
    # carrying its own enqueue (a version can hold millions of rows; one atomic DELETE of
    # that size burns its timeout and rolls back, making no progress ever).
    assert len(deletes) == 3
    assert all("INSERT INTO dirty_locations" in d for d in deletes)
    assert all("LIMIT %(batch_size)s" in d for d in deletes)
    # An EXISTING reason value — the drain rebuilds the projection whatever the label says.
    assert all("'claim_insert'" in d for d in deletes)
    # Each batch is bounded on its own; the LOOP is not.
    assert len([s for s in statements if "statement_timeout" in s]) == 3
    # The whole version: the header is stood down so the next deploy activates a fix.
    assert any("portal_contracts SET is_active = false" in s for s in statements)


def test_retracting_one_entry_leaves_the_header_active():
    conn = _FakeConn(existing_sha="", claim_batches=[4, 0])
    contracts.retract(conn, source="remax", version=1, extractor_id="rx.det.street")
    params = next(p for s, p in conn.executed if "FROM portal_contract_entries pce" in s)
    assert params["extractor_id"] == "rx.det.street"
    assert not any("retired_at = now()" in s for s, _ in conn.executed)


def test_a_target_that_matches_no_contract_version_is_an_error_not_a_no_op():
    """`deleted=0 enqueued=0` + exit 0 reads as "done". A typo'd portal, a version that was
    never projected and an `--extractor-id` that names nothing are all the same shape as a
    version that genuinely had no claims, and an operator retracting the WRONG thing has to
    be told so — the right one is still live."""
    conn = _FakeConn(existing_sha="", entry_ids=[])
    with pytest.raises(ContractError, match="no such contract version"):
        contracts.retract(conn, source="remax", version=99)
    assert not [s for s, _ in conn.executed if "DELETE FROM location_claims" in s]


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
        if "DELETE FROM location_claims" in sql:
            n = (self._conn.claim_batches.pop(0) if self._conn.claim_batches else 0)
            return (n, n)
        return (7,)

    def fetchall(self):
        sql = " ".join(self._sql.split())
        if "FROM portal_contract_entries pce" in sql:
            return [(i,) for i in self._conn.entry_ids]
        return []


class _FakeConn:
    """Enough psycopg surface to assert on statement ORDER. It cannot catch a CHECK or a
    UNIQUE violation — those belong to the migration's own tests."""

    def __init__(self, existing_sha: str, fetch_config: object = None,
                 claim_batches: list[int] | None = None,
                 entry_ids: list[int] | None = None):
        self.existing_sha = existing_sha
        self.fetch_config = fetch_config
        # Successive `deleted` counts the batched retraction loop sees, ending in 0.
        self.claim_batches = list(claim_batches or [])
        self.entry_ids = [1000, 1001] if entry_ids is None else list(entry_ids)
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
    is a psycopg Jsonb wrapper, and every text[] column gets a plain list, across _all()
    nine real contracts."""
    import psycopg.types.json

    types = _column_types_from_382()
    checked_jsonb = 0
    saw_nonempty_transform = False
    for contract in _all().values():
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


# ------------------------------------------------------- the run() preflight (2026-09-06)

def _bare_entry(reader: str | None) -> claims_intake.Entry:
    """A minimal Entry built field-by-field, bypassing `parse_entry`: this tests the
    preflight PREDICATE, not the contract parser, and must not depend on what the parser
    admits for a given reader/surface pair."""
    import dataclasses
    values: dict[str, object] = {}
    for f in dataclasses.fields(claims_intake.Entry):
        if f.name == "locator":
            values[f.name] = {"reader": reader} if reader else {}
        elif f.name in ("transform", "guards"):
            values[f.name] = ()
        elif f.type in ("dict[str, Any]",):
            values[f.name] = {}
        elif f.name in ("id", "contract_id", "contract_version"):
            values[f.name] = 1
        else:
            values[f.name] = "x"
    return claims_intake.Entry(**values)


def test_the_preflight_admits_both_substrates_of_the_one_registry():
    """`run()`'s preflight refuses an ACTIVE contract naming a reader nothing implements.
    The seven W2 activations put page readers on every active contract while the preflight
    compared against the payload registry alone and refused first — the hourly intake was
    dead for all nine portals from 2026-09-06. With one registry there is one answer."""
    by_source = {
        "a": [_bare_entry("point_pair")],          # the payload substrate
        "b": [_bare_entry("html_own_text")],       # the page substrate
        "d": [_bare_entry(None)],                  # declared, no reader yet: inert
    }
    assert claims_intake.unknown_readers(by_source, ["a", "b", "d"]) == []


def test_the_preflight_still_refuses_a_reader_no_lane_implements():
    by_source = {"a": [_bare_entry("point_pair"), _bare_entry("no_such_reader")]}
    assert claims_intake.unknown_readers(by_source, ["a"]) == ["x:no_such_reader"]


def test_every_shipped_contract_passes_the_intake_preflight():
    """The test that would have caught the outage: the exact production predicate over
    the exact production contracts, off disk. Any future contract naming a reader the
    registry does not carry reds here instead of taking the hourly intake down an hour
    after merge."""
    from tests.location_data.claim_intake_fixtures import entries_for
    by_source = {s: entries_for(s) for s in SOURCES}
    assert claims_intake.unknown_readers(by_source, list(SOURCES)) == []
    # and the census this guards is not vacuous: the shipped contracts DO name page readers
    page = {e.reader for es in by_source.values() for e in es
            if e.reader and READERS[e.reader].substrate
            == claims_intake.SUBSTRATE_ARCHIVED_HTML}
    assert page
