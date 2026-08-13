"""The contract layer — format validation and the deploy-time projection (02 §2.1).

The nine YAML files in `contracts/portals/` are the store of record; `portal_contracts` +
`portal_contract_entries` are their projection. These tests are the CI half of §2.1.8's
lifecycle: a contract that would write nonsense into an append-only table must fail here,
not at INSERT time, and never at resolution time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from location_data import contracts
from location_data.claims_intake import LEGACY_COLUMNS, READERS, SOURCES
from location_data.contracts import (
    CLAIM_TYPES,
    EXTRACTION_METHODS,
    EXTRACTOR_PREFIXES,
    W1_SUBSTRATE_SURFACES,
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


def _entry(**overrides):
    raw = dict(MINIMAL)
    raw.update(overrides)
    return parse_entry(raw, source="sreality", index=0)


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


def test_readers_exist_and_only_on_raw_json_reachable_surfaces():
    for contract in ALL.values():
        for entry in contract.entries:
            if not entry.reader:
                continue
            assert entry.reader in READERS, entry.entry_id
            assert entry.surface in W1_SUBSTRATE_SURFACES, entry.entry_id


def test_w1_executes_no_evidence_bearing_method():
    """`regex_text` / `llm_text` need a span into a retrievable document, and the
    content-addressed body store does not fill until W2a (01 §4.2)."""
    for contract in ALL.values():
        for entry in contract.entries:
            if entry.extraction_method in ("regex_text", "llm_text"):
                assert entry.reader is None, entry.entry_id


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
    assert {s: c.version for s, c in ALL.items()} == {
        "remax": 2, "ceskereality": 3, "realitymix": 3,
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


def test_contract_sha256_is_taken_from_the_bytes_on_disk():
    contract = ALL["maxima"]
    assert contract.path is not None
    import hashlib
    assert contract.sha256 == hashlib.sha256(contract.path.read_bytes()).digest()
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


def test_a_reader_on_an_html_surface_is_rejected():
    with pytest.raises(ContractError, match="W2 surface"):
        _entry(locator_kind="html_selector", extraction_method="html_selector_parse",
               locator={"reader": "scalar", "css": "h1"})


# ------------------------------------------------------------------ the projection SQL

def test_projection_is_idempotent_per_version_and_refuses_a_changed_body():
    """Entries are IMMUTABLE once loaded; a change is a new contract_version (02 §2.1.8)."""
    contract = ALL["maxima"]
    conn = _FakeConn(existing_sha="00" * 32)
    with pytest.raises(ContractError, match="bump contract_version"):
        contracts.project(conn, contract, git_ref="deadbeef")


def test_projection_stands_the_incumbent_down_before_activating():
    """The partial unique index allows exactly one active header per source, so the order
    of the two UPDATEs is load-bearing."""
    contract = ALL["maxima"]
    conn = _FakeConn(existing_sha=contract.sha256.hex())
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
            return (7, self._conn.existing_sha, False) if self._conn.existing_sha else None
        return (7,)

    def fetchall(self):
        return []


class _FakeConn:
    """Enough psycopg surface to assert on statement ORDER. It cannot catch a CHECK or a
    UNIQUE violation — those belong to the migration's own tests."""

    def __init__(self, existing_sha: str):
        self.existing_sha = existing_sha
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
