"""mmreality@3 — the slim contract, executed over every committed fixture of this portal.

One entry per claim type, the town entry (`mm.det.municipality`) mandatory. All seven
entries read the Vue `:property` blob, which `scraper.mmreality_parser.extract_property`
mirrors into `listings.raw_json` — so all seven run on the payload half of the hourly lane
and none of them depends on R2 holding a body.

The stored page body carries the SAME blob, so the town is proven on both substrates here:
`test_the_town_extracts_from_every_committed_fixture` runs the town entry over the three
committed payload rows AND over the five committed bodies, each body resolved to its
subject blob exactly as the scraper resolves it before it writes `raw_json`.

Carried forward from the deleted `test_location_w2_mmreality.py` (mmreality@2's archived
suite): the subject-selection guarantee (the larger neighbour blob is never read for the
subject, and a body with no blob for the listing raises rather than substituting) and the
Mapy-inventory veto on the portal's coordinate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from location_data import contracts
from location_data.claims_intake import (
    ARCHIVED_COORDINATE_RULES,
    COORDINATE_RULES,
    DEFAULT_MAX_CLAIM_VALUE_BYTES,
    READERS,
    coordinate_verdict,
    extract_listing,
)
from location_data.claims_common import SUBSTRATE_PAYLOAD
from location_data.resolver.bind import (
    BLURRED_DECLARED_LABELS,
    PRECISE_DECLARED_LABELS,
)
from location_data.resolver.grade import DECLARED_CAP
from scraper.mmreality_parser import PropertyMismatch, extract_property
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
_REGRESSION_ROW = (_ROOT / "tests" / "fixtures" / "location_w2" / "regressions"
                   / "mmreality" / "951845.json")
_PINNED_BODY = _ROOT / "tests" / "fixtures" / "location_w2" / "mmreality_detail.html"
_ARCHIVED_BODY = _ROOT / "tests" / "fixtures" / "portal_html" / "mmreality_detail.html"
_REFETCH = _ROOT / "tests" / "fixtures" / "location_w2a_refetch"

CONTRACT = {c.source: c for c in contracts.load_all()}["mmreality"]
BY_ID = {e.entry_id: e for e in CONTRACT.entries}

# The whole contract, enumerated. A derived set goes green on an empty contract.
ENTRY_IDS = {
    "mm.det.point",
    "mm.det.accurate",
    "mm.det.country",
    "mm.det.district",
    "mm.det.municipality",
    "mm.det.municipality_part",
    "mm.det.street",
}
TOWN_ENTRY = "mm.det.municipality"
TOWN_READER = "scalar"
TOWN_POINTER = "/municipality"

# The eleven types rule 25 (W1-c R1) leaves a contract. `kraj_name`, `psc` and both house
# numbers are absent from this portal because the blob publishes none of them — see
# `test_the_blob_publishes_no_psc_no_kraj_and_no_house_number`.
SLIM_CLAIM_TYPES = {
    "coordinate", "precision_declaration", "country", "kraj_name", "okres_name",
    "obec_name", "cast_obce_name", "street_name", "house_number_cp", "house_number_co",
    "psc",
}

# The label the contract maps `accurate: false` to. It is the RESOLVER's vocabulary on
# purpose — see `test_the_blurred_label_is_a_key_the_resolver_actually_caps_on`.
BLURRED_LABEL = "regional"
PRECISE_LABEL = "accurate"


def claims(raw_json: dict, native: str) -> dict:
    """One payload row through the real hourly extractor, keyed by extractor id."""
    row = fx.listing("mmreality", raw_json, native=native)
    result = extract_listing(row, fx.entries_for("mmreality"),
                             max_value_bytes=DEFAULT_MAX_CLAIM_VALUE_BYTES)
    found: dict = {}
    for claim in result.claims:
        assert claim.extractor_id not in found, f"{claim.extractor_id} twice"
        found[claim.extractor_id] = claim
    return found


def from_body(path: Path, native: str) -> dict:
    """The contract over a STORED PAGE BODY, with the subject blob selected the way the
    scraper selects it before it writes `raw_json` (`extract_property`, id-matched)."""
    blob = dict(extract_property(path.read_text(encoding="utf-8", errors="replace"),
                                 native))
    point = blob.get("point") or {}
    return claims(blob, native)


def regression_row() -> dict:
    return json.loads(_REGRESSION_ROW.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ the shape

def test_mmreality_ships_at_version_three():
    assert CONTRACT.version == 3


def test_the_entry_ids_are_exactly_these_seven():
    assert set(BY_ID) == ENTRY_IDS


def test_there_is_at_most_one_entry_per_claim_type_and_all_are_in_the_vocabulary():
    types = [e.claim_type for e in CONTRACT.entries]
    assert len(types) == len(set(types)), f"duplicate claim type in {types}"
    assert set(types) <= SLIM_CLAIM_TYPES, set(types) - SLIM_CLAIM_TYPES


def test_every_entry_names_a_registered_reader_on_the_payload_substrate():
    """Every entry is executed on every listing of this portal, so a readerless entry (or
    one parked on a substrate this contract does not read) claims nothing forever. The
    `listings` columns are not a substrate any more — W1-c deleted those readers."""
    for entry in CONTRACT.entries:
        assert entry.reader in READERS, entry.entry_id
        assert READERS[entry.reader].substrate == SUBSTRATE_PAYLOAD, entry.entry_id
        assert entry.surface != "legacy_column", entry.entry_id
        assert entry.extraction_method != "legacy_column", entry.entry_id
        assert "legacy_source_column" not in entry.locator, entry.entry_id


def test_the_town_entry_is_present_and_names_its_reader():
    """The invariant behind rule 25: every active Czech listing has a town, so the
    `obec_name` entry is mandatory, executable, and on the substrate that is populated for
    100% of this portal's active rows without R2 in the path."""
    town = BY_ID[TOWN_ENTRY]
    assert town.claim_type == "obec_name"
    assert town.reader == TOWN_READER
    assert town.locator["json_pointer"] == TOWN_POINTER
    assert READERS[town.reader].substrate == SUBSTRATE_PAYLOAD
    assert town.default_granularity == "obec"


def test_the_coordinate_carries_its_cap_and_the_declaration_carries_its_blurred_labels():
    cap = BY_ID["mm.det.point"].precision_map["precision_cap"]
    assert cap["granularity_max"] == {PRECISE_LABEL: "address_point",
                                      BLURRED_LABEL: "obec"}
    assert cap["position_source_max"] == {PRECISE_LABEL: "portal_pin",
                                          BLURRED_LABEL: "portal_pin_blurred"}
    assert BY_ID["mm.det.point"].guards == ["reject_outside_cz_bbox"]
    assert BY_ID["mm.det.accurate"].precision_map["blurred_labels"] == [BLURRED_LABEL]
    assert BY_ID["mm.det.accurate"].locator["labels"] == {
        "true": PRECISE_LABEL, "false": BLURRED_LABEL}


def test_the_blurred_label_is_a_key_the_resolver_actually_caps_on():
    """@1/@2 spelled the `accurate: false` label `not_accurate`, which is not a key in
    `DECLARED_CAP` — so the obec ceiling the YAML documented was never applied, and since
    2026-09-11 an unmapped blurred label takes the generic `blur_hint -> street` fallback,
    i.e. LOOSER than the cap the file claimed. `regional` is the resolver's own key for
    exactly this ceiling, so the declaration and the resolver now say the same thing."""
    assert DECLARED_CAP[BLURRED_LABEL] == "obec"
    assert BLURRED_LABEL in BLURRED_DECLARED_LABELS
    # The `true` arm names the pin PRECISE without CAPPING it, and the two are different
    # questions. `PRECISE_DECLARED_LABELS` decides which of two sibling declarations wins
    # (`declared_for_coordinate` ranks it 0), which is exactly what a portal flag is for;
    # a `DECLARED_CAP` row would additionally certify a granularity, and `accurate` does
    # not predict correctness on this portal well enough to certify one. Unmapped there is
    # identical to the `address_point` ceiling the entry documents — address_point is the
    # finest rung, so capping at it coarsens nothing.
    assert PRECISE_LABEL in PRECISE_DECLARED_LABELS
    assert PRECISE_LABEL not in DECLARED_CAP
    assert PRECISE_LABEL not in BLURRED_DECLARED_LABELS


def test_the_coordinate_entry_keeps_the_id_the_licence_ladder_names():
    """`ARCHIVED_COORDINATE_RULES['mmreality']` names ONE entry as the portal's only
    licensable ARCHIVED coordinate locator. @3 reads the pin off the payload, so that row
    is dormant — but renaming the entry would silently unlicense the archived read the
    moment anything re-points at it, which is why the id survived the bump."""
    assert ARCHIVED_COORDINATE_RULES["mmreality"].entry_id == "mm.det.point"
    assert BY_ID["mm.det.point"].reader == "point_pair"


def test_no_entry_declares_a_subject_match_the_reader_would_ignore():
    """The payload readers never consult `locator.match` or `subject_scope`, so declaring
    one would be a rail the contract names and the runtime ignores. Subject correctness on
    this portal is the PARSER's id match (`extract_property` / `PropertyMismatch`),
    asserted below over real bodies."""
    for entry in CONTRACT.entries:
        assert "match" not in entry.locator, entry.entry_id
        assert entry.subject_scope == {}, entry.entry_id


def test_the_persistence_block_still_strips_the_rotating_footer_address():
    """Kept verbatim from the shipped file because it is read at SCRAPE time: the
    Cloudflare edge re-encodes the footer mailto under a per-response key, so a body whose
    two selectors are not stripped changes on every fetch and `payload_sha256` — a
    permanent content address — moves for a constant."""
    profile = CONTRACT.volatile_profiles["detail"]
    assert 'a[href^="/cdn-cgi/l/email-protection"]' in profile.css_selectors
    assert "span.__cf_email__" in profile.css_selectors


def test_the_exclusion_zones_that_keep_other_listings_out_are_still_declared():
    """Three of the four are other listings' addresses (the non-subject `:property` blobs,
    the `:locations` neighbour list, the 'Podobné nemovitosti v okolí' block); the fourth
    is the site-nav 'Zahraniční nemovitosti' item, which is on 100% of this portal's pages
    and would otherwise type every listing foreign."""
    declared = [z["locator"].get("css") for z in CONTRACT.exclusion_zones]
    assert "[\\:property]" in declared
    assert "[\\:locations]" in declared
    assert ".similar, .podobne" in declared
    assert "nav a[href*='zahranicni']" in declared


# ------------------------------------- extraction: every entry, over a committed fixture

def test_the_pinned_regression_row_yields_six_of_the_seven_entries_including_the_town():
    """`tests/fixtures/location_w2/regressions/mmreality/951845.json` — the row the
    fixture-diff gate scores. Non-vacuity: everything but the street must fire, and 951845
    genuinely publishes no `/street` key."""
    doc = regression_row()
    found = claims(doc["raw_json"], "951845")

    assert set(found) == ENTRY_IDS - {"mm.det.street"}

    assert found[TOWN_ENTRY].claim_type == "obec_name"
    assert found[TOWN_ENTRY].value_text == "Andělská Hora"

    assert found["mm.det.point"].value_geom_wkt == "POINT(17.389086312 50.060813844)"
    assert found["mm.det.point"].licence_class == "portal"
    assert found["mm.det.accurate"].declared_precision_label == PRECISE_LABEL
    assert found["mm.det.accurate"].blur_evidence == "none"
    assert found["mm.det.country"].value_text == "Česká republika"
    assert found["mm.det.district"].value_text == "Bruntál"
    assert found["mm.det.municipality_part"].value_text == "Andělská Hora"


def test_the_kolin_payload_row_fires_all_seven_and_separates_the_admin_names():
    """`claim_intake_fixtures.MMREALITY_ACCURATE` — the row where obec, okres and část
    obce genuinely differ, so a reader crossed onto the wrong pointer cannot pass, and the
    one committed payload that carries `/street`."""
    found = claims(fx.MMREALITY_ACCURATE, "123456")

    assert set(found) == ENTRY_IDS
    assert found[TOWN_ENTRY].value_text == "Kolín"
    assert found["mm.det.district"].value_text == "Kolín"
    assert found["mm.det.municipality_part"].value_text == "Kolín I"
    assert found["mm.det.street"].value_text == "Kutnohorská"
    assert found["mm.det.street"].claim_type == "street_name"
    assert found["mm.det.point"].value_geom_wkt == "POINT(15.7712123456 50.0296123456)"


def test_accurate_false_declares_blur_and_a_null_part_claims_nothing():
    """`regional` is the blurred label the contract calibrates, and the reader writes the
    blur axis explicitly; `municipalityPart` is null on this row, and a null is an honest
    miss rather than an empty claim."""
    found = claims(fx.MMREALITY_NOT_ACCURATE, "654321")

    assert found["mm.det.accurate"].declared_precision_label == BLURRED_LABEL
    assert found["mm.det.accurate"].value_text == BLURRED_LABEL
    assert found["mm.det.accurate"].blur_evidence == "declared"
    assert found[TOWN_ENTRY].value_text == "Bochov"
    assert "mm.det.municipality_part" not in found


# ------------------------------------------ the town, on every committed fixture

# Every mmreality fixture this repo commits, with the town each one states. The payload
# rows are read as the hourly lane reads them; each body is resolved to its subject blob
# the way the scraper resolves it before writing `raw_json` — the same act, one step
# earlier. A row-and-body pair for one listing (951845) appears in both halves.
TOWN_FIXTURES = [
    ("tests/fixtures/location_w2/regressions/mmreality/951845.json", "951845",
     "Andělská Hora"),
    ("claim_intake_fixtures.MMREALITY_ACCURATE", "123456", "Kolín"),
    ("claim_intake_fixtures.MMREALITY_NOT_ACCURATE", "654321", "Bochov"),
    ("tests/fixtures/location_w2/mmreality_detail.html", "fixture", "Praha"),
    ("tests/fixtures/portal_html/mmreality_detail.html", "951845", "Andělská Hora"),
    ("tests/fixtures/location_w2a_refetch/mmreality_a1.html", "951726", "Bělčice"),
    ("tests/fixtures/location_w2a_refetch/mmreality_a2.html", "951726", "Bělčice"),
    ("tests/fixtures/location_w2a_refetch/mmreality_b1.html", "951734", "Karviná"),
]


@pytest.mark.parametrize("fixture,native,town", TOWN_FIXTURES,
                         ids=[f[0].rsplit("/", 1)[-1] + ":" + f[1]
                              for f in TOWN_FIXTURES])
def test_the_town_extracts_from_every_committed_fixture(fixture: str, native: str,
                                                        town: str) -> None:
    """Rule 25's town-coverage invariant, pinned per fixture: this portal publishes a town
    on every committed row and every committed body, so a fixture with no town claim is a
    contract defect, never a fixture without a town."""
    if fixture.endswith(".html"):
        found = from_body(_ROOT / fixture, native)
    elif fixture.startswith("claim_intake_fixtures."):
        found = claims(getattr(fx, fixture.split(".", 1)[1]), native)
    else:
        doc = json.loads((_ROOT / fixture).read_text(encoding="utf-8"))
        found = claims(doc["raw_json"], native)
    assert TOWN_ENTRY in found, f"{fixture}: the mandatory town entry claimed nothing"
    assert found[TOWN_ENTRY].value_text == town


# ------------------------------------------------------------ the payload pin, licensed

def test_the_payload_pin_is_licensed_by_the_contracts_pointer_not_by_a_stamp():
    """mmreality's rule is `payload`: the value is re-derived from the JSON pointer the
    contract names, so no `coords.source` stamp is consulted and there is nothing left to
    veto row-by-row — the Mapy inventory that used to sit above this rung is gone (W4-b).
    mmreality@2 had parked the entry on the archived lane, where the hourly pass emitted no
    coordinate at all; reading the pin off the payload is what put it back."""
    doc = regression_row()
    found = claims(doc["raw_json"], "951845")
    assert "mm.det.point" in found
    assert COORDINATE_RULES["mmreality"].substrate == "payload"
    assert coordinate_verdict("mmreality", "geocode").admitted is True
    # The admin claims ride the same pass.
    assert found[TOWN_ENTRY].value_text == "Andělská Hora"


# ------------------------------------- subject selection, over the real stored bodies

def test_the_contract_reads_the_id_matched_blob_out_of_a_real_stored_body():
    """`mmreality_b1.html` is a real refetch carrying the subject (951734) plus four
    neighbour cards with their own towns, streets and pins. Scored under a neighbour's id
    the contract returns THAT card — the proof that the blob is chosen by `/id` and never
    by document order or serialized length."""
    subject = from_body(_REFETCH / "mmreality_b1.html", "951734")
    assert set(subject) == ENTRY_IDS
    assert subject[TOWN_ENTRY].value_text == "Karviná"
    assert subject["mm.det.municipality_part"].value_text == "Ráj"
    assert subject["mm.det.street"].value_text == "Olbrachtova"
    assert subject["mm.det.point"].value_geom_wkt == "POINT(18.562308193 49.852609493)"

    neighbour = from_body(_REFETCH / "mmreality_b1.html", "945490")
    assert neighbour["mm.det.street"].value_text == "Ve Svahu"
    assert neighbour["mm.det.accurate"].declared_precision_label == BLURRED_LABEL


def test_the_larger_neighbour_blob_is_never_read_for_the_subject():
    """`tests/fixtures/portal_html/mmreality_detail.html`: three `:property` blobs, and the
    NEIGHBOUR's (950647, Ludvíkov, 23,656 chars) is LARGER than the subject's (13,827).
    The removed largest-blob fallback returned Ludvíkov's obec and pin for an Andělská Hora
    listing; a caller WITH an id can no longer reach it."""
    found = from_body(_ARCHIVED_BODY, "951845")
    assert found[TOWN_ENTRY].value_text == "Andělská Hora"
    assert found["mm.det.point"].value_geom_wkt == "POINT(17.389086312 50.060813844)"
    seen = " ".join(f"{c.value_text} {c.value_geom_wkt}" for c in found.values())
    for decoy in ("Ludvíkov", "17.347457655", "50.113874456"):
        assert decoy not in seen, decoy


def test_the_pinned_fixture_body_keeps_its_longer_decoy_blob():
    """`tests/fixtures/location_w2/mmreality_detail.html` exists to make the size fallback
    visible: its subject blob (`"id":"fixture"`) is SHORTER than the neighbour card's, and
    nothing the contract claims for the subject may carry Kladno's values."""
    html = _PINNED_BODY.read_text(encoding="utf-8", errors="replace")
    subject, decoy = html.split(':property="')[1:3]
    assert len(decoy) > len(subject)
    found = from_body(_PINNED_BODY, "fixture")
    assert found[TOWN_ENTRY].value_text == "Praha"
    assert found["mm.det.municipality_part"].value_text == "Karlín"
    seen = " ".join(f"{c.value_text} {c.value_geom_wkt}" for c in found.values())
    for decoy_value in ("Kladno", "Sokolovská", "14.10245"):
        assert decoy_value not in seen, decoy_value


def test_a_body_that_carries_no_blob_for_the_listing_raises_rather_than_substituting():
    """The rail the payload lane inherits instead of `subject_scope: {on_miss: fail}`:
    since 2026-09-07 a removed listing's page of substitute cards raises here rather than
    overwriting the row with a preview card."""
    html = (_REFETCH / "mmreality_b1.html").read_text(encoding="utf-8", errors="replace")
    with pytest.raises(PropertyMismatch):
        extract_property(html, "999999")


def test_the_blob_publishes_no_psc_no_kraj_and_no_house_number():
    """Why the contract has no `psc`, no `kraj_name` and no house-number entry: the portal
    publishes none of them. `/district` carries the OKRES name (and, for Prague, 'Hlavní
    město Praha' — the city's own okres-less label, not a kraj)."""
    for path, native in (("tests/fixtures/portal_html/mmreality_detail.html", "951845"),
                         ("tests/fixtures/location_w2a_refetch/mmreality_b1.html",
                          "951734")):
        blob = extract_property(
            (_ROOT / path).read_text(encoding="utf-8", errors="replace"), native)
        keys = {k.lower() for k in blob}
        assert not {k for k in keys if "psc" in k or "zip" in k or "postal" in k}, path
        assert not {k for k in keys if "region" in k or "kraj" in k}, path
        assert not {k for k in keys if "house" in k or "descriptive" in k}, path


# --------------------------------------------------------------- what was dropped

@pytest.mark.parametrize("claim_type", ["obec_code", "portal_admin_id", "poi_distance",
                                        "micro_position", "neighbour_listing_ref",
                                        "cadastral_territory_name"])
def test_the_types_outside_the_slim_vocabulary_are_gone(claim_type: str) -> None:
    """mmreality@2 carried all six. They are not deferred, they are deleted: the slim
    store has no field for any of them."""
    assert claim_type not in {e.claim_type for e in CONTRACT.entries}
