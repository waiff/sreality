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
from pathlib import Path
from typing import Any

import pytest

from location_data import contracts
from location_data.html_scope import (
    DOM,
    GUARD_EXCLUDED_ZONE,
    PAYLOAD,
    SCOPER_VERSION,
    TEXT,
    UNSUPPORTED,
    ScopeError,
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


def _scoped(portal: str, fixture: Path | None = None):
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
        ("idnes", "embedded_json", "script[data-maptiler-json]",
         "/geojson/features[isSimilar=true]")] == PAYLOAD
    assert dispositions[("mmreality", "embedded_json", "[\\:property]", None)] == PAYLOAD
    assert dispositions[("mmreality", "embedded_json", "[\\:locations]", None)] == DOM
    assert dispositions[
        ("ceskereality", "jsonld", None, "/offers/offeredby/address")] == PAYLOAD
    assert dispositions[("sreality", "api_json", None, "/premise")] == PAYLOAD
    assert dispositions[("bezrealitky", "description", None, None)] == TEXT


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
    assert not scoped.contains("49°59'01.5")          # the carousel's coordinate
    assert scoped.css("[data-address]") == []
    assert not excluded_zone_admits(scoped, "Oleška, okres Praha-východ")

    header = scoped.css_first("h2.pd-header__address")
    assert header is not None and "Pod Slovany" in header.text()
    subject_pin = scoped.css_first("#printMap[data-gps], #listingMap[data-gps]")
    assert subject_pin is not None
    assert subject_pin.attributes["data-gps"].startswith("50°03'46.7")
    assert excluded_zone_admits(scoped, "ulice Pod Slovany")
    assert scoped.nodes_removed == 5


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
    assert pin.attributes["data-address"].endswith("Plzeň 2-Slovany")
    assert scoped.contains("Slovanská alej")
    assert len(scoped.css("script[type='application/ld+json']")) == 1


def test_a_node_from_a_raw_parse_is_not_owned_by_the_scoped_document() -> None:
    """The guard at node grain — the raw body is not a substrate extraction may use."""
    from selectolax.lexbor import LexborHTMLParser

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
    payload = scope_json({"locality": {"street": "Vinohradská"}},
                         register=REGISTERS["sreality"])

    assert payload.removed_pointers == ()
    assert payload.data == {"locality": {"street": "Vinohradská"}}


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
    register = ScopeRegister.from_zones("bazos", [
        {"locator_kind": "html_selector", "locator": {"css": ".podobne"}},
        {"locator_kind": "html_selector", "locator": {"css": "]not[[a selector"}},
    ])

    scoped = scope_html("<html><body><div class='podobne'>x</div><p>y</p></body></html>",
                        register=register)

    assert scoped.unsupported_selectors == ("]not[[a selector",)
    assert not scoped.is_complete
    assert not scoped.contains("x")
    assert scoped.contains("y")


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
    assert scoped.html[start:end] == "ulice Pod Slovany, Úvaly"
    assert scoped.find_span("Oleška, okres Praha-východ") is None


def test_find_span_tolerates_the_whitespace_a_text_read_collapses() -> None:
    """The real page indents the header across four tabs and a newline."""
    scoped = _scoped("remax", _ARCHIVED / "remax_detail.html")
    quote = "ulice Pod Slovany, Úvaly"

    span = scoped.find_span(quote)

    assert scoped.html.find(quote) == -1
    assert span is not None
    assert " ".join(scoped.html[span[0]:span[1]].split()) == quote


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
    ("idnes", "idnes_detail.html", ".b-similar, .broker, nav",
     ".grid-similar-offers", "Josefův Důl - Dolní Maxov", "Na Balkáně"),
    ("realitymix", "realitymix_detail.html", ".broker-contact, .contact-box",
     ".offer-detail-sidebar__company", "Karla Čapka 1357", "Stráň, Potůčky"),
)


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


def test_no_register_selector_is_hardcoded_in_the_scoper() -> None:
    """The register is contract data. A selector in Python would be a rule with no
    `contract_version`, invisible to the fixture-diff gate and unretractable."""
    tree = ast.parse((_ROOT / "location_data" / "html_scope.py").read_text(encoding="utf-8"))
    literals = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    declared = {
        selector
        for register in REGISTERS.values()
        for selector in register.dom_selectors + register.payload_pointers
    }

    assert not (literals & declared), sorted(literals & declared)
