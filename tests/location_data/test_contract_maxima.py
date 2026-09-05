"""maxima@2 — the activation, exercised through the SHIPPED contract rather than a mock.

Every entry here is read off `contracts/portals/maxima.yaml` and run by the real archive
lane (`extract_payload`, including the C6 licence ladder), so a locator edit that stops
matching fails here rather than mining zero claims in production for a month.

Two substrates, both real:
  * `tests/fixtures/location_w2/maxima_detail.html` — the pinned body the fixture-diff gate
    scores. It carries a Point feature and a three-segment locality line.
  * three LIVE captures, fetched 2026-09-05 (HTTP 200, one request each, 2 s apart) from
    `https://nemovitosti.maxima.cz/nemovitosti/<id>/`. Their `JSON.parse('…')` argument is
    reproduced here BYTE-FOR-BYTE — escapes intact — because the escaping is the thing under
    test, and the surrounding page is reduced to the two nodes the contract addresses (the
    live pages are 64-96 KB of theme chrome). Nothing is invented: every literal below was
    copied out of a captured response.

What the live captures settled, which the 2026-08-10 recon could not:
  * a Circle is `{"type":"Circle","center":[lon,lat],"radius":<degrees>}` — `center`, NOT
    `coordinates`. The ladder accepts both; before this it was accepting a guess.
  * d40026367 serves TWO features (a Point, then a Circle), so `then: /features/0` types
    that page as a Point.
  * f60012522 — the D6 homonym regression — serves NO map script at all, so the archived
    body mints no coordinate for it. The stored pin has no portal geometry behind it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from location_data import claims_intake, contracts
from location_data.claims_intake import Entry, IntakeRefused
from location_data.claims_remine_archive import (
    ARCHIVE_READERS,
    ArchivedPayload,
    extract_payload,
)
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

# --- the live captures, verbatim ------------------------------------------------------
# `\"` throughout: maxima serves the config as a JS single-quoted string literal, which is
# the whole reason `decode: js_string` exists on these three entries.
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


def live_body(locality: str, title: str, config: str | None) -> bytes:
    """The two nodes the contract addresses, plus the map script when the page has one."""
    script = (f"<script type=\"module\">const mapdata = JSON.parse('{config}');</script>"
              if config is not None else "")
    return (
        "<!DOCTYPE html><html lang=\"cs\"><head><title>" + title + "</title></head><body>"
        "<div class=\"locality\">" + locality + "</div>" + script +
        "<div class=\"podobne\"><h3>Podobné nemovitosti v naší nabídce</h3>"
        "<ul><li><a href=\"/nemovitosti/d40031686/\">Kostelec nad Černými Lesy</a></li>"
        "</ul></div></body></html>"
    ).encode("utf-8")


def run(body: bytes, *, native: str = "fixture", in_mapy_inventory: bool = False,
        entries: list[Entry] | None = None):
    payload = ArchivedPayload(
        id=1, source="maxima", source_id_native=native, page_kind="detail",
        payload_sha256="0" * 64, first_observed_at=FETCHED_AT, body=body)
    row = fx.listing("maxima", {}, native=native, in_mapy_inventory=in_mapy_inventory)
    return extract_payload(payload, row, entries if entries is not None else ENTRIES,
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


# ---------------------------------------------------------------- the shipped entry set

def test_the_activated_entry_set_is_exactly_the_six_this_version_switches_on():
    """`mx.det.view_centre` and `mx.desc.homonym` stay INERT at v2, and that is the point:
    the view centre is 130 m and 660 m from the circle centre on the two Circle rows and
    9.2 km out on the empty-features one, so this wave mints no claim for it."""
    executable = {e.entry_id for e in ENTRIES if e.reader in ARCHIVE_READERS}
    assert executable == {
        "mx.det.map_features", "mx.det.map_shape", "mx.det.zoom",
        "mx.det.locality", "mx.det.locality_quarter", "mx.det.locality_street",
        "mx.det.title",
    }
    assert BY_ID["mx.det.view_centre"].reader is None
    assert BY_ID["mx.desc.homonym"].reader is None
    assert CONTRACT.version == 2 and CONTRACT.shadow is True


def test_every_activated_entry_claims_on_the_pinned_body_with_a_resolvable_span():
    """Six of the seven claim on this body. `mx.det.map_shape` is the one that does not,
    and its silence is the contract working: the pinned page draws a POINT, a point
    declares no uncertainty shape, and migration 383's class default is the honest bound
    there — inventing a radius would be worse than having none."""
    result = run(_PINNED.read_bytes())
    found = claims_by_entry(result)
    assert set(found) == {
        "mx.det.map_features", "mx.det.zoom", "mx.det.locality",
        "mx.det.locality_quarter", "mx.det.locality_street", "mx.det.title",
    }
    assert not result.absences
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
    # A Point declares no uncertainty SHAPE; the shape entry still claims nothing for it.
    shape = claims_by_entry(run(_PINNED.read_bytes())).get("mx.det.map_shape")
    assert shape is None or all(c.value_shape_wkt is None for c in shape)


def test_the_zoom_entry_records_the_number_the_rail_is_judged_against():
    claim = one(run(_PINNED.read_bytes()), "mx.det.zoom")
    assert claim.claim_type == "map_zoom"
    assert claim.value_text == "15" and claim.value_num == 15.0


# ---------------------------------------------------------------- the live captures

def test_a_live_circle_declares_its_own_blur_and_a_radius_in_metres():
    """f60012682, fetched 2026-09-05. This is the shape the 2026-08-10 recon could not
    state: the centre is under `center`, not `coordinates`, and the radius is DEGREES."""
    result = run(live_body("Údrnice, Únětice",
                           "Prodej pozemku ostatní, 2&nbsp;621&nbsp;m2  Údrnice, Únětice",
                           LIVE_CIRCLE),
                 native="f60012682")
    pin = one(result, "mx.det.map_features")
    assert pin.value_geom_wkt == "POINT(15.265521169300019 50.360596371862215)"
    assert pin.declared_precision_label == "circle"
    # 06 §6.6 rule 7: a Circle is the ONE sanctioned case where blur rides on the
    # coordinate — the portal is drawing its own imprecision.
    assert pin.blur_evidence == "declared" and pin.licence_class == "portal"
    shape = one(result, "mx.det.map_shape")
    assert shape.value_text == "Circle"
    assert shape.value_shape_wkt.startswith("POINT(")
    assert shape.declared_radius_m == pytest.approx(406.8, abs=0.5)
    assert shape.value_jsonb["radius_basis"] == "radius_deg_times_111000"
    assert one(result, "mx.det.zoom").value_num == pytest.approx(13.780432630830754)


def test_a_two_feature_page_types_the_first_feature_and_never_the_view_centre():
    """d40026367 serves a Point AND a Circle. `then: /features/0` is the whole selection
    rule — and the view centre (15.2596,50.3634), which is what the LIVE parser stores,
    appears in no claim."""
    result = run(live_body("Údrnice, Únětice",
                           "Prodej rodinného domu, 121&nbsp;m2  Údrnice, Únětice",
                           LIVE_TWO_FEATURES),
                 native="d40026367")
    pin = one(result, "mx.det.map_features")
    assert pin.value_geom_wkt == "POINT(15.271186828990166 50.370363263565366)"
    assert pin.declared_precision_label == "point" and pin.blur_evidence == "none"
    assert "mx.det.map_shape" not in claims_by_entry(result)
    for claim in result.claims:
        assert "50.36339611563554" not in (claim.value_text or "")
        assert "50.36339611563554" not in (claim.value_geom_wkt or "")


def test_a_page_with_no_map_script_mints_no_map_claim_at_all():
    """f60012522 — the D6 homonym regression — carries no `JSON.parse` anywhere. The
    locality entries still read, so "no map" and "no page" stay distinguishable."""
    result = run(live_body("Krásný Les",
                           "Prodej pozemku trvalý travní porost, 22&nbsp;432&nbsp;m2  "
                           "Krásný Les", None),
                 native="f60012522")
    found = claims_by_entry(result)
    assert not {"mx.det.map_features", "mx.det.map_shape", "mx.det.zoom"} & set(found)
    # A one-segment line is an OBEC, and every locality entry refuses it rather than
    # typing it as an obvod, a quarter or a street.
    assert set(found) == {"mx.det.title"}


def test_a_two_segment_line_yields_no_quarter():
    result = run(live_body("Údrnice, Únětice", "Prodej pozemku ostatní", LIVE_CIRCLE),
                 native="f60012682")
    found = claims_by_entry(result)
    assert found["mx.det.locality"][0].value_text == "Údrnice"
    assert found["mx.det.locality_street"][0].value_text == "Únětice"
    assert "mx.det.locality_quarter" not in found


def test_the_three_segment_split_is_obvod_quarter_street():
    result = run(live_body("Praha 3, Žižkov, Jeseniova", "Prodej bytu", LIVE_CIRCLE))
    found = claims_by_entry(result)
    assert found["mx.det.locality"][0].value_text == "Praha 3"
    assert found["mx.det.locality_quarter"][0].value_text == "Žižkov"
    assert found["mx.det.locality_street"][0].value_text == "Jeseniova"
    # Diacritics survive the entity-encoded spelling and the claims are identical.
    entity = run(live_body("Praha 3, &#381;i&#382;kov, Jeseniova", "Prodej bytu",
                           LIVE_CIRCLE))
    assert claims_by_entry(entity)["mx.det.locality_quarter"][0].value_text == "Žižkov"


# ---------------------------------------------------------------- the refusals

def test_a_regional_zoom_refuses_the_coordinate_and_leaves_the_zoom_claim_standing():
    """The second rail, and a different failure from an empty `features`: d40031686 draws
    a real centre at zoom 10.20, ~9.2 km from its stored pin and in a different okres."""
    config = ('{\\"center\\":[14.972620,49.989445],\\"zoom\\":10.20,\\"features\\":'
              '[{\\"type\\":\\"Point\\",\\"coordinates\\":[14.972620,49.989445]}]}')
    result = run(live_body("Kostelec nad Černými Lesy", "Prodej pozemku", config),
                 native="d40031686")
    found = claims_by_entry(result)
    assert "mx.det.map_features" not in found
    assert found["mx.det.zoom"][0].value_num == pytest.approx(10.20)


def test_an_empty_features_array_refuses_structurally_and_writes_no_absence():
    """"features: [] emits no coordinate" is enforced by this entry's own `then` pointer
    missing, which is why v1's never-implemented `reject_empty_geometry` guard was dropped
    rather than written: a guard is `(lat, lon) -> bool` and there is no point to hand it."""
    config = '{\\"center\\":[14.972620,49.989445],\\"zoom\\":10.20,\\"features\\":[]}'
    result = run(live_body("Kostelec nad Černými Lesy", "Prodej pozemku", config))
    found = claims_by_entry(result)
    assert not {"mx.det.map_features", "mx.det.map_shape"} & set(found)
    assert [a.reason for a in result.absences] == []
    assert "reject_empty_geometry" not in BY_ID["mx.det.map_features"].guards


def test_a_listing_in_the_mapy_inventory_gets_no_archived_coordinate():
    """Rung (a) of the ladder sits ABOVE the substrate branch, so the licence veto reaches
    the archived body too. The refusal is RECORDED as an absence, never swallowed."""
    result = run(_PINNED.read_bytes(), in_mapy_inventory=True)
    assert "mx.det.map_features" not in claims_by_entry(result)
    assert any(a.field_ == "coordinate" for a in result.absences)
    # The rest of the contract is untouched by a coordinate veto.
    assert "mx.det.locality_street" in claims_by_entry(result)


def test_a_coordinate_entry_with_no_position_branch_is_refused_by_name():
    """Which branch of the portal's map produced a position IS its licence class (C6), and
    it is never inferred from what the reader stamped."""
    entry = BY_ID["mx.det.map_features"]
    stripped = {k: v for k, v in entry.locator.items() if k != "position_branch"}
    with pytest.raises(IntakeRefused) as excinfo:
        run(_PINNED.read_bytes(), entries=[replace(entry, locator=stripped)])
    assert "mx.det.map_features" in str(excinfo.value)


def test_the_decoy_block_is_unreachable_from_every_activated_entry():
    """`.similar, .podobne` is this contract's only exclusion zone, and the sibling block is
    "an active mis-attribution hazard" [mine-maxima]."""
    result = run(_PINNED.read_bytes())
    for claim in result.claims:
        assert "Kounicova" not in (claim.value_text or "")
        assert "Nerudova" not in (claim.value_text or "")


# ---------------------------------------------------------------- the standing rails

def test_maximas_archived_coordinate_rule_did_not_move():
    """Activating a portal must not widen the C6 ladder. The rule was written in W2-2 and
    names ONE entry; a second coordinate-typed entry on this portal is refused
    'unrecognised_archived_coordinate_locator', and that is the point."""
    rule = claims_intake.ARCHIVED_COORDINATE_RULES["maxima"]
    assert (rule.entry_id, rule.licence_class, rule.geocoded_licence_class) == (
        "mx.det.map_features", "portal", None)


def test_the_zoom_rail_is_declared_once_and_executed_once():
    """The declarative `precision_cap.reject_when` and the executable
    `locator.reject_zoom_at_or_below` are two spellings of one rule; a contract where they
    disagree states a threshold it does not apply."""
    entry = BY_ID["mx.det.map_features"]
    assert entry.locator["reject_zoom_at_or_below"] == 12
    assert "zoom_le_12" in entry.precision_map["precision_cap"]["reject_when"]


def test_every_comma_segment_transform_arg_in_the_fleet_parses():
    """`comma_segment:2of3` is a plausible typo for `comma_segment:2@3`, and a malformed arg
    is a no-op that mines nothing forever. `_check_executable` validates the transform NAME,
    not its arg; this is the only rail that can see the arg."""
    for contract in contracts.load_all():
        for entry in contract.entries:
            for spec in entry.transform:
                name, _, arg = spec.partition(":")
                if name == "comma_segment":
                    assert claims_intake._COMMA_SEGMENT_RE.match(arg), \
                        f"{entry.entry_id}: {spec!r}"


@pytest.mark.parametrize("listing_id", ["f60012522", "d40026367", "f60012682"])
def test_every_pinned_regression_now_has_a_captured_body(listing_id):
    """All three pinned ids were `listings_without_a_fixture_body` in maxima@1. Each body
    below is `scraper.maxima_parser.parse_detail` over the live page of 2026-09-05."""
    doc = json.loads((_BODIES / f"{listing_id}.json").read_text(encoding="utf-8"))
    assert doc["raw_json"]["id"] == listing_id
    assert doc["_http_status"] == 200
    # The stored lat/lon is the parser's read of the map VIEW CENTRE — the trap this
    # activation replaces. d40026367 and f60012682 are the same plot, and their view
    # centres are ~830 m apart while their declared circle centres are ~12 m apart.
    if listing_id == "f60012522":
        assert doc["lat"] is None and doc["raw_json"]["coords"]["source"] is None
    else:
        assert doc["raw_json"]["coords"]["source"] == "page"
