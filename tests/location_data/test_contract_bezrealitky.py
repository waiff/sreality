"""The bezrealitky slim location contract (rule 25, W1-c).

One entry per claim type, every entry executable on the hourly PAYLOAD lane, and the town
entry mandatory. bezrealitky's substrate is the GraphQL advert JSON already stored in
`listings.raw_json` (`scraper/bezrealitky_parser.parse_advert` keeps `dict(advert)`), so
there is no page-body arm here and no archived reader may appear.

The extraction assertions run the REAL readers over the committed advert payload
`claim_intake_fixtures.BEZREALITKY` — the same body the fixture-diff gate scores. The one
exception is `bzr.det.country`, which reads `addressInput`: that key is in the live detail
query (`scraper/bezrealitky_client._DETAIL_QUERY`, pinned by
`tests/scraper/test_bezrealitky_parser.py::test_ruian_identity_fields_reach_raw`) but not
in the recon-era shared fixture, which still carries the pre-W0 spelling
`addressUserInput`. That arm therefore runs over the live-query shape, built here from the
same committed line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from location_data import claims_intake, contracts, page_readers
from location_data.claims_common import SUBSTRATE_PAYLOAD
from location_data.claims_intake import (
    DEFAULT_MAX_CLAIM_VALUE_BYTES,
    Claim,
    extract_listing,
)
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT = _ROOT / "contracts" / "portals" / "bezrealitky.yaml"

CONTRACT_VERSION = 2

# The town entry. Named here rather than derived, because "which entry carries the town"
# is the one fact rule 25 makes mandatory — deriving it from the file would let a rename
# pass silently.
TOWN_ENTRY_ID = "bzr.det.city"
TOWN_READER = "scalar"

# id -> claim_type, in file order. The ids are permanent (02 §2.1.8: never reused), so this
# is also the pin that a slimmed contract kept the ids of the acts it kept.
ENTRIES: dict[str, str] = {
    "bzr.det.gps": "coordinate",
    "bzr.det.city": "obec_name",
    "bzr.det.city_district": "cast_obce_name",
    "bzr.det.street": "street_name",
    "bzr.det.house_number_cp": "house_number_cp",
    "bzr.det.house_number_co": "house_number_co",
    "bzr.det.zip": "psc",
    "bzr.det.country": "country",
}

# One assertion per entry over the committed advert payload. `value_text` for the text
# claims; the coordinate is pinned as WKT because that is what the claim carries.
EXPECTED: dict[str, tuple[str, str]] = {
    "bzr.det.gps": ("value_geom_wkt", "POINT(14.4749 50.1092)"),
    "bzr.det.city": ("value_text", "Praha"),
    "bzr.det.city_district": ("value_text", "Praha - Libeň"),
    "bzr.det.street": ("value_text", "Davídkova"),
    "bzr.det.house_number_cp": ("value_text", "655"),
    "bzr.det.house_number_co": ("value_text", "31"),
    "bzr.det.zip": ("value_text", "15400"),
}

# The prior each entry keeps from bezrealitky@1 (W1-c R3: `prior:` survives the slim, the
# resolver reads it until W2). An entry absent here declares none.
PRIORS: dict[str, dict[str, str]] = {
    "bzr.det.gps": {"position_source": "portal_pin", "match_confidence": "medium"},
    "bzr.det.city": {"granularity": "obec"},
    "bzr.det.city_district": {"granularity": "cast_obce_or_quarter"},
    "bzr.det.street": {"granularity": "street"},
    "bzr.det.country": {"granularity": "country"},
}

# The advert as the LIVE detail query returns it: `addressInput`, not the recon-era
# `addressUserInput`. The line is the one committed in
# `tests/scraper/test_bezrealitky_parser.py::test_ruian_identity_fields_reach_raw`.
LIVE_ADDRESS_INPUT = "Poděbradská, Hloubětín, Praha 14, 194 00, Česko"


def _live_query_shape(**overrides: Any) -> dict[str, Any]:
    advert = {k: v for k, v in fx.BEZREALITKY.items() if k != "addressUserInput"}
    advert["addressInput"] = LIVE_ADDRESS_INPUT
    advert.update(overrides)
    return advert


def _by_id(row: Any) -> dict[str, list[Claim]]:
    result = extract_listing(row, fx.entries_for("bezrealitky"),
                             max_value_bytes=DEFAULT_MAX_CLAIM_VALUE_BYTES)
    grouped: dict[str, list[Claim]] = {}
    for claim in result.claims:
        grouped.setdefault(claim.extractor_id, []).append(claim)
    return grouped


@pytest.fixture(scope="module")
def contract() -> contracts.PortalContract:
    return contracts.parse_contract(_CONTRACT)


@pytest.fixture(scope="module")
def raw_yaml() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def claims() -> dict[str, list[Claim]]:
    return _by_id(fx.listing("bezrealitky", fx.BEZREALITKY, native="1037096"))


def test_the_contract_version_is_pinned(contract: contracts.PortalContract) -> None:
    assert contract.source == "bezrealitky"
    assert contract.version == CONTRACT_VERSION


def test_the_entry_ids_are_exactly_the_declared_set(
        contract: contracts.PortalContract) -> None:
    assert [e.entry_id for e in contract.entries] == list(ENTRIES)
    assert {e.entry_id: e.claim_type for e in contract.entries} == ENTRIES


def test_at_most_one_entry_per_claim_type(contract: contracts.PortalContract) -> None:
    """Rule 25's shape rule. A second entry for one type is two answers to one question."""
    types = [e.claim_type for e in contract.entries]
    assert len(types) == len(set(types)), sorted(types)
    assert set(types) <= contracts.CLAIM_TYPES


def test_every_entry_is_executable_by_the_hourly_payload_lane(
        contract: contracts.PortalContract) -> None:
    """bezrealitky has no page body, so every entry must name a reader THIS lane runs.

    `contracts.READER_CONTRACTS` is a superset — it carries the 14 page readers too — so
    the pin is the registry's substrate, which is what decides whether the hourly lane
    executes the entry at all. An entry naming a DOM reader here would be projected,
    counted in every census, and never run.
    """
    payload_ids = {e.entry_id for e in claims_intake.payload_entries(
        fx.entries_for("bezrealitky"))}
    for entry in contract.entries:
        reader = entry.locator.get("reader")
        assert reader, entry.entry_id
        assert claims_intake.READERS[reader].substrate == SUBSTRATE_PAYLOAD, (
            entry.entry_id, reader)
        assert reader not in page_readers.PAGE_READERS, (entry.entry_id, reader)
        assert entry.entry_id in payload_ids, entry.entry_id


def test_the_town_entry_is_present_and_names_its_reader(
        contract: contracts.PortalContract) -> None:
    """Rule 25: the `obec_name` entry is mandatory and live."""
    town = [e for e in contract.entries if e.claim_type == "obec_name"]
    assert len(town) == 1
    assert town[0].entry_id == TOWN_ENTRY_ID
    assert town[0].locator["reader"] == TOWN_READER
    assert town[0].locator["json_pointer"] == "/city"


def test_the_file_carries_only_the_allowed_keys(raw_yaml: dict[str, Any]) -> None:
    """W1-c R2/R3, pinned on the FILE rather than on the loader: the six allowed top-level
    keys, `persistence` among them (it is read from git at scrape time), and no entry
    carrying a rail the runtime does not run."""
    assert set(raw_yaml) <= contracts._TOP_LEVEL_KEYS
    assert "persistence" in raw_yaml and raw_yaml["persistence"]["version_cap"] == 20
    for entry in raw_yaml["extractions"]:
        for retired in contracts.RETIRED_ENTRY_KEYS:
            assert retired not in entry, (entry["id"], retired)
        assert entry["locator_kind"] == "graphql", entry["id"]


def test_no_legacy_listings_column_is_read(contract: contracts.PortalContract) -> None:
    """The class-B `listings` columns are not a substrate any more: the lane reads
    `raw_json` and the stored page body, and this portal has no page body."""
    for entry in contract.entries:
        assert entry.surface == "graphql", entry.entry_id
        assert entry.locator.get("reader") not in ("legacy_text_column", "geom_column",
                                                   "coords_stamp_quality")
        assert "legacy_source_column" not in entry.locator, entry.entry_id


def test_the_priors_survive_the_slim(contract: contracts.PortalContract) -> None:
    """R3: `prior:` is the one per-entry block that stays — the resolver reads it until W2
    replaces it. Dropping one silently changes how the claim is resolved, not whether it
    is made, which is the kind of change no other test would see."""
    assert {e.entry_id: e.precision_map.get("prior") for e in contract.entries
            if e.precision_map.get("prior")} == PRIORS


def test_the_coordinate_entry_declares_its_cap_and_bbox_guard(
        contract: contracts.PortalContract) -> None:
    pin = next(e for e in contract.entries if e.claim_type == "coordinate")
    assert pin.precision_map["precision_cap"] == {
        "granularity_max": "address_point", "position_source_max": "portal_pin"}
    assert "reject_outside_cz_bbox" in pin.guards


@pytest.mark.parametrize("entry_id", list(EXPECTED))
def test_each_entry_extracts_its_value_from_the_committed_advert(
        entry_id: str, claims: dict[str, list[Claim]]) -> None:
    """The contract, run by the real readers over the real fixture body."""
    field, expected = EXPECTED[entry_id]
    emitted = claims.get(entry_id, [])
    assert len(emitted) == 1, f"{entry_id} emitted {len(emitted)} claims"
    assert getattr(emitted[0], field) == expected
    assert emitted[0].claim_type == ENTRIES[entry_id]
    assert emitted[0].licence_class == "portal"
    assert emitted[0].blur_evidence == "none"


def test_the_town_extracts_from_the_fixture_body(claims: dict[str, list[Claim]]) -> None:
    """The red-line invariant, at contract grain: this portal's town is readable."""
    town = claims[TOWN_ENTRY_ID][0]
    assert town.claim_type == "obec_name"
    assert town.value_text == "Praha"


def test_a_statutory_city_obvod_never_becomes_the_town() -> None:
    """R4, on the town entry's own transform. RÚIAN has no obec "Praha 8", so a town claim
    carrying the obvod resolves to nothing — a coverage hole that reads as a portal with no
    town. The fold is identity on every real town name."""
    obvod = _by_id(fx.listing("bezrealitky", _live_query_shape(city="Praha 8"),
                              native="1037096"))
    assert obvod[TOWN_ENTRY_ID][0].value_text == "Praha"
    plain = _by_id(fx.listing("bezrealitky", _live_query_shape(city="Frýdek-Místek"),
                              native="1037096"))
    assert plain[TOWN_ENTRY_ID][0].value_text == "Frýdek-Místek"


def test_the_country_is_the_tail_of_the_live_address_line() -> None:
    """The one entry the recon-era shared fixture cannot exercise: it carries the pre-W0
    key `addressUserInput`, while the live query asks for `addressInput`. The claim VALUE is
    the ISO code, never the portal's spelling."""
    live = _by_id(fx.listing("bezrealitky", _live_query_shape(), native="989482"))
    assert live["bzr.det.country"][0].value_text == "CZ"
    assert live["bzr.det.country"][0].claim_type == "country"
    # "foreign is a determination, never a default": a tail that is not a country at all
    # claims nothing rather than defaulting to CZ.
    czechless = _by_id(fx.listing(
        "bezrealitky", _live_query_shape(addressInput="Davídkova 655/31, Libeň, Praha"),
        native="1037096"))
    assert "bzr.det.country" not in czechless
    assert "addressInput" not in fx.BEZREALITKY, (
        "the shared fixture now carries the live key — re-bless bezrealitky@2.json")


def test_the_house_number_pair_is_split_into_two_claims(
        claims: dict[str, list[Claim]]) -> None:
    """'655/31' is a čp/čo PAIR, never two alternatives."""
    assert claims["bzr.det.house_number_cp"][0].value_text == "655"
    assert claims["bzr.det.house_number_co"][0].value_text == "31"
    assert fx.BEZREALITKY["houseNumber"] == "655/31"


def test_the_town_is_never_composed_with_the_city_district(
        claims: dict[str, list[Claim]]) -> None:
    """`_locality()` joins the two and dedupes only on exact equality, which produced
    'Praha - Praha - Libeň' on 41.2% of active rows. The claims stay separate, and the
    část obce keeps the town prefix the portal publishes."""
    assert claims[TOWN_ENTRY_ID][0].value_text == "Praha"
    assert claims["bzr.det.city_district"][0].value_text == "Praha - Libeň"


def test_the_psc_is_normalised_to_five_digits(claims: dict[str, list[Claim]]) -> None:
    """The portal publishes both '19000' and '154 00'; the claim is the normalised form."""
    assert fx.BEZREALITKY["zip"] == "154 00"
    assert claims["bzr.det.zip"][0].value_text == "15400"


def test_the_portals_own_pin_is_claimed_off_the_contracts_pointer() -> None:
    """§6.4's licence rail, on this portal's own pin. The Mapy inventory used to veto it
    row-by-row; with the geocoder gone, `advert.gps{lat,lng}` — the pointer the contract
    names — is first-party by construction and there is nothing left to veto."""
    found = _by_id(fx.listing("bezrealitky", fx.BEZREALITKY, native="1037096"))
    assert "bzr.det.gps" in found
    assert found[TOWN_ENTRY_ID][0].value_text == "Praha"


def test_the_advert_description_is_never_a_claim_substrate(
        contract: contracts.PortalContract, claims: dict[str, list[Claim]]) -> None:
    """The advert's free text is the portal's most PII-dense field (it names people, phone
    numbers and neighbours' addresses) and bezrealitky@1's only `description` entry was the
    LLM transit-distance miner. Nothing reads it now: no entry declares the surface, and no
    claim carries the fixture's description text."""
    assert all(e.surface != "description" for e in contract.entries)
    description = fx.BEZREALITKY["description"]
    for emitted in claims.values():
        for claim in emitted:
            assert claim.value_text != description
            assert claim.evidence_quote is None
