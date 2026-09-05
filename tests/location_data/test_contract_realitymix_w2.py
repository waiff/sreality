"""realitymix@4 — the W2-8 activation, scored against the REAL archived body.

Every test here runs the SHIPPED contract entries (no hand-written locators) through the
real readers, the real scoper and the real licence ladder over
`tests/fixtures/portal_html/realitymix_detail.html` — the captured page of listing 8662169,
which `contracts/portals/realitymix.yaml` pins as a regression. The pinned modelled page
(`tests/fixtures/location_w2/realitymix_detail.html`) is scored by the fixture-diff golden
instead; a selector that works on a page written from the contract proves the shape, not the
population, and three of v3's locators matched nothing on the real body while passing review.

That capture is therefore LOAD-BEARING: re-capturing it moves the expected values below and
the normalised digest `test_payload_norm_by_page_kind` pins. Its agent block was scrubbed with
`scripts/fetch_and_anonymize_fixtures.py --scrub-contacts --name …` before this suite was
written, so the name in it is the placeholder and not a real broker.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from location_data import contracts
from location_data.claims_intake import Entry, ListingRow
from location_data.claims_remine_archive import (
    ARCHIVE_READERS,
    ARCHIVED_COORDINATE_RULES,
    ArchivedPayload,
    _licensed_coordinate,
    extract_payload,
    stamp_archive_claim,
)
from location_data.claims_intake import ARCHIVE_ONLY_READERS
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
_ARCHIVED_BODY = _ROOT / "tests" / "fixtures" / "portal_html" / "realitymix_detail.html"
_PINNED_BODY = _ROOT / "tests" / "fixtures" / "location_w2" / "realitymix_detail.html"

NATIVE = "8662169"
FETCHED_AT = datetime(2026, 8, 13, 4, 30, tzinfo=UTC)

CONTRACT = {c.source: c for c in contracts.load_all()}["realitymix"]
ENTRIES = {e.entry_id: e for e in fx.entries_for("realitymix")}
REGISTER = ScopeRegister.from_zones("realitymix", CONTRACT.exclusion_zones)


def register_for(zones: list[dict]) -> ScopeRegister:
    return ScopeRegister.from_zones("realitymix", zones)


def document(body: bytes | str | None = None) -> ScopedDocument:
    if body is None:
        body = _ARCHIVED_BODY.read_bytes()
    if isinstance(body, str):
        body = body.encode("utf-8")
    return scope_html(body, register=REGISTER)


def row(*, in_mapy_inventory: bool = False) -> ListingRow:
    return fx.listing("realitymix", {}, native=NATIVE,
                      in_mapy_inventory=in_mapy_inventory)


def payload(body: bytes | None = None) -> ArchivedPayload:
    return ArchivedPayload(
        id=9001, source="realitymix", source_id_native=NATIVE, page_kind="detail",
        payload_sha256="ab" * 32, first_observed_at=FETCHED_AT,
        body=_ARCHIVED_BODY.read_bytes() if body is None else body)


def read(entry_id: str, doc: ScopedDocument | None = None, *, listing: ListingRow | None = None):
    entry = ENTRIES[entry_id]
    doc = document() if doc is None else doc
    return ARCHIVE_READERS[str(entry.reader)](
        entry, listing or row(), payload(), doc)


def value(entry_id: str, doc: ScopedDocument | None = None) -> str | None:
    reads = read(entry_id, doc)
    assert len(reads) <= 1, f"{entry_id} produced {len(reads)} reads; every v4 entry is one"
    return reads[0].claim.value_text if reads else None


# ------------------------------------------------------------------ the contract itself

def test_the_activation_is_shadowed_and_names_only_registered_readers():
    """A reader in `ARCHIVE_READERS` but not in `ARCHIVE_ONLY_READERS` takes the HOURLY W1
    intake down for this portal, so the pair is asserted per entry rather than fleet-wide."""
    assert CONTRACT.version == 4 and CONTRACT.shadow is True
    dom = [e for e in CONTRACT.entries if e.reader in ARCHIVE_READERS]
    assert {e.entry_id for e in dom} == {
        "rm.det.gps", "rm.det.agency_gps_flag", "rm.det.agency_est_flag",
        "rm.det.address_all_segments", "rm.det.form_address", "rm.det.map_address",
        "rm.det.map_obec", "rm.det.map_okres", "rm.det.map_street",
        "rm.det.map_house_number_cp", "rm.det.map_house_number_co",
        "rm.det.breadcrumb_geo", "rm.det.breadcrumb_kraj", "rm.det.breadcrumb_obec",
        "rm.det.breadcrumb_quarter"}
    for entry in dom:
        assert entry.reader in ARCHIVE_ONLY_READERS, entry.entry_id
        assert entry.page_kind == "detail"
        for spec in entry.transform:
            assert spec.partition(":")[0] in contracts.IMPLEMENTED_TRANSFORMS, entry.entry_id


def test_the_three_entries_left_inert_are_the_ones_no_registry_can_execute():
    """og_meta and url_slug have no reader admitting those substrates and `llm_text` has no
    registry at all — inert on purpose, not forgotten."""
    assert {e.entry_id for e in CONTRACT.entries if e.reader is None} == {
        "rm.det.og_psc", "rm.det.slug", "rm.desc.cadastral"}


# ------------------------------------------------------------------ exclusion zones

def test_the_v4_zones_strip_the_agent_the_agency_and_the_neighbour_carousel():
    doc = document()
    assert doc.is_complete and doc.nodes_removed == 6
    # The agency office address is the 5-digit decoy [mine-realitymix finding 8] names, the
    # agent name is the page's only personal data, and `Vysočany` is the operator's own seat
    # in the footer.
    for decoy in ("Karla Čapka 1357", "35601", "Jan Novák", "Vysočany"):
        assert not doc.contains(decoy), decoy
    # The two v3 zones are RETAINED and match nothing on the served page — a zone that
    # compiles and matches nothing is not a hole, and `zones_unmatched` is what reports it
    # (per selector, so the compound zone reports as its two halves).
    assert {".broker-contact", ".contact-box"} <= set(doc.zones_unmatched)
    # NOT claimed: the operator's seat also appears in the GDPR consent prose, outside the
    # footer. No entry can reach it (every reader here is anchored on `#print-map`, the
    # contact-form div or the ld+json block) and a zone written for a legal notice would be
    # speculative, so it is recorded here rather than silently zoned.
    assert doc.contains("Na Harfě")


def test_the_subject_map_node_survives_the_zones():
    """The other half of every zone assertion: a scoper that strips the page also makes the
    decoys unreachable and is not a fix."""
    pin = document().css_first("div#print-map")
    assert pin is not None
    assert pin.attributes["data-gps-lat"] == "50.427238"
    assert pin.attributes["data-address"] == "Stráň, Potůčky, okres Karlovy Vary"


# ------------------------------------------------------------------ the coordinate

def test_the_gps_pair_is_a_portal_pin_through_the_real_licence_ladder():
    reads = read("rm.det.gps")
    assert len(reads) == 1 and reads[0].position_branch == "portal_pin"
    claim = stamp_archive_claim(reads[0].claim, payload(),
                                scope_version=document().scope_version)
    licensed, reason = _licensed_coordinate(claim, row(), ENTRIES["rm.det.gps"],
                                            reads[0].position_branch)
    assert licensed is not None, reason
    assert licensed.value_geom_wkt == "POINT(12.742544 50.427238)"
    assert licensed.licence_class == "portal"


def test_a_coordinate_from_any_other_entry_id_is_refused_by_the_ladder():
    """`ARCHIVED_COORDINATE_RULES` names ONE realitymix entry (rm.det.gps). The rung exists
    so a future entry cannot license a position by declaring a branch."""
    assert ARCHIVED_COORDINATE_RULES["realitymix"].entry_id == "rm.det.gps"
    reads = read("rm.det.gps")
    claim = stamp_archive_claim(reads[0].claim, payload(),
                                scope_version=document().scope_version)
    impostor = replace(ENTRIES["rm.det.gps"], entry_id="rm.det.not_the_rule")
    licensed, reason = _licensed_coordinate(claim, row(), impostor, "portal_pin")
    assert licensed is None and reason


def test_a_listing_in_the_mapy_inventory_yields_no_archived_coordinate():
    """The Mapy veto sits ABOVE the substrate branch, so it reaches the archived body too."""
    reads = read("rm.det.gps")
    claim = stamp_archive_claim(reads[0].claim, payload(),
                                scope_version=document().scope_version)
    licensed, reason = _licensed_coordinate(
        claim, row(in_mapy_inventory=True), ENTRIES["rm.det.gps"], "portal_pin")
    assert licensed is None and "mapy" in reason.lower()


# ------------------------------------------------------------------ the declared mode

def test_the_declared_mode_is_read_from_the_attributes_and_not_from_the_sentence_pair():
    """The measurement that retired v3's locator: BOTH sentence blocks are served on every
    page and the map JS reveals one, so a first-match selector over the pair reports "gps"
    100% of the time. `.advert-map__text--gps, .advert-map__text--estimated` matching two
    nodes here IS that measurement."""
    doc = document()
    assert len(doc.css(".advert-map__text--gps, .advert-map__text--estimated")) == 2
    assert value("rm.det.agency_gps_flag") == "gps"
    assert read("rm.det.agency_est_flag", doc) == []


def test_the_orientational_branch_is_its_own_entry_with_its_own_selector():
    """`html_marker` is presence-ONLY, so the absent branch is a `:not([...])` selector
    rather than a second label — and it is the branch that carries the blur."""
    body = ('<html><body><div id="print-map" '
            'data-address="Zlín"></div></body></html>')
    doc = document(body)
    assert read("rm.det.agency_gps_flag", doc) == []
    reads = read("rm.det.agency_est_flag", doc)
    assert len(reads) == 1
    assert reads[0].claim.value_text == "estimated"
    assert reads[0].claim.blur_evidence == "declared"
    assert reads[0].claim.declared_precision_label == "estimated"


def test_a_blank_gps_attribute_produces_no_declaration_at_all():
    """The honest under-claim: `data-gps-lat=""` is neither branch — the marker refuses a
    blank attribute and the `:not()` selector still matches the attribute's presence."""
    body = ('<html><body><div id="print-map" data-gps-lat="" data-gps-lon="" '
            'data-address="Zlín"></div></body></html>')
    doc = document(body)
    assert read("rm.det.agency_gps_flag", doc) == []
    assert read("rm.det.agency_est_flag", doc) == []


# ------------------------------------------------------------------ the comma address

def test_the_live_page_quarter_is_not_published_as_a_street():
    """THE regression this activation exists for. 8662169's `data-address` segment 0 is
    "Stráň", a ČÁST OBCE — corroborated by `data-form-address` and by the breadcrumb tail —
    so a positional street read would fabricate a street on this page."""
    assert value("rm.det.map_street") is None
    assert value("rm.det.map_house_number_cp") is None
    assert value("rm.det.map_house_number_co") is None


def test_the_split_roles_the_live_page_does_carry():
    assert value("rm.det.map_address") == "Stráň, Potůčky, okres Karlovy Vary"
    assert value("rm.det.map_obec") == "Potůčky"
    assert value("rm.det.map_okres") == "Karlovy Vary"
    # Three segments: the segment before the obec IS the leading one, so the městský obvod
    # role fires on nothing rather than typing a část obce as an obvod.
    assert value("rm.det.address_all_segments") is None


@pytest.mark.parametrize(
    "address,street,cp,co,obec,okres,obvod",
    [("Křimická, Plzeň 3, Plzeň, okres Plzeň-město",
      "Křimická", None, None, "Plzeň", "Plzeň-město", "Plzeň 3"),
     ("Křimická 655/31, Plzeň 3, Plzeň, okres Plzeň-město",
      "Křimická", "655", "31", "Plzeň", "Plzeň-město", "Plzeň 3"),
     ("Křimická 655, Plzeň 3, Plzeň, okr. Plzeň-město",
      "Křimická", "655", None, "Plzeň", "Plzeň-město", "Plzeň 3"),
     ("Plzeň 3, Plzeň, okres Plzeň-město", None, None, None, "Plzeň", "Plzeň-město", None),
     ("Zlín", None, None, None, "Zlín", None, None)])
def test_the_address_shapes_this_portal_serves(address, street, cp, co, obec, okres, obvod):
    """The four `data-address` shapes, each read by the SHIPPED entries. The abbreviated
    `okr.` tail is in here because `strip_prefix:"okres "` would have published it verbatim —
    which is why the okres role is keyed on the qualifier instead."""
    doc = document(f'<html><body><div id="print-map" data-gps-lat="49.7" '
                   f'data-gps-lon="13.3" data-address="{address}"></div></body></html>')
    assert value("rm.det.map_street", doc) == street
    assert value("rm.det.map_house_number_cp", doc) == cp
    assert value("rm.det.map_house_number_co", doc) == co
    assert value("rm.det.map_obec", doc) == obec
    assert value("rm.det.map_okres", doc) == okres
    assert value("rm.det.address_all_segments", doc) == obvod
    assert value("rm.det.map_address", doc) == address


def test_a_house_number_is_never_claimed_off_a_segment_that_is_not_a_street():
    """"Plzeň 3" carries a trailing number and is not a street, so the number gate has to be
    the STREET gate — `address_part_house_number` runs the identical tests."""
    doc = document('<html><body><div id="print-map" '
                   'data-address="Plzeň 3, Plzeň, okres Plzeň-město"></div></body></html>')
    assert value("rm.det.map_house_number_cp", doc) is None


# ------------------------------------------------------------------ the form address

def test_the_form_address_carrier_is_not_an_input():
    """v3's `input[data-form-address]` matched NOTHING on this body. Pinned so it cannot come
    back: the carrier is a div, and all three copies stamp the same value."""
    doc = document()
    assert doc.css_first("input[data-form-address]") is None
    assert doc.css_first("[data-advert-detail-contact-form][data-form-address]") is not None
    assert value("rm.det.form_address") == "Stráň"


# ------------------------------------------------------------------ the breadcrumb chain

@pytest.mark.parametrize(
    "entry_id,expected",
    [("rm.det.breadcrumb_kraj", "Karlovarský kraj"),
     ("rm.det.breadcrumb_geo", "Karlovy Vary"),
     ("rm.det.breadcrumb_obec", "Potůčky"),
     ("rm.det.breadcrumb_quarter", "Stráň")])
def test_the_live_breadcrumb_anchors_at_position_four(entry_id, expected):
    """This page's category path is two-level (domy/pronajem), so its geo chain starts at
    position 4 while the recon's byty/2+1/pronajem sample starts at 5. v3's
    `positions: [5,6,7,8]` was wrong on this page; the kraj slug is the anchor."""
    assert value(entry_id) == expected


def test_a_chain_that_stops_at_the_obec_claims_no_quarter():
    body = ('<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":['
            '{"@type":"ListItem","position":4,"item":'
            '{"@id":"https://realitymix.cz/reality/pozemky/prodej/zlinsky",'
            '"name":"Zlínský kraj"}},'
            '{"@type":"ListItem","position":5,"item":'
            '{"@id":"https://realitymix.cz/reality/pozemky/prodej/zlinsky/zlin",'
            '"name":"Zlín"}},'
            '{"@type":"ListItem","position":6,"item":'
            '{"@id":"https://realitymix.cz/reality/pozemky/prodej/zlinsky/zlin/zlin",'
            '"name":"Zlín"}}]}</script></head><body></body></html>')
    doc = document(body)
    assert value("rm.det.breadcrumb_kraj", doc) == "Zlínský kraj"
    assert value("rm.det.breadcrumb_obec", doc) == "Zlín"
    assert read("rm.det.breadcrumb_quarter", doc) == []


def test_an_unknown_kraj_slug_costs_coverage_and_never_correctness():
    """Eleven of the fourteen anchor slugs are unverified. The reader fails closed, so a
    wrong one drops the chain instead of mis-typing it — and a kraj with zero breadcrumb
    claims and non-zero listings is how it gets found."""
    body = ('<html><head><script type="application/ld+json">'
            '{"@type":"BreadcrumbList","itemListElement":['
            '{"@type":"ListItem","position":4,"item":'
            '{"@id":"https://realitymix.cz/reality/domy/pronajem/neverland",'
            '"name":"Neverland"}},'
            '{"@type":"ListItem","position":5,"item":'
            '{"@id":"https://realitymix.cz/reality/domy/pronajem/neverland/x",'
            '"name":"X"}}]}</script></head><body></body></html>')
    doc = document(body)
    for entry_id in ("rm.det.breadcrumb_kraj", "rm.det.breadcrumb_geo",
                     "rm.det.breadcrumb_obec", "rm.det.breadcrumb_quarter"):
        assert read(entry_id, doc) == []


# ------------------------------------------------------------------ the whole lane

def test_the_lane_over_the_live_body_yields_exactly_these_claims():
    """`extract_payload` end to end: the readers, the scoper, the ladder and the evidence
    assertions, on the page the contract pins."""
    result = extract_payload(payload(), row(), list(ENTRIES.values()), register=REGISTER)
    assert {(c.extractor_id, c.value_geom_wkt or c.value_text) for c in result.claims} == {
        ("rm.det.gps", "POINT(12.742544 50.427238)"),
        ("rm.det.agency_gps_flag", "gps"),
        ("rm.det.form_address", "Stráň"),
        ("rm.det.map_address", "Stráň, Potůčky, okres Karlovy Vary"),
        ("rm.det.map_obec", "Potůčky"),
        ("rm.det.map_okres", "Karlovy Vary"),
        ("rm.det.breadcrumb_kraj", "Karlovarský kraj"),
        ("rm.det.breadcrumb_geo", "Karlovy Vary"),
        ("rm.det.breadcrumb_obec", "Potůčky"),
        ("rm.det.breadcrumb_quarter", "Stráň"),
    }
    assert result.absences == [] and result.oversized == 0
    assert all(c.surface == "archived_html" for c in result.claims)
    assert all(c.licence_class == "portal" for c in result.claims)


def test_every_archived_claim_carries_a_resolvable_evidence_span():
    """Migration 382's `loc_claim_text_evidence` refuses an evidence-bearing claim with no
    span, and a span that does not contain its quote is worse than none."""
    doc = document()
    result = extract_payload(payload(), row(), list(ENTRIES.values()), register=REGISTER)
    for claim in result.claims:
        assert claim.evidence_quote, claim.extractor_id
        assert claim.span_start is not None, claim.extractor_id
        assert doc.html[claim.span_start:claim.span_end] == claim.evidence_quote


def test_a_listing_in_the_mapy_inventory_records_an_absence_not_a_silence():
    result = extract_payload(payload(), row(in_mapy_inventory=True),
                             list(ENTRIES.values()), register=REGISTER)
    assert [a.field_ for a in result.absences] == ["coordinate"]
    assert "rm.det.gps" not in {c.extractor_id for c in result.claims}


def test_an_incomplete_scope_admits_nothing_and_says_so():
    """The scoper fails closed: "the boundary had a hole" may never read as "no zones
    matched, extract freely"."""
    broken = scope_html(_ARCHIVED_BODY.read_bytes(),
                        register=register_for(list(CONTRACT.exclusion_zones) + [
                            {"locator_kind": "html_selector",
                             "locator": {"css": "div:has(> :not(*))"},
                             "reason": "a selector html_scope cannot compile"}]))
    if broken.is_complete:  # pragma: no cover - the compiler accepted it after all
        pytest.skip("the scoper compiled the deliberately broken selector")
    result = extract_payload(payload(), row(), list(ENTRIES.values()),
                             register=broken.register)
    assert result.claims == [] and result.absences


# ------------------------------------------------------------------ the modelled fixture

def test_the_modelled_fixture_states_a_shape_this_portal_actually_serves():
    """The golden's archived arm scores the modelled page, so its `data-address` has to be a
    comma address ("street, obvod, obec, okres X") and not the admin chain v3 modelled — the
    admin chain made the split entries certify obec="Plzeň 2-Slovany" and městský
    obvod="okres Plzeň-město", neither of which realitymix ever emits."""
    doc = scope_html(_PINNED_BODY.read_bytes(), register=REGISTER)
    assert value("rm.det.map_street", doc) == "Slovanská alej"
    assert value("rm.det.map_obec", doc) == "Plzeň"
    assert value("rm.det.map_okres", doc) == "Plzeň-město"
    assert value("rm.det.address_all_segments", doc) == "Plzeň 2-Slovany"
    assert value("rm.det.form_address", doc) == "Slovany"
