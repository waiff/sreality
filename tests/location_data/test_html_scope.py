"""D7's security boundary, asserted in both directions on every portal.

06 §6.2.3 makes excluded-block scoping mandatory for the deterministic re-mine
and calls it a security boundary; the failure it exists to prevent is measured,
not theoretical — remax's neighbour-carousel `data-address` reached
`listings.street` on 2 rows, and the re-miner would repeat that on 445,191 pages.

So EVERY decoy assertion here is paired with a subject assertion. A scoper that
strips the whole page also makes the decoy unreachable and is not a fix; the two
directions together are the only statement worth making. Where a real archived
page exists in this repo (remax, idnes, realitymix under
`tests/fixtures/portal_html/`) the assertions run against it; the rest of
`tests/fixtures/location_w2/` is modelled on each contract's own W2 locators plus
that contract's `exclusion_zones` block, so a fixture can never assert more than
the register actually says.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from selectolax.lexbor import LexborHTMLParser

from location_data import contracts, html_scope
from location_data.html_scope import (
    DOM,
    GUARD_EXCLUDED_ZONE,
    PAYLOAD,
    SCOPER_VERSION,
    TEXT,
    UNSUPPORTED,
    ScopeError,
    ScopedDocument,
    ScopeRegister,
    excluded_zone_admits,
    payload_scope_version,
    scope_html,
    scope_json,
)

_ROOT = Path(__file__).resolve().parents[2]
_W2 = _ROOT / "tests" / "fixtures" / "location_w2"
_ARCHIVED = _ROOT / "tests" / "fixtures" / "portal_html"


def _raw_zones() -> dict[str, list[dict[str, Any]]]:
    return {c.source: list(c.exclusion_zones) for c in contracts.load_all()}


def _registers() -> dict[str, ScopeRegister]:
    return {
        source: ScopeRegister.from_zones(source, zones)
        for source, zones in _raw_zones().items()
    }


RAW_ZONES = _raw_zones()
REGISTERS = _registers()


def _widened(portal: str, selector: str) -> ScopeRegister:
    """The portal's shipped register plus one more DOM zone — the shape a contract
    version bump would take, expressed without editing an immutable YAML."""
    return ScopeRegister.from_zones(portal, [
        *RAW_ZONES[portal],
        {"locator_kind": "html_selector", "locator": {"css": selector},
         "reason": "W2 register gap, pinned by tests/location_data/test_html_scope.py"},
    ])


def _scoped(portal: str, fixture: Path | None = None) -> ScopedDocument:
    body = (fixture or (_W2 / f"{portal}_detail.html")).read_bytes()
    return scope_html(body, register=REGISTERS[portal])


# ------------------------------------------------------------------ the register


def test_every_portal_register_classifies_and_every_dom_selector_compiles() -> None:
    """A zone the engine cannot compile is a hole in the boundary, not a warning."""
    probe = "<html><body><div>x</div></body></html>"
    for source, register in REGISTERS.items():
        assert register.zones, f"{source} ships no exclusion zones"
        for zone in register.zones:
            assert zone.disposition != UNSUPPORTED, (source, zone)
        scoped = scope_html(probe, register=register)
        assert scoped.unsupported_selectors == (), (source, scoped.unsupported_selectors)
        assert scoped.is_complete


def test_the_guard_name_is_the_one_the_contracts_already_spell() -> None:
    """sreality's grandfathered-inert entry names it; the re-mine lane implements it."""
    assert GUARD_EXCLUDED_ZONE in contracts.GRANDFATHERED_INERT_GUARDS["sr.det.inaccuracy_type"]
    assert GUARD_EXCLUDED_ZONE not in contracts.IMPLEMENTED_GUARDS, (
        "`contracts.IMPLEMENTED_GUARDS` mirrors `claims_intake.GUARDS`, whose members "
        "are coordinate predicates; the document guard is wired by the re-mine lane")


def test_an_uncompilable_or_addressless_zone_is_classified_not_raised() -> None:
    register = ScopeRegister.from_zones("bazos", [
        {"locator_kind": "description", "locator": {"pattern": "Regus("}},
        {"locator_kind": "html_selector", "locator": {}, "reason": "addresses nothing"},
    ])

    assert [z.disposition for z in register.zones] == [UNSUPPORTED, UNSUPPORTED]
    assert register.text_patterns == ()
    assert register.dom_selectors == ()


def test_a_malformed_register_entry_raises_where_it_can_be_fixed() -> None:
    with pytest.raises(ScopeError):
        ScopeRegister.from_zones("bazos", [{"locator_kind": "html_selector",
                                            "locator": ["not", "a", "mapping"]}])


def test_a_qualified_zone_is_deferred_and_never_a_dom_strip() -> None:
    """The three zones whose node also carries the subject (02 §2.1.4)."""
    dispositions = {
        (source, zone.locator_kind, zone.selector, zone.json_pointer): zone.disposition
        for source, register in REGISTERS.items()
        for zone in register.zones
    }
    assert dispositions[
        ("idnes", "embedded_json", "script[data-maptiler-json]", None)] == PAYLOAD
    assert dispositions[("mmreality", "embedded_json", "[\\:property]", None)] == PAYLOAD
    assert dispositions[("mmreality", "embedded_json", "[\\:locations]", None)] == DOM
    assert dispositions[
        ("ceskereality", "jsonld", None, "/offers/offeredby/address")] == PAYLOAD
    assert dispositions[("sreality", "api_json", None, "/premise")] == PAYLOAD
    assert dispositions[("bezrealitky", "description", None, None)] == TEXT


def test_a_predicate_is_not_a_json_pointer_and_never_pretends_to_be_one() -> None:
    """`/geojson/features[isSimilar=true]` selects BY VALUE; no pop can honour it.

    Classifying it as poppable is what let `scope_json` hand back a payload that
    had silently kept every neighbour address while reporting a clean scope. It
    stays a PAYLOAD zone — the deferred reader still owes it — but it is carried
    as a `narrowing`, and a narrowing no engine can execute makes the payload
    INCOMPLETE.
    """
    idnes = {zone.selector: zone for zone in REGISTERS["idnes"].zones}
    zone = idnes["script[data-maptiler-json]"]

    assert zone.disposition == PAYLOAD
    assert zone.json_pointer is None
    assert zone.narrowing == "then=/geojson/features[isSimilar=true]"
    assert "/geojson/features[isSimilar=true]" not in REGISTERS["idnes"].payload_pointers
    assert REGISTERS["idnes"].unhonourable_payload_zones == (zone,)

    mmreality = REGISTERS["mmreality"].unhonourable_payload_zones
    assert [z.narrowing for z in mmreality] == ["scope=non_subject_blobs"]

    for source in ("sreality", "ceskereality", "bezrealitky", "remax"):
        assert REGISTERS[source].unhonourable_payload_zones == (), source


# ------------------------------------------------------------------ remax


def test_the_remax_carousel_decoy_is_unreachable_on_a_real_archived_page() -> None:
    """The confirmed contaminator, on the real page it was confirmed on.

    `rx.idx.gps` selects a bare `[data-gps]` and the parser took the first
    `data-address` in the document — both of which are a neighbour card's on this
    page. After scoping neither is in the tree at all.
    """
    scoped = _scoped("remax", _ARCHIVED / "remax_detail.html")

    assert not scoped.contains("Oleška, okres Praha-východ")
    assert not scoped.contains("Havlíčkova, Stará Boleslav")
    assert scoped.css(".area-listings__item") == []
    assert not scoped.contains("49°59'01.5\"N,14°54'28.4\"E")   # the whole coordinate
    assert scoped.css("[data-address]") == []
    assert not excluded_zone_admits(scoped, "Oleška, okres Praha-východ")

    header = scoped.css_first("h2.pd-header__address")
    assert header is not None and "Pod Slovany" in header.text()
    subject_pin = scoped.css_first("#printMap[data-gps], #listingMap[data-gps]")
    assert subject_pin is not None
    assert subject_pin.attributes["data-gps"] == "50°03'46.7\"N,14°43'41.5\"E"
    assert excluded_zone_admits(scoped, "ulice Pod Slovany")
    assert scoped.nodes_removed == 5


def test_every_carousel_coordinate_on_the_real_page_is_refused_and_the_subjects_is_not() -> None:
    """The BLOCKER this module exists for, at the exact value grain that failed.

    `rx.idx.gps` reads `[data-gps]`, and every remax coordinate is
    `50°03'46.7"N,14°43'41.5"E`-shaped — it CONTAINS a double quote. The
    serialisation of the stripped card spells that `&quot;`, so a guard that
    compared the reader's value against the removed markup could never match a
    coordinate to the card it came from and admitted all five neighbours.

    The decoys are harvested from the archived bytes rather than typed here, so
    the test cannot drift away from the page it is about.
    """
    body = (_ARCHIVED / "remax_detail.html").read_bytes()
    raw = LexborHTMLParser(body.decode("utf-8"))
    decoys = [node.attributes["data-gps"]
              for node in raw.css(".area-listings__item[data-gps]")]
    subject = raw.css_first("#printMap[data-gps]").attributes["data-gps"]

    assert len(decoys) == 5 and subject not in decoys
    assert all('"' in coordinate for coordinate in (subject, *decoys))

    scoped = scope_html(body, register=REGISTERS["remax"])

    for coordinate in decoys:
        assert not scoped.contains(coordinate), coordinate
        assert not excluded_zone_admits(scoped, coordinate), coordinate
    assert scoped.contains(subject)
    assert excluded_zone_admits(scoped, subject)
    assert scoped.css_first("#printMap[data-gps]").attributes["data-gps"] == subject


def test_contains_reads_an_attribute_value_the_serialisation_would_escape() -> None:
    """`contains()` is a reachability check readers use; it has to answer in the
    form a reader holds the value — decoded, as `node.attributes[...]` returns it."""
    scoped = _scoped("remax", _ARCHIVED / "remax_detail.html")
    gps = scoped.css_first("#printMap[data-gps]").attributes["data-gps"]

    assert '"' in gps
    assert gps not in scoped.html          # the serialisation spells it `&quot;`
    assert scoped.contains(gps)


def test_remax_broker_block_and_footer_country_list_go_with_the_subject_intact() -> None:
    scoped = _scoped("remax")

    assert not scoped.contains("Samcova 1177/1")      # the sole PSČ on 12/12 pages
    assert not scoped.contains("110 00")
    assert not scoped.contains("Bulgaria")            # the footer country list
    assert not scoped.contains("Oleška, okres Praha-východ")

    assert scoped.contains("ulice Pod Slovany, Úvaly")
    assert scoped.css_first("#printMap") is not None
    assert scoped.css_first(".pd-header__location") is not None


# ------------------------------------------------------------------ bazos


def test_bazos_podobne_inzeraty_and_footer_go_with_the_subject_intact() -> None:
    scoped = _scoped("bazos")

    for decoy in ("gen. Píky", "Lidická 8/22", "Peškova", "Norská",
                  "Klimentská 1216/46", "110 00"):
        assert not scoped.contains(decoy), decoy
        assert not excluded_zone_admits(scoped, decoy), decoy

    assert scoped.contains("Sokolovská 234")
    assert scoped.contains("/inzeraty/praha-8/18600/")   # bzs.det.obec_slug
    assert scoped.contains("okres Praha")                # bzs.det.okres_text
    assert scoped.css_first("a[href*='/place/']").attributes["title"] == "Přibližná lokalita"
    assert excluded_zone_admits(scoped, "Sokolovská 234")


# ------------------------------------------------------------------ ceskereality


def test_ceskereality_operator_footer_and_similar_block_go_and_the_title_survives() -> None:
    scoped = _scoped("ceskereality")

    for decoy in ("Kostelní 942/46", "370 04", "Nová 118",
                  "Rudolfovská", "Puklicova", "Zahraniční nemovitosti"):
        assert not scoped.contains(decoy), decoy

    title = scoped.css_first("title")
    assert title is not None and "ulice Nádražní" in title.text()   # accented, cr.det.title_line
    assert scoped.css_first("#driving_calculator_from").attributes["data-city"] == "České Budějovice"
    markers = json.loads(scoped.css_first("#mapCanvas").attributes["data-markers"])
    assert markers["data"][0]["nid"] == 3790435                      # cr.map.coordinate


def test_the_ceskereality_jsonld_script_survives_and_the_office_is_stripped_in_json() -> None:
    """`offeredby.address` is inside the script that also carries the BreadcrumbList.

    Removing the node would take `cr.det.breadcrumb` with it, so the zone is
    deferred to the reader that can address `/offers/offeredby/address` — and
    `scope_json` is that reader's boundary.
    """
    scoped = _scoped("ceskereality")
    assert len(scoped.css("script[type='application/ld+json']")) == 2
    assert scoped.contains("Lannova 1234/5")           # still in the DOM, by design

    product = json.loads(scoped.css("script[type='application/ld+json']")[1].text())
    payload = scope_json(product, register=REGISTERS["ceskereality"])

    assert payload.removed_pointers == ("/offers/offeredby/address",)
    assert not payload.contains("Lannova 1234/5")
    assert not excluded_zone_admits(payload, "Lannova 1234/5")
    assert payload.contains("České Budějovice")        # cr.det.jsonld_area_served
    assert payload.data["offers"]["offeredby"]["name"] == "REALITY ČB s.r.o."


# ------------------------------------------------------------------ idnes


def test_idnes_similar_and_broker_blocks_go_but_the_maptiler_script_survives() -> None:
    """The subject Point lives in the same script as the 20 neighbour features."""
    scoped = _scoped("idnes")

    assert not scoped.contains("Údolní 44")
    assert not scoped.contains("Krakovská 1675/2")
    assert not scoped.contains("Zahraniční nemovitosti")

    script = scoped.css_first("script[data-maptiler-json]")
    assert script is not None
    config = json.loads(script.text())
    features = config["geojson"]["features"]
    subject = [f for f in features if not f["properties"]["isSimilar"]]
    assert len(subject) == 1
    assert subject[0]["geometry"]["coordinates"] == [15.31331632, 50.74437214]
    assert any(f["properties"]["isSimilar"] for f in features), (
        "the neighbour features must survive the DOM strip — they are the deferred "
        "zone the W2 idnes reader applies, not something this scoper may delete")

    assert scoped.contains("Na Balkáně")               # id.det.locality_line
    assert scoped.contains("Nemovitost nemá přesnou adresu")   # id.det.no_exact_disclaimer


def test_the_idnes_nav_strip_leaves_the_real_pages_subject_signals_alone() -> None:
    scoped = _scoped("idnes", _ARCHIVED / "idnes_detail.html")

    assert scoped.css_first("script[data-maptiler-json]") is not None
    assert scoped.contains("Na Balkáně")
    info = scoped.css_first(".b-detail__info")
    assert info is not None and "Tanvald" in info.text()
    assert scoped.nodes_removed >= 1


# ------------------------------------------------------------------ mmreality


def test_mmreality_neighbour_blob_goes_and_the_subject_property_blob_survives() -> None:
    scoped = _scoped("mmreality")

    assert scoped.css("[\\:locations]") == []
    for decoy in ("Vrchlického", "Dragounská", "Řípská 20", "Zahraniční nemovitosti"):
        assert not scoped.contains(decoy), decoy
        assert not excluded_zone_admits(scoped, decoy), decoy

    subject = scoped.css_first("[\\:property]")
    assert subject is not None, (
        "`[\\\\:property]` is scoped `non_subject_blobs`; stripping it would delete "
        "mm.det.point, mm.det.street and mm.det.municipality_id along with the decoys")
    blob = json.loads(subject.attributes[":property"])
    assert blob["street"] == "Křižíkova"
    assert blob["point"] == {"latitude": 50.09239, "longitude": 14.45118}
    assert excluded_zone_admits(scoped, "ul. Křižíkova")


# ------------------------------------------------------------------ maxima


def test_maxima_similar_block_goes_and_the_openlayers_config_survives() -> None:
    scoped = _scoped("maxima")

    for decoy in ("Kounicova 42", "Nerudova 7", "Palackého třída 118"):
        assert not scoped.contains(decoy), decoy

    assert scoped.contains("Brno, Brno-střed, Veveří")     # mx.det.locality
    assert scoped.contains('"coordinates":[16.60411,49.20256]')   # mx.det.map_features
    assert scoped.css_first("div.locality") is not None


# ------------------------------------------------------------------ realitymix


def test_realitymix_broker_and_operator_addresses_go_with_the_subject_intact() -> None:
    scoped = _scoped("realitymix")

    for decoy in ("Jiráskovo náměstí 2684/2", "Okružní 3407/11", "434 01",
                  "Na Harfě 916/9a", "190 00", "Zahraniční nemovitosti"):
        assert not scoped.contains(decoy), decoy

    pin = scoped.css_first("div#print-map")
    assert pin is not None
    assert pin.attributes["data-gps-lat"] == "49.73561"
    # v4 restated the fixture's `data-address` to the shape the portal serves — a comma
    # address ending in the okres qualifier, not the admin chain kraj→okres→obec→quarter.
    assert pin.attributes["data-address"].endswith("okres Plzeň-město")
    assert scoped.contains("Slovanská alej")
    assert len(scoped.css("script[type='application/ld+json']")) == 1
    # The v4 carrier for `rm.det.form_address`: a div, not the <input> v3 declared.
    assert scoped.css_first("[data-advert-detail-contact-form][data-form-address]") is not None


def test_a_node_from_a_raw_parse_is_not_owned_by_the_scoped_document() -> None:
    """The guard at node grain — the raw body is not a substrate extraction may use."""
    body = (_ARCHIVED / "remax_detail.html").read_bytes()
    scoped = _scoped("remax", _ARCHIVED / "remax_detail.html")
    unscoped = LexborHTMLParser(body.decode("utf-8"))

    assert scoped.owns(scoped.css_first("h2.pd-header__address"))
    assert not scoped.owns(unscoped.css_first("[data-address]"))
    assert not scoped.owns(unscoped.css_first("h2.pd-header__address"))


def test_a_value_that_is_also_reachable_outside_a_zone_is_still_admitted() -> None:
    """The guard rejects what could ONLY have come from a zone, nothing wider.

    The broker office shares the subject's PSČ, which is ordinary — the subject's
    own `og:title` publishes it, so the claim has a licit source and rejecting it
    would cost real yield.
    """
    scoped = _scoped("realitymix")

    assert scoped.contains("326 00")
    assert excluded_zone_admits(scoped, "326 00")
    assert not excluded_zone_admits(scoped, "Okružní 3407/11")


# ------------------------------------------------------------------ sreality / json


def test_sreality_premise_block_is_unreachable_and_the_subject_locality_survives() -> None:
    """sreality has no archived HTML; its decoy is `/premise` in the JSON payload."""
    raw = json.loads((_W2 / "sreality_detail.json").read_text(encoding="utf-8"))
    payload = scope_json(raw, register=REGISTERS["sreality"])

    assert payload.removed_pointers == ("/premise", "/labels_extended")
    assert not payload.contains("Korunní 810/104")
    assert not payload.contains("101 00")
    assert not payload.contains("Park Riegrovy sady")
    assert not excluded_zone_admits(payload, "Korunní 810/104")

    assert payload.data["locality"]["street"] == "Vinohradská"
    assert payload.data["locality"]["gps_lat"] == 50.0776
    assert payload.contains("120 00")
    assert excluded_zone_admits(payload, "Vinohradská")


def test_scope_json_never_mutates_its_input() -> None:
    raw = json.loads((_W2 / "sreality_detail.json").read_text(encoding="utf-8"))
    before = json.dumps(raw, sort_keys=True)

    scope_json(raw, register=REGISTERS["sreality"])

    assert json.dumps(raw, sort_keys=True) == before


def test_a_missing_pointer_is_a_no_op_not_a_failure() -> None:
    """Absent from THIS payload is not the same as unhonourable: nothing to remove."""
    payload = scope_json({"locality": {"street": "Vinohradská"}},
                         register=REGISTERS["sreality"])

    assert payload.removed_pointers == ()
    assert payload.data == {"locality": {"street": "Vinohradská"}}
    assert payload.is_complete
    assert excluded_zone_admits(payload, "Vinohradská")


def test_the_idnes_neighbour_features_survive_the_dom_and_close_the_json_reader() -> None:
    """The PR's own headline hard case, asserted in the direction that can hurt.

    `then: /geojson/features[isSimilar=true]` is a predicate, so `scope_json`
    cannot pop it — and the 20 neighbour addresses are sitting in the payload it
    hands back. The one safe answer is to admit nothing until the reader that CAN
    address the sub-document has applied the zone.
    """
    scoped = _scoped("idnes")
    config = json.loads(scoped.css_first("script[data-maptiler-json]").text())
    neighbours = [f for f in config["geojson"]["features"] if f["properties"]["isSimilar"]]
    assert neighbours, "the deferred zone's decoys must reach the JSON reader intact"
    decoy = neighbours[0]["properties"]["address"]

    payload = scope_json(config, register=REGISTERS["idnes"])

    assert payload.unsupported_pointers == ("then=/geojson/features[isSimilar=true]",)
    assert not payload.is_complete
    assert payload.contains(decoy), "the decoy is still IN the payload — that is the point"
    assert not excluded_zone_admits(payload, decoy)
    assert not excluded_zone_admits(payload, "Na Balkáně")   # the subject too: fail closed


def test_the_mmreality_non_subject_blob_rule_also_closes_the_json_reader() -> None:
    """`scope: non_subject_blobs` is a rule about WHICH blob, not a pointer."""
    scoped = _scoped("mmreality")
    blob = json.loads(scoped.css_first("[\\:property]").attributes[":property"])

    payload = scope_json(blob, register=REGISTERS["mmreality"])

    assert payload.unsupported_pointers == ("scope=non_subject_blobs",)
    assert not payload.is_complete
    assert not excluded_zone_admits(payload, "Křižíkova")


def test_the_bezrealitky_text_zone_rejects_the_regus_boilerplate() -> None:
    """The one register entry with no node and no pointer — a description pattern."""
    register = REGISTERS["bezrealitky"]
    payload = scope_json({"description": "Byt v Karlíně."}, register=register)

    assert register.text_patterns == ("Regus|IWG|globální síť",)
    assert not excluded_zone_admits(payload, "Regus, globální síť kanceláří")
    assert excluded_zone_admits(payload, "Karlín")


# ------------------------------------------------------------------ scope version


def test_payload_scope_version_changes_with_the_register_and_only_with_it() -> None:
    zones = [{"locator_kind": "html_selector", "locator": {"css": ".similar"},
              "reason": "neighbours"}]
    base = payload_scope_version("remax", zones)

    assert base.startswith(f"{SCOPER_VERSION}:remax:")
    assert payload_scope_version("remax", list(zones)) == base
    assert payload_scope_version("bazos", zones) != base
    assert payload_scope_version(
        "remax", zones + [{"locator_kind": "html_selector",
                           "locator": {"css": "footer"}, "reason": "chrome"}]) != base
    assert payload_scope_version(
        "remax", [{"locator_kind": "html_selector", "locator": {"css": ".similar"},
                   "reason": "neighbour cards"}]) != base, (
        "a reason edit is already a contract_version bump; the scope stamp has to "
        "move with it or a claim cannot resolve to the register bytes that scoped it")


def test_payload_scope_version_survives_the_jsonb_round_trip() -> None:
    """YAML parse and `portal_contracts.exclusion_zones` must hash identically."""
    zones = [{"locator_kind": "html_selector", "reason": "x", "locator": {"css": "footer"}}]
    reordered = [{"reason": "x", "locator": {"css": "footer"},
                  "locator_kind": "html_selector"}]

    assert payload_scope_version("bazos", zones) == payload_scope_version("bazos", reordered)


def test_every_portal_gets_a_distinct_stable_stamp() -> None:
    stamps = {source: register.scope_version for source, register in REGISTERS.items()}

    assert len(set(stamps.values())) == len(stamps)
    assert stamps == {source: register.scope_version
                      for source, register in _registers().items()}


def test_the_scoped_document_carries_the_stamp_of_the_register_that_made_it() -> None:
    scoped = _scoped("remax")

    assert scoped.scope_version == REGISTERS["remax"].scope_version
    assert scoped.source == "remax"


# ------------------------------------------------------------------ robustness


def test_nested_and_overlapping_zones_remove_the_outermost_exactly_once() -> None:
    """Two ways to double-free, both fatal to the boundary.

    The register's own remax zone selects the same card through `[data-address]`
    and through `[data-gps]`; decomposing it twice corrupted the tree so badly
    that the card's coordinates read back out of the document it had been removed
    from. A zone nested inside another zone is the same bug by a different route.
    """
    register = ScopeRegister.from_zones("remax", [
        {"locator_kind": "html_selector", "locator": {"css": ".outer"}},
        {"locator_kind": "html_selector", "locator": {"css": ".inner, .outer"}},
        {"locator_kind": "html_selector", "locator": {"css": "div[data-x]"}},
    ])
    body = ("<html><body><div class='outer' data-x='1'>"
            "<div class='inner'>DECOY</div></div><p>SUBJECT</p></body></html>")

    scoped = scope_html(body, register=register)

    assert scoped.nodes_removed == 1
    assert not scoped.contains("DECOY")
    assert scoped.contains("SUBJECT")
    assert scoped.css(".outer") == []


@pytest.mark.parametrize("body", [
    b"",
    b"<html><body><div class='podobne'>gen. Pi",          # truncated mid-node
    b"\x00\x01\x02\xff\xfe not html at all",
    "<html><body>".encode("utf-16"),                       # undecodable as utf-8
])
def test_a_malformed_or_truncated_body_degrades_instead_of_raising(body: bytes) -> None:
    scoped = scope_html(body, register=REGISTERS["bazos"])

    assert isinstance(scoped.html, str)
    assert isinstance(scoped.css("div"), list)
    assert not scoped.contains("gen. Pi")


def test_a_body_the_parser_refuses_fails_closed() -> None:
    """No scoped document means nothing can be SHOWN to be outside a zone."""
    class Unreadable:
        def __bytes__(self) -> bytes:
            raise ValueError("boom")

    scoped = scope_html(Unreadable(), register=REGISTERS["bazos"])  # type: ignore[arg-type]

    assert scoped.parse_failed
    assert not scoped.is_complete
    assert scoped.html == ""
    assert not excluded_zone_admits(scoped, "Sokolovská 234")
    assert scoped.css("div") == []


def test_an_uncompilable_selector_marks_the_document_incomplete_without_raising() -> None:
    """One zone the engine will not compile is one zone STILL STANDING.

    The subtree it named is in the tree, reachable by every other selector, so
    `admits` must refuse everything on this document — including the value the
    unapplied zone was meant to cover. Anything else is a boundary that opens
    itself the moment a future contract PR ships one malformed selector.
    """
    register = ScopeRegister.from_zones("bazos", [
        {"locator_kind": "html_selector", "locator": {"css": ".podobne"}},
        {"locator_kind": "html_selector", "locator": {"css": "]not[[a selector"}},
    ])

    scoped = scope_html(
        "<html><body><div class='podobne'>x</div>"
        "<div class='broker'>Samcova 1177/1, 110 00</div><p>y</p></body></html>",
        register=register)

    assert scoped.unsupported_selectors == ("]not[[a selector",)
    assert not scoped.is_complete
    assert not scoped.contains("x")
    assert scoped.contains("y")
    assert not excluded_zone_admits(scoped, "Samcova 1177/1")
    assert not excluded_zone_admits(scoped, "y")


class _Undecomposable:
    """A node that quotes fine and refuses to be removed."""

    def __init__(self, node: Any) -> None:
        self._node = node

    def __getattr__(self, name: str) -> Any:
        return getattr(self._node, name)

    def decompose(self) -> None:
        raise RuntimeError("lexbor said no")


class _FailingStripTree:
    """A tree whose every zone match refuses `decompose()`."""

    def __init__(self, tree: Any) -> None:
        self._tree = tree
        self.root = tree.root

    def css(self, selector: str) -> list[Any]:
        return [_Undecomposable(node) for node in self._tree.css(selector)]

    @property
    def html(self) -> str:
        return self._tree.html

    def text(self) -> str:
        return self._tree.text()


def test_a_strip_that_raises_fails_the_guard_closed_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other way a declared zone stays standing: `decompose()` raised.

    `strip_failures` is the count, and it has to reach the guard — a zone that
    was matched but not removed is exactly as open as one that never compiled.
    """
    register = ScopeRegister.from_zones("bazos", [
        {"locator_kind": "html_selector", "locator": {"css": ".podobne"}}])
    body = ("<html><body><div class='podobne'>gen. Píky 12</div>"
            "<p>Sokolovská 234</p></body></html>")

    clean = scope_html(body, register=register)
    assert clean.is_complete and clean.strip_failures == 0
    assert excluded_zone_admits(clean, "Sokolovská 234")

    monkeypatch.setattr(
        html_scope, "_PARSER", lambda text: _FailingStripTree(LexborHTMLParser(text)))
    broken = scope_html(body, register=register)

    assert broken.strip_failures == 1
    assert broken.nodes_removed == 0
    assert not broken.is_complete
    assert broken.contains("gen. Píky 12")                 # the zone is still standing
    assert not excluded_zone_admits(broken, "gen. Píky 12")
    assert not excluded_zone_admits(broken, "Sokolovská 234")


def test_an_uncompilable_extraction_selector_raises_rather_than_reading_as_absent() -> None:
    """A broken extractor and an absent field are different facts.

    Swallowing the compile error into `[]` records "this page has no such value"
    (class E) when what happened is "the reader is broken", and the fixture-diff
    gate cannot tell those two apart. Register selectors take the opposite call —
    those are classified, so one bad zone cannot make a page unscopeable.
    """
    scoped = _scoped("remax")

    with pytest.raises(ScopeError):
        scoped.css("div >>> span")
    with pytest.raises(ScopeError):
        scoped.css_first("]not[[a selector")
    assert scoped.css("h2.pd-header__address")


def test_the_empty_register_scopes_nothing_and_still_stamps() -> None:
    register = ScopeRegister.from_zones("maxima", [])

    scoped = scope_html("<html><body><p>SUBJECT</p></body></html>", register=register)

    assert scoped.nodes_removed == 0
    assert scoped.contains("SUBJECT")
    assert scoped.scope_version.startswith(f"{SCOPER_VERSION}:maxima:")


# ------------------------------------------------------------------ evidence spans


def test_find_span_points_into_the_scoped_document() -> None:
    """01 §4.2: a span is meaningless without the document it indexes into, and
    migration 382 makes that document the SCOPED payload."""
    scoped = _scoped("remax")

    span = scoped.find_span("ulice Pod Slovany, Úvaly")
    assert span is not None
    start, end = span
    # W2-6 put the real archived header block into this fixture, so the span indexes the
    # UNCOLLAPSED source — the portal breaks that one line across two, with a tab run
    # between them. The quote is the collapsed value and the span is where it was read;
    # the two are the same text, not the same bytes (`_span_pattern`'s whole purpose).
    quoted = scoped.html[start:end]
    assert " ".join(quoted.split()) == "ulice Pod Slovany, Úvaly"
    assert scoped.find_span("Oleška, okres Praha-východ") is None


def test_find_span_tolerates_the_whitespace_a_text_read_collapses() -> None:
    """The real page indents the header across four tabs and a newline."""
    scoped = _scoped("remax", _ARCHIVED / "remax_detail.html")
    quote = "ulice Pod Slovany, Úvaly"

    span = scoped.find_span(quote)

    assert scoped.html.find(quote) == -1
    assert span is not None
    assert " ".join(scoped.html[span[0]:span[1]].split()) == quote


def test_find_span_tolerates_the_entities_a_text_read_resolves() -> None:
    """`&nbsp;` is six characters in the source and one in the quote.

    Migration 382 refuses an evidence-bearing claim with no span
    (`loc_claim_text_evidence`), so a quote crossing an `&nbsp;` or an `&amp;` —
    which is most Czech agency names and every `190 00` PSČ on realitymix —
    silently lost its evidence instead of recording it.
    """
    scoped = scope_html(
        "<html><body><p id='a'>Molík reality s.r.o. &amp; syn, Okružní 3407/11</p>"
        "<p id='b'>Praha&nbsp;9, 190&nbsp;00</p></body></html>",
        register=ScopeRegister.from_zones("realitymix", []))

    for selector in ("#a", "#b"):
        quote = scoped.css_first(selector).text()
        span = scoped.find_span(quote)

        assert scoped.contains(quote), quote
        assert span is not None, quote
        assert scoped.html[span[0]:span[1]].startswith(quote.split()[0])
        # 382 checks the quote against the payload the span indexes into; the
        # source run is LONGER than the quote because the entities are spelled out.
        assert span[1] - span[0] > len(quote)


def test_find_span_finds_a_real_archived_quote_that_crosses_an_nbsp() -> None:
    """Not a constructed fixture: `&nbsp;` is pervasive in the archive."""
    scoped = _scoped("realitymix", _ARCHIVED / "realitymix_detail.html")
    node = next(n for n in scoped.css("div, p, span, li")
                if "\xa0" in n.text(deep=False) and 8 < len(n.text(deep=False).strip()) < 90)
    quote = node.text(deep=False).strip()

    span = scoped.find_span(quote, within=node)

    assert scoped.html.find(quote) == -1, "the source spells the NBSP as an entity"
    assert span is not None
    assert "&nbsp;" in scoped.html[span[0]:span[1]]


def test_find_span_can_be_anchored_to_the_node_the_claim_came_from() -> None:
    """A span is evidence. Pointing it at the `<title>` is a wrong answer that
    still passes 382's substring check, which is worse than no answer at all."""
    scoped = _scoped("remax")
    header = scoped.css_first("h2.pd-header__address")

    loose = scoped.find_span("Pod Slovany")
    anchored = scoped.find_span("Pod Slovany", within=header)

    assert loose is not None and anchored is not None and anchored != loose
    assert _encloses(scoped, scoped.css_first("title"), loose), (
        "unanchored, the first textual occurrence is the page title")
    assert _encloses(scoped, header, anchored)
    assert scoped.html[anchored[0]:anchored[1]] == "Pod Slovany"
    assert scoped.find_span("Úvaly u Prahy", within=header) is None


def _encloses(scoped: Any, node: Any, span: tuple[int, int]) -> bool:
    offset = scoped.html.find(node.html)
    return 0 <= offset <= span[0] and span[1] <= offset + len(node.html)


# ------------------------------------------------------------------ known gaps


# Zones the register DECLARES but whose selector does not match the markup on the
# real archived page in this repo. Not edited here: contract entries are immutable
# (02 §2.1.8) and a register change is a `contract_version` bump made in the
# portal's own W2 PR. Each row pins the hole AND the one-line YAML fix, so the
# per-portal PR has a test that flips the moment it lands.
REGISTER_GAPS = (
    ("remax", "remax_detail.html", ".broker, .office",
     ".pd-sidebar__agent-info-print", "Samcova", "Pod Slovany"),
    ("remax", "remax_detail.html", ".area-listings__item[data-address], "
     ".area-listings__item[data-gps]", ".similar-property", "Velké Popovice",
     "Pod Slovany"),
    ("remax", "remax_detail.html", "footer", ".footer", "Bulgaria", "Pod Slovany"),
    ("idnes", "idnes_detail.html", ".b-similar, .broker, nav",
     ".grid-similar-offers", "Josefův Důl - Dolní Maxov", "Na Balkáně"),
    ("idnes", "idnes_detail.html", ".b-similar, .broker, nav",
     ".b-detail-contact", "Arbesova", "Na Balkáně"),
    # realitymix's row is GONE: contract v4 (the W2-8 activation) declared
    # `.offer-detail-sidebar__company` — plus the agent block, the similar-adverts carousel
    # and the footer — so the gap this table pinned is closed and the test's own failure
    # message ("drop this row from REGISTER_GAPS") is what removed it. The two v3 zones that
    # matched nothing are retained in the YAML and still enumerated by ZERO_MATCH_ZONES.
)

# Every archived page in this repo, and every declared zone COMPONENT that matches
# nothing on it. Derived from `ScopedDocument.zones_unmatched`, pinned here so the
# table above can never quietly fall behind the registers: a zone that goes inert,
# or a new register entry that never matched anything, fails this test instead of
# shipping as a hole nobody enumerated. A zero-match zone is not automatically a
# register BUG — realitymix's nav zone matches nothing because this capture has no
# `<nav>` at all — which is why the judgement stays in REGISTER_GAPS.
ARCHIVED_PAGES = (
    ("remax", "remax_detail.html"),
    ("idnes", "idnes_detail.html"),
    ("realitymix", "realitymix_detail.html"),
)
ZERO_MATCH_ZONES = {
    ("remax", "remax_detail.html"): (".broker", ".office", "footer"),
    ("idnes", "idnes_detail.html"): (".b-similar", ".broker"),
    ("realitymix", "realitymix_detail.html"): (
        ".broker-contact", ".contact-box", "nav a[href*='zahranicni']"),
}


def test_zero_match_zones_are_counted_and_enumerated_not_discovered_by_hand() -> None:
    """MINOR-7's counter, used as MINOR-8's gate.

    A zone that compiled and matched nothing is NOT incompleteness — a page may
    legitimately not carry the block, and refusing every claim on it would cost
    the whole corpus. It is a metric: "declared zone, 0 matches across N pages" is
    the shape of a register bug, and this is the assertion that surfaces one
    without a human re-reading nine registers against nine archived pages.
    """
    observed = {}
    for portal, fixture in ARCHIVED_PAGES:
        scoped = scope_html((_ARCHIVED / fixture).read_bytes(), register=REGISTERS[portal])
        assert scoped.is_complete, (portal, "a zero-match zone is not a hole")
        assert scoped.zone_matches, portal
        observed[(portal, fixture)] = scoped.zones_unmatched

    assert observed == ZERO_MATCH_ZONES
    for (portal, fixture), zones in observed.items():
        declared = " ".join(REGISTERS[portal].dom_selectors)
        for zone in zones:
            assert zone in declared, (portal, zone)


def test_a_zone_that_matches_is_counted_per_component() -> None:
    """`.b-similar, .broker, nav` matching twice hides that `.broker` matched zero."""
    scoped = _scoped("idnes", _ARCHIVED / "idnes_detail.html")

    assert dict(scoped.zone_matches) == {".b-similar": 0, ".broker": 0, "nav": 2}
    assert scoped.nodes_removed == 2


@pytest.mark.parametrize("portal,fixture,declared,proposed,decoy,subject", REGISTER_GAPS)
def test_known_register_gaps_are_pinned_with_their_one_line_fix(
    portal: str, fixture: str, declared: str, proposed: str, decoy: str, subject: str,
) -> None:
    """Asserted at NODE grain, not value grain.

    idnes's neighbour addresses reach the page twice — through this DOM block and
    through the maptiler features, which are a DEFERRED zone and must survive — so
    "is the string gone" cannot tell the two apart. "Is the block still standing"
    can.
    """
    body = (_ARCHIVED / fixture).read_bytes()
    shipped = REGISTERS[portal]
    assert declared in shipped.dom_selectors

    open_scope = scope_html(body, register=shipped)
    left_standing = open_scope.css(proposed)
    assert left_standing, (
        f"{portal}: the gap closed — the shipped register now removes {proposed}, so "
        f"drop this row from REGISTER_GAPS")
    assert any(decoy in node.text() for node in left_standing)

    closed = scope_html(body, register=_widened(portal, proposed))

    assert closed.css(proposed) == []
    assert closed.is_complete
    assert closed.contains(subject)
    assert closed.scope_version != shipped.scope_version


def test_the_gap_fix_keeps_the_subject_reachable() -> None:
    """Widening remax's register must not cost the subject header."""
    body = (_ARCHIVED / "remax_detail.html").read_bytes()

    scoped = scope_html(body, register=_widened("remax", ".pd-sidebar__agent-info-print"))

    assert "Pod Slovany" in scoped.css_first("h2.pd-header__address").text()
    assert scoped.css_first("#printMap[data-gps]") is not None
    assert not scoped.contains("Samcova")


# ------------------------------------------------------------------ purity


def test_the_scoper_is_pure() -> None:
    """It runs per listing inside a batch drain: no DB, no network, no clock."""
    module = _ROOT / "location_data" / "html_scope.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= {
        "__future__", "copy", "hashlib", "json", "re", "collections", "dataclasses",
        "typing", "selectolax",
    }, sorted(imported)


# Words a selector shares with ordinary Python prose; matching on them would flag
# the module's own vocabulary rather than a smuggled rule.
_GENERIC_SELECTOR_TOKENS = frozenset({
    "href", "name", "class", "type", "data", "text", "html", "json", "item", "link",
    "script", "select",
})


def _executable_literals(module_source: str) -> list[str]:
    """Every string literal in the module EXCEPT docstrings.

    Prose references are intended and load-bearing — the module has to be able to
    explain why mmreality's register forced the engine choice — so the guarantee
    is about strings the code can execute, not strings a reader can see.
    """
    tree = ast.parse(module_source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _selector_tokens(selector: str) -> set[str]:
    """The portal-specific fragments of a selector: classes, ids, attribute names
    and quoted attribute values. Bare element names (`nav`, `footer`) are left to
    the exact-match arm — they carry no portal in them."""
    tokens = set()
    tokens.update(re.findall(r"[.#]([A-Za-z_][\w-]*)", selector))
    tokens.update(re.findall(r"\[\\?:?([A-Za-z_][\w-]*)", selector))
    tokens.update(re.findall(r"=\s*['\"]([^'\"]+)['\"]", selector))
    return {t for t in tokens if len(t) >= 4 and t.lower() not in _GENERIC_SELECTOR_TOKENS}


def test_no_register_selector_or_fragment_of_one_is_hardcoded_in_the_scoper() -> None:
    """The register is contract data. A selector in Python would be a rule with no
    `contract_version`, invisible to the fixture-diff gate and unretractable.

    Exact equality alone was too weak to mean that: `".area-listings__item"` or
    `if "data-gps" in selector` are the shapes a smuggled rule actually takes, and
    neither is byte-identical to a declared selector. So both arms run — the
    declared strings themselves, and every portal-specific fragment of one.
    """
    source = (_ROOT / "location_data" / "html_scope.py").read_text(encoding="utf-8")
    literals = _executable_literals(source)
    declared = {
        rule
        for register in REGISTERS.values()
        for rule in (register.dom_selectors + register.payload_pointers
                     + register.narrowings)
    }

    exact = set(literals) & declared
    assert not exact, sorted(exact)

    tokens = {token for selector in declared for token in _selector_tokens(selector)}
    assert {"area-listings__item", "data-gps", "podobne", "zahranicni"} <= tokens, (
        "the token harvester stopped seeing the fragments it exists to catch")
    smuggled = sorted(
        (token, literal)
        for token in tokens for literal in literals if token in literal
    )
    assert not smuggled, smuggled
