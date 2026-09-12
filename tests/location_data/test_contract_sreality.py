"""sreality@2 — the slim location contract (rule 25), and the portal guarantees it carries.

One entry per claim type over ONE substrate: the v1 estate JSON the detail-drain persists
into `listings.raw_json`. Every entry names a payload reader, so every entry runs on the one
hourly lane; there is no page body for this portal (the SSR detail page 302s into a
login.seznam.cz autologin loop and must not be evaded), so there is no archived-DOM arm.

This file supersedes the sreality half of the W1/W2 portal suites (W1-c R14): it pins the
version, the exact entry-id set, one-entry-per-claim-type, the mandatory town entry and its
reader, one extraction per entry over a committed body, and the two portal rails those
suites carried — the agency-office decoy and the two non-post-cutover payload shapes.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from location_data import contracts
from location_data.claims_intake import READERS, extract_listing, sreality_payload_shape
from tests.location_data import claim_intake_fixtures as fx

CONTRACT = next(c for c in contracts.load_all() if c.source == "sreality")

VERSION = 2
TOWN_ENTRY = "sr.det.name_city"

# 02 §2.1.8: ids are permanent and never reused. Every one of these survives from @1.
ENTRY_IDS = (
    "sr.det.gps",
    "sr.det.inaccuracy_type",
    "sr.det.street",
    "sr.det.housenumber",
    "sr.det.streetnumber",
    "sr.det.zip",
    "sr.det.name_city",
    "sr.det.name_citypart",
    "sr.det.name_district",
    "sr.det.name_region",
    "sr.det.name_country",
)

CLAIM_TYPES = {
    "sr.det.gps": "coordinate",
    "sr.det.inaccuracy_type": "precision_declaration",
    "sr.det.street": "street_name",
    "sr.det.housenumber": "house_number_cp",
    "sr.det.streetnumber": "house_number_co",
    "sr.det.zip": "psc",
    "sr.det.name_city": "obec_name",
    "sr.det.name_citypart": "cast_obce_name",
    "sr.det.name_district": "okres_name",
    "sr.det.name_region": "kraj_name",
    "sr.det.name_country": "country",
}

# The `prior:` blocks @1 declared. They project onto `portal_contract_entries`
# (`default_granularity` / `default_position_source`) and feed the resolver until W2, so
# dropping one is a column going silently NULL rather than a YAML tidy (W1-c R3).
PRIORS = {
    "sr.det.gps": (None, "portal_pin"),
    "sr.det.street": ("street", None),
    "sr.det.name_city": ("obec", None),
    "sr.det.name_citypart": ("cast_obce_or_quarter", None),
}

_ROOT = Path(__file__).resolve().parents[2]
_W2_BODY = _ROOT / "tests" / "fixtures" / "location_w2" / "sreality_detail.json"
_DISK_BODY = (_ROOT / "tests" / "fixtures" / "location_w2" / "regressions" / "sreality"
              / "post_cutover.json")


def entry(entry_id: str) -> contracts.ContractEntry:
    return next(e for e in CONTRACT.entries if e.entry_id == entry_id)


def claims(raw: dict[str, Any], lat: float | None = None, lon: float | None = None):
    row = fx.listing("sreality", raw, native="probe", lat=lat, lon=lon)
    result = extract_listing(row, fx.entries_for("sreality"))
    return {c.extractor_id: c for c in result.claims}, result


POST_CUTOVER, POST_CUTOVER_RESULT = claims(
    fx.SREALITY_POST_CUTOVER, lat=50.0784977, lon=14.4501973)
W2_FIXTURE, _ = claims(json.loads(_W2_BODY.read_text(encoding="utf-8")),
                       lat=50.0776, lon=14.4394)


# ------------------------------------------------------------------ contract shape

def test_the_contract_is_at_version_2() -> None:
    assert CONTRACT.version == VERSION


def test_the_entry_id_set_is_exactly_the_slim_eleven_in_order() -> None:
    """Order is asserted, not just membership: the deploy projection assigns
    `contract_entry_id` by file position (02 §2.1.8 forbids reordering)."""
    assert tuple(e.entry_id for e in CONTRACT.entries) == ENTRY_IDS


def test_each_claim_type_is_declared_exactly_once_and_the_set_is_the_vocabulary() -> None:
    """Rule 25: one entry per claim type. sreality is the portal that carries all eleven —
    a twelfth type, or a second entry for one, is what the slim store exists to forbid."""
    counts = Counter(e.claim_type for e in CONTRACT.entries)
    assert [t for t, n in counts.items() if n > 1] == []
    assert {e.entry_id: e.claim_type for e in CONTRACT.entries} == CLAIM_TYPES
    assert set(counts) == set(contracts.CLAIM_TYPES)


def test_the_town_entry_is_present_and_names_a_payload_reader() -> None:
    """The mandatory entry. Readerless means it never runs, and an entry that never runs
    cannot satisfy 'every active Czech listing has a town'."""
    town = entry(TOWN_ENTRY)
    assert town.claim_type == contracts.MANDATORY_CLAIM_TYPE == "obec_name"
    assert town.locator["reader"] == "scalar"
    assert town.locator["json_pointer"] == "/locality/city"
    assert town.locator["reader"] in READERS


def test_every_entry_runs_on_the_one_payload_lane() -> None:
    """No page substrate on this portal, and no `listings` column: one lane, one body."""
    for e in CONTRACT.entries:
        assert e.locator["reader"] in READERS, e.entry_id
        assert e.surface == "api_json", e.entry_id
        assert e.page_kind == "detail", e.entry_id
        assert "legacy_source_column" not in e.locator, e.entry_id


def test_no_entry_declares_a_transform_or_guard_the_runtime_would_ignore() -> None:
    """An unimplemented name, or one on a reader that never consults the axis, rejects and
    normalises nothing in silence — @1 carried two such guards and this is what replaced
    them (W1-c R3)."""
    for e in CONTRACT.entries:
        spec = contracts.READER_CONTRACTS[str(e.locator["reader"])]
        for declared in e.transform:
            assert declared.partition(":")[0] in contracts.IMPLEMENTED_TRANSFORMS
            assert spec.consults_transforms, e.entry_id
        for guard in e.guards:
            assert guard in contracts.IMPLEMENTED_GUARDS
            assert spec.consults_guards, e.entry_id
    assert entry("sr.det.gps").guards == ["reject_outside_cz_bbox"]


def test_the_priors_that_project_onto_the_entry_row_survive_the_slim_rewrite() -> None:
    """@1 stated these; `parse_entry` maps them onto columns migration 382 created and the
    resolver reads until W2. A dropped `prior:` is a NULL nobody would see."""
    for entry_id, (granularity, position_source) in PRIORS.items():
        assert entry(entry_id).default_granularity == granularity, entry_id
        assert entry(entry_id).default_position_source == position_source, entry_id
    assert {e.entry_id for e in CONTRACT.entries
            if e.default_granularity or e.default_position_source} == set(PRIORS)


def test_the_pin_declares_a_cap_that_blurs_on_every_imprecise_label() -> None:
    """The loader demands a cap on a coordinate; @1 kept the ladder in a top-level
    `precision_caps:` block the slim format no longer allows, so the pin states it."""
    cap = entry("sr.det.gps").precision_map["precision_cap"]
    assert cap["granularity_max"] == "per_inaccuracy_type"
    assert cap["position_source_max"] == {
        "gps": "portal_pin", "address": "portal_pin", "_default": "portal_pin_blurred"}


def test_the_precision_entry_carries_the_blurred_label_set() -> None:
    """`blurred_labels` is the calibration set the reader types the blur axis by, so it is
    contract data — re-calibrating it is a version bump, not a code change."""
    precision = entry("sr.det.inaccuracy_type")
    assert precision.precision_map["blurred_labels"] == [
        "street", "ward", "quarter", "municipality"]
    assert precision.locator["json_pointer"] == "/locality/inaccuracy_type"
    assert precision.precision_map["precision_cap"]["granularity_max"]["municipality"] == \
        "obec"


def test_the_archive_profile_is_the_one_the_scraper_reads_at_runtime() -> None:
    """`persistence.volatile_paths` is the ONE key read from git at scrape time
    (`payload_norm`), so it is carried verbatim across the version bump rather than
    re-derived — an edit here moves `payload_sha256` for every sreality body."""
    profile = CONTRACT.volatile_profiles["detail"]
    assert profile.css_selectors == () and profile.strip_attributes == ()
    assert {"/stats", "/edited", "/advert_images/-/url", "/sdn_*_attachment_url",
            "/_embedded/note"} <= set(profile.json_pointers)
    assert CONTRACT.fetch_config["persistence"]["version_cap"] == 20


def test_the_pinned_regressions_still_stand() -> None:
    assert [str(line).split(" —")[0]
            for line in CONTRACT.fetch_config["regressions"]] == [
        "520268", "1588965452", "3067969612"]


def test_the_agency_office_zone_is_still_excluded() -> None:
    pointers = {z["locator"].get("json_pointer") for z in CONTRACT.exclusion_zones}
    assert {"/premise", "/labels_extended"} <= pointers


# ------------------------------------------------ one extraction per entry, per fixture

@pytest.mark.parametrize("entry_id,claim_type,value", [
    ("sr.det.gps", "coordinate", "POINT(14.4501973 50.0784977)"),
    ("sr.det.inaccuracy_type", "precision_declaration", "street"),
    ("sr.det.street", "street_name", "náměstí Jiřího z Poděbrad"),
    ("sr.det.housenumber", "house_number_cp", "1558"),
    ("sr.det.streetnumber", "house_number_co", "7"),
    ("sr.det.zip", "psc", "13000"),
    ("sr.det.name_city", "obec_name", "Praha"),
    ("sr.det.name_citypart", "cast_obce_name", "Vinohrady"),
    ("sr.det.name_district", "okres_name", "Praha 3"),
    ("sr.det.name_region", "kraj_name", "Hlavní město Praha"),
    ("sr.det.name_country", "country", "Česká republika"),
])
def test_every_entry_extracts_from_the_post_cutover_body(
        entry_id: str, claim_type: str, value: str) -> None:
    """All eleven, over one committed body — an entry that claims nothing on every fixture
    is a hole the contract cannot see (02 §2.1.2)."""
    claim = POST_CUTOVER[entry_id]
    assert claim.claim_type == claim_type
    assert (claim.value_geom_wkt if claim_type == "coordinate"
            else claim.value_text) == value


def test_the_declared_precision_is_typed_on_the_blur_axis() -> None:
    """`street` names a blurred class -> declared; `address` names a precise one -> none,
    written EXPLICITLY either way (06 §6.6 rule 7). The label is the value, per W1-c R5."""
    precision = POST_CUTOVER["sr.det.inaccuracy_type"]
    assert (precision.value_text, precision.declared_precision_label) == ("street", "street")
    assert precision.blur_evidence == "declared"
    assert W2_FIXTURE["sr.det.inaccuracy_type"].declared_precision_label == "address"
    assert W2_FIXTURE["sr.det.inaccuracy_type"].blur_evidence == "none"


def test_the_town_extracts_from_the_second_committed_body_too() -> None:
    """`tests/fixtures/location_w2/sreality_detail.json`, the hand-written W2 body. It
    spells the house number `/locality/house_number` (one glued čp/čo string) rather than
    the API's `housenumber` + `streetnumber` pair, so those two entries stay silent on it —
    the town, which is what rule 25 measures, does not."""
    assert W2_FIXTURE["sr.det.name_city"].value_text == "Praha"
    assert W2_FIXTURE["sr.det.name_citypart"].value_text == "Vinohrady"
    assert W2_FIXTURE["sr.det.street"].value_text == "Vinohradská"
    assert W2_FIXTURE["sr.det.zip"].value_text == "12000"
    assert "sr.det.housenumber" not in W2_FIXTURE


def test_the_town_survives_a_row_that_has_nothing_else() -> None:
    """The zip:-1 sentinel row (regression 3067969612): street/citypart/region absent and
    the sentinel dropped — and the town still lands, which is the invariant."""
    by_id, _ = claims(fx.SREALITY_ZIP_SENTINEL, lat=49.3955, lon=13.2951)
    assert by_id["sr.det.name_city"].value_text == "Klatovy"
    assert "sr.det.zip" not in by_id


def test_a_numbered_obvod_in_the_city_field_is_folded_to_the_city() -> None:
    """W1-c R4: a numbered městský obvod is never the town. `/locality/city` is already the
    obec on every committed body, so `statutory_city_obec` is a RAIL on the mandatory claim
    — it is a no-op on a real obec name and the difference only shows if the portal ever
    publishes the obvod where the town belongs."""
    obvod = copy.deepcopy(fx.SREALITY_POST_CUTOVER)
    obvod["locality"]["city"] = "Praha 8"
    by_id, _ = claims(obvod, lat=50.0784977, lon=14.4501973)
    assert by_id["sr.det.name_city"].value_text == "Praha"
    assert POST_CUTOVER["sr.det.name_city"].value_text == "Praha"


# ---------------------------------------------------- the portal rails, R14: kept here

def test_the_agency_office_never_becomes_a_claim() -> None:
    """`premise` is the ESTATE AGENCY's own office — name, address, phone and its own
    gps pair — a fully-formed decoy in 11 of 12 mined files, and third-party personal data
    this lane has no business storing. No entry addresses it and the exclusion zone says
    so; this asserts the OUTCOME on a body that carries one."""
    office = fx.SREALITY_POST_CUTOVER["premise"]["locality"]
    values = {c.value_text for c in POST_CUTOVER.values()}
    points = {c.value_geom_wkt for c in POST_CUTOVER.values()}
    assert office["street"] not in values
    assert office["housenumber"] not in values
    assert f"POINT({office['gps_lon']!r} {office['gps_lat']!r})" not in points
    assert all(not str(e.locator.get("json_pointer", "")).startswith("/premise")
               for e in CONTRACT.entries)


def test_a_legacy_shape_row_claims_nothing_and_says_so() -> None:
    """The pre-cutover payload carries no `/locality/*` structured field at all — a display
    string plus a coarse `accuracy` flag — and the slim vocabulary has no
    `address_line_verbatim`, so it yields zero claims (W1-c R11: accepted; the recovery is
    a detail refetch by the scraper, not a contract entry). The shape is REFUSED by name so
    the cohort stays visible in the run log rather than reading as 'portal published
    nothing'."""
    assert sreality_payload_shape(fx.SREALITY_LEGACY) == "legacy"
    by_id, result = claims(fx.SREALITY_LEGACY, lat=49.3955, lon=13.2951)
    assert by_id == {}
    assert dict(result.refusals) == {"sreality_payload_shape:legacy": 1}


def test_a_truncated_payload_claims_nothing_and_says_so() -> None:
    """1588965452: an 80 KB geometry blob truncated raw_json and destroyed the locality
    object. `absent`, not `legacy` — a different cohort with a different recovery."""
    assert sreality_payload_shape(fx.SREALITY_TRUNCATED) == "absent"
    by_id, result = claims(fx.SREALITY_TRUNCATED, lat=50.0, lon=14.0)
    assert by_id == {}
    assert dict(result.refusals) == {"sreality_payload_shape:absent": 1}


def test_no_dropped_at_1_entry_still_extracts() -> None:
    """@2 drops seven executable @1 entries (the six portal `*_id`s and `street_id`, the
    bbox, the quarter/ward names, `entity_type`, the legacy display string). None of their
    ids may reappear in a claim, and no claim may carry a type the slim vocabulary
    retired."""
    dropped = {"sr.det.entity_type", "sr.det.geometry", "sr.det.street_id",
               "sr.det.name_quarter", "sr.det.name_ward", "sr.det.municipality_id",
               "sr.det.quarter_id", "sr.det.ward_id", "sr.det.district_id",
               "sr.det.region_id", "sr.det.country_id", "sr.det.legacy_locality_value",
               "sr.idx.gps", "sr.idx.geohash", "sr.idx.poi_distance",
               "sr.det.labels_extended", "sr.det.micro_position", "sr.det.seo_names"}
    assert dropped.isdisjoint(POST_CUTOVER)
    assert {c.claim_type for c in POST_CUTOVER_RESULT.claims} <= contracts.CLAIM_TYPES


def test_the_disk_regression_body_is_the_python_fixture_byte_for_byte() -> None:
    """`tests/fixtures/location_w2/regressions/sreality/post_cutover.json` is the golden's
    only sreality body with a locality object, and it is a SECOND copy of
    `claim_intake_fixtures.SREALITY_POST_CUTOVER`. Bound here so the two cannot drift in
    silence. Its key is the payload SHAPE, not a listing id: no real sreality listing in
    this repo is tied to committed bytes, and inventing one would report coverage of a
    pinned regression the gate does not have."""
    doc = json.loads(_DISK_BODY.read_text(encoding="utf-8"))
    assert doc["raw_json"] == fx.SREALITY_POST_CUTOVER
    assert (doc["lat"], doc["lon"]) == (50.0784977, 14.4501973)
    assert doc["_note"].startswith("SYNTHESISED, not captured")
