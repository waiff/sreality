"""maxima@3 — the slim contract, exercised through the SHIPPED YAML rather than a mock.

Every entry here is read off `contracts/portals/maxima.yaml` and run by the real page lane
(`extract_page`, including the C6 licence ladder), so a locator edit that stops matching
fails here rather than mining zero claims in production for a month.

maxima is a BODY portal at @3: all six entries read the stored detail page and none reads
`listings.raw_json`, so the payload lane mints nothing for this source by construction.

Two substrates, both real:
  * `tests/fixtures/location_w2/maxima_detail.html` — the pinned body the fixture-diff gate
    scores. A Point feature, a description block, and recon §3.2's three-segment locality
    shape ("městský obvod, katastrální území/čtvrť, street") whose obvod the town entry has
    to fold and whose tail is a real street.
  * LIVE captures fetched 2026-09-05 (HTTP 200, one request each, 2 s apart) from
    `https://nemovitosti.maxima.cz/nemovitosti/<id>/`, whose `JSON.parse('…')` argument is
    reproduced BYTE-FOR-BYTE — escapes intact — because the escaping is the thing under
    test. The locality lines and the okres sentences are verbatim from the same corpus.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scraper import street

from location_data import claims_common, claims_intake, contracts
from location_data.claims_intake import Entry, IntakeRefused
from location_data.page_readers import PAGE_READERS, ArchivedPayload, extract_page
from location_data.html_scope import ScopeRegister, scope_html
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
_PINNED = _ROOT / "tests" / "fixtures" / "location_w2" / "maxima_detail.html"
_BODIES = _ROOT / "tests" / "fixtures" / "location_w2" / "regressions" / "maxima"

FETCHED_AT = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)
CONTRACT = {c.source: c for c in contracts.load_all()}["maxima"]
ENTRIES = fx.entries_for("maxima")
BY_ID = {entry.entry_id: entry for entry in ENTRIES}
REGISTER = ScopeRegister.from_zones("maxima", CONTRACT.exclusion_zones)

ENTRY_IDS = {
    "mx.det.map_features", "mx.det.map_geometry", "mx.det.locality_obec",
    "mx.det.locality_quarter", "mx.det.locality_street", "mx.det.description_okres",
}

# --- the live captures, verbatim ------------------------------------------------------
# `\"` throughout: maxima serves the config as a JS single-quoted string literal, which is
# the whole reason `decode: js_string` exists on the two map entries.
LIVE_TWO_FEATURES = (
    '{\\"center\\":[15.259603745235422,50.36339611563554],'
    '\\"zoom\\":14.379884735764898,'
    '\\"features\\":[{\\"type\\":\\"Point\\",'
    '\\"coordinates\\":[15.271186828990166,50.370363263565366]},'
    '{\\"type\\":\\"Circle\\",\\"center\\":[15.265555606887938,50.360492682585914],'
    '\\"radius\\":0.0037507203377007414}]}'
)
LIVE_CIRCLE = (
    '{\\"center\\":[15.266305699712364,50.35640416157747],'
    '\\"zoom\\":13.780432630830754,'
    '\\"features\\":[{\\"type\\":\\"Circle\\",'
    '\\"center\\":[15.265521169300019,50.360596371862215],'
    '\\"radius\\":0.0036644622101906776}]}'
)
# a10070727 (recon §3.4): Praha 3, Žižkov, Jeseniova — a 2-point ~700 m LineString at
# zoom 15.679. Two of the five live pages on record draw one, and `json_point` returns
# nothing for a LineString, which is the reason this entry reads `json_geometry`.
LIVE_LINESTRING = (
    '{\\"center\\":[14.47723212131153,50.090010810586364],'
    '\\"zoom\\":15.679,'
    '\\"features\\":[{\\"type\\":\\"LineString\\",\\"coordinates\\":'
    '[[14.4817548,50.0907159],[14.4723199,50.0889962]]}]}'
)
# The D6 homonym sentence, verbatim from the mining corpus row f60012522.
LIVE_OKRES_PROSE = (
    "Nabízíme k prodeji dva zemědělské pozemky o celkové výměře 22 432 m2, v krásném "
    "prostředí severních Čech, katastrální území Krásný Les u Frýdlantu, obec Krásný Les, "
    "okres Liberec. Parcela č. 1532/1 o výměře 13 832 m2 (orná půda)."
)


def live_body(locality: str, config: str | None, description: str | None = None) -> bytes:
    """The nodes the contract addresses, plus the map script when the page has one."""
    script = (f"<script type=\"module\">const mapdata = JSON.parse('{config}');</script>"
              if config is not None else "")
    prose = (f"<div id=\"collapse-inzerat-text\">{description}</div>"
             if description is not None else "")
    return (
        "<!DOCTYPE html><html lang=\"cs\"><head><title>MAXIMA REALITY</title></head><body>"
        "<div class=\"locality\">" + locality + "</div>" + script + prose +
        "<div class=\"podobne\"><h3>Podobné nemovitosti v naší nabídce</h3>"
        "<ul><li><a href=\"/nemovitosti/d40031686/\">Kounicova 42, okres Brno-venkov</a>"
        "</li></ul></div></body></html>"
    ).encode("utf-8")


def run(body: bytes, *, native: str = "fixture", entries: list[Entry] | None = None):
    payload = ArchivedPayload(
        id=1, source="maxima", source_id_native=native, page_kind="detail",
        payload_sha256="0" * 64, first_observed_at=FETCHED_AT, body=body)
    row = fx.listing("maxima", {}, native=native)
    return extract_page(payload, row, entries if entries is not None else ENTRIES,
                        register=REGISTER)


def claims_by_entry(result) -> dict[str, list]:
    found: dict[str, list] = {}
    for claim in result.claims:
        found.setdefault(claim.extractor_id, []).append(claim)
    return found


def one(result, entry_id: str):
    found = claims_by_entry(result).get(entry_id, [])
    assert len(found) == 1, f"{entry_id}: expected one claim, got {len(found)}"
    return found[0]


# ---------------------------------------------------------------- the shipped shape

def test_the_version_and_the_entry_id_set_are_exactly_what_this_wave_ships():
    """v2's twelve ids become six. The eight that carried a retired claim type or a deleted
    reader are GONE rather than re-typed (`mx.det.locality` was minted as a
    mestsky_obvod_name and an id never changes meaning), so every claim stamped with one of
    them is retracted at @2 rather than silently re-read under @3."""
    assert CONTRACT.version == 3
    assert {entry.entry_id for entry in ENTRIES} == ENTRY_IDS
    assert all(entry.reader in PAGE_READERS for entry in ENTRIES)


def test_each_claim_type_has_exactly_one_carrier_and_the_town_is_among_them():
    """Rule 25 / W1-c R1: two entries of one type reach the resolver with the same
    (source, extraction_method) and which one wins is the order the DB returned them in."""
    by_type: dict[str, list[str]] = {}
    for entry in ENTRIES:
        by_type.setdefault(entry.claim_type, []).append(entry.entry_id)
    assert all(len(ids) == 1 for ids in by_type.values()), by_type
    assert by_type["obec_name"] == ["mx.det.locality_obec"]
    assert set(by_type) <= contracts.CLAIM_TYPES


def test_the_town_entry_reads_div_locality_and_folds_a_statutory_city_obvod():
    """The town is `div.locality` segment 1 chained through `statutory_city_obec`: RÚIAN has
    no obec called 'Praha 3', so a town claim carrying the obvod resolves to nothing at all
    (W1-c R4)."""
    entry = BY_ID["mx.det.locality_obec"]
    assert entry.locator["reader"] == "html_text"
    assert entry.locator["css"] == "div.locality"
    assert list(entry.transform) == ["comma_segment:1@*", "statutory_city_obec"]
    assert one(run(live_body("Praha 3, Žižkov, Jeseniova", LIVE_CIRCLE)),
               "mx.det.locality_obec").value_text == "Praha"
    # Both spellings of an obvod fold: the ordinal one above and the hyphenated one the
    # pinned body carries.
    assert one(run(_PINNED.read_bytes()), "mx.det.locality_obec").value_text == "Brno"


def test_every_entry_claims_on_the_pinned_body_with_a_resolvable_span():
    """All six fire on the body the fixture-diff gate scores — the property that makes the
    golden a real gate rather than a record of six silences."""
    result = run(_PINNED.read_bytes())
    found = claims_by_entry(result)
    assert set(found) == ENTRY_IDS
    assert not result.refusals
    # `Brno-střed, Veveří, Grohova` — the obvod is folded to the city on the body the
    # permanent golden scores, so W1-c R4 is pinned by the gate and not only by a unit test.
    assert [found[i][0].value_text for i in
            ("mx.det.locality_obec", "mx.det.locality_quarter", "mx.det.locality_street",
             "mx.det.description_okres", "mx.det.map_geometry")] == [
        "Brno", "Veveří", "Grohova", "Brno-město", "Point"]
    # A span that does not resolve to its own quote is worse than no span (mig 382's CHECK
    # only tests substring-ness, so a span pointing at another occurrence still passes it).
    document = scope_html(_PINNED.read_bytes(), register=REGISTER)
    for claim in result.claims:
        assert claim.evidence_quote is not None, claim.extractor_id
        assert claim.span_start is not None and claim.span_end is not None, claim.extractor_id
        assert document.html[claim.span_start:claim.span_end] == claim.evidence_quote


def test_the_pinned_point_feature_is_licensed_as_a_portal_pin():
    """The C6 ladder decides the class, never the reader: `ARCHIVED_COORDINATE_RULES`
    names mx.det.map_features and `position_branch: portal_pin` is what admits it."""
    claim = one(run(_PINNED.read_bytes()), "mx.det.map_features")
    assert claim.value_geom_wkt == "POINT(16.60411 49.20256)"
    assert claim.licence_class == "portal"
    assert claim.declared_precision_label == "point" and claim.blur_evidence == "none"


def test_the_okres_entry_reads_the_description_and_resolves_the_d6_homonym():
    """f60012522 stores obec Petrovice / okres Ústí nad Labem while its own description says
    'obec Krásný Les, okres Liberec' — the other Krásný Les, ~100 km west. The description
    is the ONLY published signal that separates them."""
    entry = BY_ID["mx.det.description_okres"]
    assert entry.locator["css"] == "#collapse-inzerat-text"
    assert entry.extraction_method == "regex_text"
    claim = one(run(live_body("Krásný Les", None, LIVE_OKRES_PROSE), native="f60012522"),
                "mx.det.description_okres")
    assert claim.value_text == "Liberec"
    assert claim.evidence_quote == "okres Liberec"


def test_the_okres_pattern_stops_at_the_sentence_punctuation_and_keeps_a_hyphen():
    """'okres Jičín, Královéhradecký kraj' must not swallow the kraj, and 'okres Brno-město'
    must keep its hyphen — both spellings are in the mined corpus."""
    for prose, want in (("…, okres Jičín, Královéhradecký kraj. Obec Údrnice leží…",
                         "Jičín"),
                        ("…, obec Brno, okres Brno-město, Jihomoravský kraj.",
                         "Brno-město")):
        assert one(run(live_body("Údrnice, Únětice", None, prose)),
                   "mx.det.description_okres").value_text == want


# ---------------------------------------------------------------- the live captures

def test_a_live_circle_declares_its_own_blur_and_the_type_is_the_precision_label():
    """f60012682, fetched 2026-09-05. The centre is under `center`, not `coordinates`, and
    W1-c R5 makes the precision_declaration's VALUE its label."""
    result = run(live_body("Údrnice, Únětice", LIVE_CIRCLE), native="f60012682")
    pin = one(result, "mx.det.map_features")
    assert pin.value_geom_wkt == "POINT(15.265521169300019 50.360596371862215)"
    assert pin.declared_precision_label == "circle"
    # 06 §6.6 rule 7: a Circle is the ONE sanctioned case where blur rides on the
    # coordinate — the portal is drawing its own imprecision.
    assert pin.blur_evidence == "declared" and pin.licence_class == "portal"
    shape = one(result, "mx.det.map_geometry")
    assert shape.value_text == "Circle" and shape.declared_precision_label == "Circle"
    assert shape.claim_type == "precision_declaration"
    assert shape.value_text in BY_ID["mx.det.map_geometry"].precision_map["blurred_labels"]


def test_a_live_linestring_mints_the_segment_midpoint_that_json_point_would_have_lost():
    """a10070727, recon §3.4: a 2-point ~700 m line at zoom 15.679. `json_point` returns
    nothing for a LineString and LineStrings are two of the five live pages on record, so
    this is the arm that decides whether the coordinate entry is worth having at all — and
    it is the only arm the permanent golden cannot score (one body per portal)."""
    result = run(live_body("Praha 3, Žižkov, Jeseniova", LIVE_LINESTRING),
                 native="a10070727")
    pin = one(result, "mx.det.map_features")
    assert pin.value_geom_wkt == "POINT(14.47703735 50.089856049999995)"
    assert pin.declared_precision_label == "linestring"
    # And the reason the precision entry is NOT redundant with the pin: a Circle hands the
    # reader a radius, so the COORDINATE claim itself comes out `blur_evidence: declared`
    # (test above) — a LineString hands it none, so the pin says `none` and the blur is
    # stated only by the separate precision_declaration claim.
    assert pin.blur_evidence == "none" and pin.licence_class == "portal"
    shape = one(result, "mx.det.map_geometry")
    assert shape.value_text == "LineString" and shape.declared_precision_label == "LineString"
    assert shape.blur_evidence == "declared"
    assert shape.value_text in BY_ID["mx.det.map_geometry"].precision_map["blurred_labels"]
    # The contract caps a line at a street segment; only a Point may reach an address point.
    caps = BY_ID["mx.det.map_features"].precision_map["precision_cap"]
    assert caps["granularity_max"]["LineString"] == "street_segment"
    assert caps["position_source_max"]["LineString"] == "portal_pin"


def test_a_point_page_still_states_its_precision_rather_than_staying_silent():
    """The reason the precision entry reads the TYPE with `json_scalar` instead of the
    geometry ladder: the ladder emits nothing for a Point, so 'the portal drew a precise
    pin' and 'the portal published no map' were the same empty result."""
    shape = one(run(_PINNED.read_bytes()), "mx.det.map_geometry")
    assert shape.value_text == "Point" and shape.declared_precision_label == "Point"
    assert "Point" not in BY_ID["mx.det.map_geometry"].precision_map["blurred_labels"]


def test_a_two_feature_page_types_the_first_feature_and_never_the_view_centre():
    """d40026367 serves a Point AND a Circle. `then: /features/0` is the whole selection
    rule — and the view centre (15.2596,50.3634), which is what the LIVE parser stores,
    appears in no claim."""
    result = run(live_body("Údrnice, Únětice", LIVE_TWO_FEATURES), native="d40026367")
    pin = one(result, "mx.det.map_features")
    assert pin.value_geom_wkt == "POINT(15.271186828990166 50.370363263565366)"
    assert pin.declared_precision_label == "point" and pin.blur_evidence == "none"
    assert one(result, "mx.det.map_geometry").value_text == "Point"
    for claim in result.claims:
        assert "50.36339611563554" not in (claim.value_text or "")
        assert "50.36339611563554" not in (claim.value_geom_wkt or "")


def test_a_page_with_no_map_script_still_states_its_town():
    """f60012522 — the D6 homonym regression — carries no `JSON.parse` anywhere. The town
    entry still reads, so 'no map' and 'no page' stay distinguishable, and the one-segment
    line is an OBEC rather than a refusal (the @2 shape left that row townless)."""
    result = run(live_body("Krásný Les", None), native="f60012522")
    found = claims_by_entry(result)
    assert not {"mx.det.map_features", "mx.det.map_geometry"} & set(found)
    assert found["mx.det.locality_obec"][0].value_text == "Krásný Les"
    assert not {"mx.det.locality_quarter", "mx.det.locality_street"} & set(found)


def test_a_two_segment_line_claims_the_town_and_fabricates_no_street():
    """The fix the review asked for. `Údrnice, Únětice` is 2 of the 12 mined lines and
    `Únětice` is a ČÁST OBCE of Údrnice, not a street; `comma_segment:-1@2+` typed it
    street_name on both pinned regression rows. The mined corpus has 8 two-segment lines
    (and ZERO three-segment ones); 4 of the 8 tails are not streets (Únětice x2,
    Budějovické Předměstí, Rozvojová zóna), so the unconditional shape fabricated as often
    as it was right."""
    found = claims_by_entry(run(live_body("Údrnice, Únětice", LIVE_CIRCLE),
                                native="f60012682"))
    assert found["mx.det.locality_obec"][0].value_text == "Údrnice"
    assert "mx.det.locality_street" not in found
    assert "mx.det.locality_quarter" not in found
    assert list(BY_ID["mx.det.locality_street"].transform) == ["comma_segment:-1@3"]


def test_the_three_segment_gate_is_the_shared_doctrine_not_a_second_opinion():
    """Why `comma_segment:-1@3` and not a morphology test: `scraper/street.py:285-289`
    already rules that "a 3+-segment locality is 'City, Quarter, Street' — the last segment
    is reliably a street, so the morphology gate is only needed for the ambiguous 2-segment
    case", and `street_from_locality` applies `looks_like_czech_street` only when
    `len(parts) < 3`. On a three-segment line the shipped transform and the shared
    extractor therefore agree by construction; the `@3` arity IS that rule as contract data.
    Where they differ is the two-segment line, and that difference is measured in REPORT.md
    (morphology recovers 2 of the 8 mined tails; this transform recovers none)."""
    assert list(BY_ID["mx.det.locality_street"].transform) == ["comma_segment:-1@3"]
    for line, want in (("Brno-střed, Veveří, Grohova", "Grohova"),
                       ("Liberec, Liberec XIV-Ruprechtice, Baltská", "Baltská"),
                       ("Údrnice, Únětice", None),
                       ("Písek, Budějovické Předměstí", None)):
        shared = street.street_from_locality(line, position="last", require_morphology=True)
        found = claims_by_entry(run(live_body(line, None))).get("mx.det.locality_street")
        assert shared == want, line
        assert (found[0].value_text if found else None) == want, line


def test_the_three_segment_split_is_town_quarter_street():
    result = run(live_body("Liberec, Liberec XIV-Ruprechtice, Baltská", LIVE_CIRCLE))
    found = claims_by_entry(result)
    assert found["mx.det.locality_obec"][0].value_text == "Liberec"
    assert found["mx.det.locality_quarter"][0].value_text == "Liberec XIV-Ruprechtice"
    assert found["mx.det.locality_street"][0].value_text == "Baltská"
    # Diacritics survive the entity-encoded spelling and the claims are identical.
    entity = run(live_body("Praha 3, &#381;i&#382;kov, Jeseniova", LIVE_CIRCLE))
    assert claims_by_entry(entity)["mx.det.locality_quarter"][0].value_text == "Žižkov"


# ---------------------------------------------------------------- the refusals

def test_a_regional_zoom_refuses_the_coordinate_and_the_page_still_states_its_town():
    """The rail that exists because d40031686 draws a real centre at zoom 10.20, ~9.2 km
    from its stored pin and in a different okres."""
    config = ('{\\"center\\":[14.972620,49.989445],\\"zoom\\":10.20,\\"features\\":'
              '[{\\"type\\":\\"Point\\",\\"coordinates\\":[14.972620,49.989445]}]}')
    found = claims_by_entry(run(live_body("Kostelec nad Černými Lesy", config),
                                native="d40031686"))
    assert "mx.det.map_features" not in found
    assert found["mx.det.locality_obec"][0].value_text == "Kostelec nad Černými Lesy"


def test_an_empty_features_array_refuses_structurally_and_counts_nothing():
    """"features: [] emits no coordinate" is enforced by this entry's own `then` pointer
    missing, which is why v1's never-implemented `reject_empty_geometry` guard was dropped
    rather than written: a guard is `(lat, lon) -> bool` and there is no point to hand it."""
    config = '{\\"center\\":[14.972620,49.989445],\\"zoom\\":10.20,\\"features\\":[]}'
    result = run(live_body("Kostelec nad Černými Lesy", config))
    found = claims_by_entry(result)
    assert not {"mx.det.map_features", "mx.det.map_geometry"} & set(found)
    assert not result.refusals
    assert "reject_empty_geometry" not in BY_ID["mx.det.map_features"].guards


def test_only_the_rules_own_entry_id_licenses_this_portals_archived_pin():
    """The Mapy inventory used to veto above the substrate branch; with the geocoder gone
    the entry id `ARCHIVED_COORDINATE_RULES` names IS the licence, and a refusal is COUNTED
    under its reason rather than swallowed."""
    impostor = replace(BY_ID["mx.det.map_features"], entry_id="mx.det.not_the_rule")
    result = run(_PINNED.read_bytes(), entries=[impostor])
    assert result.claims == []
    assert result.refusals["unrecognised_archived_coordinate_locator"] == 1
    # The rest of the contract is untouched by a coordinate refusal.
    assert "mx.det.locality_obec" in claims_by_entry(run(_PINNED.read_bytes()))


def test_a_coordinate_entry_with_no_position_branch_is_refused_by_name():
    """Which branch of the portal's map produced a position IS its licence class (C6), and
    it is never inferred from what the reader stamped."""
    entry = BY_ID["mx.det.map_features"]
    stripped = {k: v for k, v in entry.locator.items() if k != "position_branch"}
    with pytest.raises(IntakeRefused) as excinfo:
        run(_PINNED.read_bytes(), entries=[replace(entry, locator=stripped)])
    assert "mx.det.map_features" in str(excinfo.value)


def test_the_decoy_block_is_unreachable_from_every_entry():
    """`.similar, .podobne` is this contract's only exclusion zone, and the sibling block is
    "an active mis-attribution hazard" [mine-maxima]: it names other listings' streets AND
    their okres, which the description entry would otherwise be free to read."""
    for body in (_PINNED.read_bytes(),
                 live_body("Údrnice, Únětice", LIVE_CIRCLE, LIVE_OKRES_PROSE)):
        for claim in run(body).claims:
            assert "Kounicova" not in (claim.value_text or "")
            assert "Nerudova" not in (claim.value_text or "")
            assert "Brno-venkov" not in (claim.value_text or "")


# ---------------------------------------------------------------- the standing rails

def test_maximas_archived_coordinate_rule_did_not_move():
    """A contract rewrite must not widen the C6 ladder. The rule was written in W2-2 and
    names ONE entry; a second coordinate-typed entry on this portal is refused
    'unrecognised_archived_coordinate_locator', and that is the point."""
    rule = claims_intake.ARCHIVED_COORDINATE_RULES["maxima"]
    assert (rule.entry_id, rule.licence_class, rule.geocoded_licence_class) == (
        "mx.det.map_features", "portal", None)


def test_the_zoom_rail_is_executable_contract_data():
    """v2 declared the threshold twice — `precision_cap.reject_when: [zoom_le_12]` beside
    the executable `locator.reject_zoom_at_or_below` — and a contract where two spellings
    of one rule can disagree states a threshold it may not apply. One spelling now."""
    entry = BY_ID["mx.det.map_features"]
    assert entry.locator["reject_zoom_at_or_below"] == 12
    assert "reject_when" not in entry.precision_map["precision_cap"]


def test_no_entry_reads_a_listings_column_or_the_payload():
    """maxima is a BODY portal at @3: the hourly payload lane mints nothing for it, which is
    a coverage dependency on the stored-body lane, not a silent gap."""
    assert all(entry.surface in {"html_selector", "map_config"} for entry in ENTRIES)
    assert not claims_intake.extract_listing(
        fx.listing("maxima", fx.MAXIMA_PAGE, native="d40031686"), ENTRIES).claims


def test_every_comma_segment_transform_arg_in_the_fleet_parses():
    """`comma_segment:2of3` is a plausible typo for `comma_segment:2@3`, and a malformed arg
    is a no-op that mines nothing forever. `_check_executable` validates the transform NAME,
    not its arg; this is the only rail that can see the arg."""
    for contract in contracts.load_all():
        for entry in contract.entries:
            for spec in entry.transform:
                name, _, arg = spec.partition(":")
                if name == "comma_segment":
                    assert claims_common._COMMA_SEGMENT_RE.match(arg), \
                        f"{entry.entry_id}: {spec!r}"


@pytest.mark.parametrize("listing_id", ["f60012522", "d40026367", "f60012682"])
def test_every_pinned_regression_still_has_a_captured_body(listing_id):
    """Each body is `scraper.maxima_parser.parse_detail` over the live page of 2026-09-05."""
    doc = json.loads((_BODIES / f"{listing_id}.json").read_text(encoding="utf-8"))
    assert doc["raw_json"]["id"] == listing_id
    assert doc["_http_status"] == 200
    # The stored lat/lon is the parser's read of the map VIEW CENTRE — the trap the map
    # entry replaces. d40026367 and f60012682 are the same plot, and their view centres are
    # ~830 m apart while their declared circle centres are ~12 m apart.
    if listing_id == "f60012522":
        assert doc["lat"] is None and doc["raw_json"]["coords"]["source"] is None
    else:
        assert doc["raw_json"]["coords"]["source"] == "page"


def test_the_captured_bodies_carry_no_broker_identity():
    """The PII rail these fixtures ship under: `parse_detail` output is property facts only
    — no broker name, phone or e-mail — and a re-capture must not quietly widen that."""
    for path in sorted(_BODIES.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert set(doc["raw_json"]) <= {
            "id", "title", "price_text", "locality_text", "maxima_ref", "coords", "params"}
        blob = json.dumps(doc, ensure_ascii=False)
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", blob), path.name
        assert not re.search(r"(?<!\d)(?:\+420[ ]?)?\d{3}[ ]\d{3}[ ]\d{3}(?!\d)", blob), \
            path.name
