"""remax@4 — the slim contract, entry by entry, over the bodies this repo commits.

`test_page_reader_canon.py` proves what each READER does. This file proves what remax's own
CONTRACT says, by running the entries `contracts/portals/remax.yaml` actually ships through
the real scoper, the real transforms and the real C6 licence ladder — never hand-built
lookalikes, because a selector typo, a dropped `position_branch` or a re-pointed entry id is
invisible to a test that builds its own locator.

It is also the only file left holding three things the W1-c delete took with it
(`test_page_readers_remax.py`): the PII rail on the captured 437234 body, the subject-vs-
carousel separation that body proves, and the operator ruling that remax's STREET comes from
the subject map and never from the headline (W2-6, 2026-09-05; restated as W1-c R8).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any

import pytest

from location_data import contracts, page_readers
from location_data.claims_intake import ARCHIVED_COORDINATE_RULES, Entry, ListingRow
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from location_data.page_readers import (
    PAGE_READERS,
    ArchivedPayload,
    _licensed_coordinate,
    stamp_page_claim,
)
from scraper.remax_parser import parse_dms_pair
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parent.parent.parent
_W2 = _ROOT / "tests" / "fixtures" / "location_w2"
_REGRESSION = _W2 / "regressions" / "remax" / "437234.json"

# Every remax page body this repo commits. The town entry is asserted on ALL of them: rule
# 25's invariant is "every active Czech listing has a town", so a body where this contract
# cannot state one is a coverage hole, not a fixture quirk.
PINNED_BODY = _W2 / "remax_detail.html"
ARCHIVED_BODY = _ROOT / "tests" / "fixtures" / "portal_html" / "remax_detail.html"
REFETCH = _ROOT / "tests" / "fixtures" / "location_w2a_refetch"
COMMITTED_BODIES: dict[str, Path] = {
    "location_w2/remax_detail.html": PINNED_BODY,
    "portal_html/remax_detail.html": ARCHIVED_BODY,
    "location_w2a_refetch/remax_a1.html": REFETCH / "remax_a1.html",
    "location_w2a_refetch/remax_a2.html": REFETCH / "remax_a2.html",
    "location_w2a_refetch/remax_b1.html": REFETCH / "remax_b1.html",
}
TOWN_PER_BODY = {
    "location_w2/remax_detail.html": "Úvaly",
    "portal_html/remax_detail.html": "Úvaly",
    "location_w2a_refetch/remax_a1.html": "Horní Tošanovice",
    "location_w2a_refetch/remax_a2.html": "Horní Tošanovice",
    "location_w2a_refetch/remax_b1.html": "Soutice",
}

# The neighbour cards on the archived page. The register strips them, and no entry may reach
# one — that is why every subject entry is scoped to an element id or to the subject header.
CAROUSEL_ADDRESSES = (
    "Oleška, okres Praha-východ",
    "Velké Popovice, okres Praha-východ",
    "Havlíčkova, Stará Boleslav, Brandýs nad Labem-Stará Boleslav, okres Praha-východ",
    "Ořechová, Ondřejov, okres Praha-východ",
    "náměstí Smiřických, Kostelec nad Černými lesy, okres Praha-východ",
)

FETCHED_AT = datetime(2026, 8, 13, 4, 30, tzinfo=UTC)
CONTRACT = {c.source: c for c in contracts.load_all()}["remax"]

EXPECTED_ENTRIES = {
    "rx.det.gps": "coordinate",
    "rx.det.header_obec": "obec_name",
    "rx.det.header_cast_obce": "cast_obce_name",
    "rx.det.header_okres": "okres_name",
    "rx.det.crumb_kraj": "kraj_name",
    "rx.det.map_address": "street_name",
}


# ------------------------------------------------------------------ harness

def entry_named(entry_id: str) -> Entry:
    """The SHIPPED entry, projected the way the deploy projects it."""
    for item in fx.entries_for("remax"):
        if item.entry_id == entry_id:
            return item
    raise AssertionError(f"remax@{CONTRACT.version} declares no entry {entry_id!r}")


def listing_row(native: str = "fixture", **overrides: Any) -> ListingRow:
    kwargs: dict[str, Any] = {
        "listing_id": 4242, "source": "remax", "source_id_native": native, "raw_json": {},
        "lat": None, "lon": None, "observed_at": FETCHED_AT, "in_mapy_inventory": False,
    }
    kwargs.update(overrides)
    return ListingRow(**kwargs)


def payload(body: bytes | None = None) -> ArchivedPayload:
    return ArchivedPayload(
        id=9001, source="remax", source_id_native="fixture", page_kind="detail",
        payload_sha256="ab" * 32, first_observed_at=FETCHED_AT, body=body)


def scoped(body: bytes | str) -> ScopedDocument:
    """Through remax's OWN shipped exclusion zones — the decoys a reader must not reach are
    the ones the contract declares, never ones a test invents."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    return scope_html(body, register=ScopeRegister.from_zones(
        "remax", CONTRACT.exclusion_zones))


def run_entry(
    entry: Entry, document: ScopedDocument, *, row: ListingRow | None = None,
) -> list[page_readers.PageRead]:
    return PAGE_READERS[str(entry.reader)](
        entry, row or listing_row(), payload(), document)


def value_of(entry_id: str, document: ScopedDocument) -> str | None:
    reads = run_entry(entry_named(entry_id), document)
    assert len(reads) <= 1, f"{entry_id} returned {len(reads)} reads"
    return reads[0].claim.value_text if reads else None


def only(reads: list[page_readers.PageRead]) -> page_readers.PageRead:
    assert len(reads) == 1, f"expected exactly one read, got {len(reads)}"
    return reads[0]


def span_text(document: ScopedDocument, claim: Any) -> str | None:
    if claim.span_start is None or claim.span_end is None:
        return None
    return document.html[claim.span_start:claim.span_end]


_HEADER = re.compile(r'(<h2 class="pd-header__address">)(.*?)(</h2>)', re.S)
_JUMP = (' <a href="#" data-scroll-to-anchor="#map" class="link link--ar">'
         'mapa <i class="icon-arrow-right"></i></a>')


def body_with_header(header: str) -> ScopedDocument:
    """The REAL archived page with only its `<h2>` swapped.

    Header shapes remax renders that no committed body happens to carry (the dash forms, the
    statutory-city obvody) are asserted this way rather than by planting a whole new fixture:
    everything around the header — the register's zones, the nested `mapa` jump-link, the
    tab runs the portal breaks the line with — stays the real page's."""
    source = ARCHIVED_BODY.read_text(encoding="utf-8")
    swapped = _HEADER.sub(
        lambda m: f"{m.group(1)}\n\t\t\t\t\t\t{header}{_JUMP}\n\t\t\t\t\t{m.group(3)}",
        source, count=1)
    assert swapped != source, "the header swap matched nothing"
    return scoped(swapped)


# --------------------------------------------------------------- the contract's shape

def test_the_contract_pins_version_four():
    """origin/main ships @3; entries are immutable per version, so the slim restatement is
    a bump, never an edit."""
    assert CONTRACT.version == 4
    assert CONTRACT.source == "remax"


def test_the_entry_ids_and_their_claim_types_are_exactly_these_six():
    """The census of the version. An id that leaves is a claim cohort that stops being
    re-mined, and an id that arrives is one that starts — neither may happen unremarked."""
    assert {e.entry_id: e.claim_type for e in CONTRACT.entries} == EXPECTED_ENTRIES


def test_each_claim_type_has_exactly_one_carrier_and_all_are_in_the_vocabulary():
    """W1-c R1: at most one entry per type, and only the eleven types the resolver binds.
    Two entries of one type would make the contract a vote survivorship never asked for."""
    types = [e.claim_type for e in CONTRACT.entries]
    assert len(types) == len(set(types))
    assert set(types) <= contracts.CLAIM_TYPES


def test_every_entry_is_executable_on_the_detail_page_body():
    """Every entry names a page reader and reads the stored detail body. No entry reads a
    `listings` column (the `legacy_column` surface is retired) and none is declared ahead of
    a wave that would run it."""
    for entry in CONTRACT.entries:
        assert entry.reader in PAGE_READERS, entry.entry_id
        assert entry.page_kind == "detail", entry.entry_id
        assert entry.surface in {"html_selector", "archived_html", "map_config"}


def test_the_town_entry_is_the_header_and_names_a_reader():
    """`obec_name` is the one mandatory type (rule 25). For remax it is the subject header —
    the only subject-owned location string the detail page carries — read by `html_regex` and
    folded through `address_part_obec`."""
    entry = entry_named("rx.det.header_obec")
    assert entry.claim_type == contracts.MANDATORY_CLAIM_TYPE
    assert entry.reader == "html_regex"
    assert entry.locator["css"] == "h2.pd-header__address"
    assert entry.transform == ("address_part_obec",)


def test_the_coordinate_entry_keeps_the_id_the_licence_ladder_names():
    """`ARCHIVED_COORDINATE_RULES` keys the licence on the ENTRY ID, so renaming `rx.det.gps`
    would silence the ladder rather than fail — the id is fixed permanently."""
    assert ARCHIVED_COORDINATE_RULES["remax"].entry_id == "rx.det.gps"
    assert entry_named("rx.det.gps").locator["position_branch"] == "portal_pin"


def test_the_pin_declares_no_transform_and_no_guard():
    """Both were inert on `html_point_dms` (it consults neither axis) and both would now be a
    projection-time ContractError. The CZ envelope is intrinsic to `parse_dms_pair`, so the
    removal is of a declaration, never of a check — proven below."""
    entry = next(e for e in CONTRACT.entries if e.entry_id == "rx.det.gps")
    assert entry.transform == [] and entry.guards == []


# ------------------------------------------------- the town, on every committed body

@pytest.mark.parametrize("name", sorted(COMMITTED_BODIES))
def test_the_town_extracts_from_every_committed_body(name: str):
    """The red line. Five bodies, five towns — the hand-built fixture, the real archived
    page, and the three W2a refetch captures."""
    assert value_of("rx.det.header_obec",
                    scoped(COMMITTED_BODIES[name].read_bytes())) == TOWN_PER_BODY[name]


@pytest.mark.parametrize("name, okres, kraj", [
    ("location_w2/remax_detail.html", None, None),
    ("portal_html/remax_detail.html", None, "Středočeský kraj"),
    ("location_w2a_refetch/remax_a1.html", "Frýdek-Místek", "Moravskoslezský kraj"),
    ("location_w2a_refetch/remax_a2.html", "Frýdek-Místek", "Moravskoslezský kraj"),
    ("location_w2a_refetch/remax_b1.html", "Benešov", "Středočeský kraj"),
])
def test_the_admin_entries_read_what_each_body_actually_states(
    name: str, okres: str | None, kraj: str | None,
):
    """Per-entry extraction over the committed bodies, with the ABSTENTIONS pinned too: the
    hand-built fixture carries no breadcrumbs and the `ulice` header form carries no okres,
    and a contract that invented one there would be the defect this gate exists to catch."""
    document = scoped(COMMITTED_BODIES[name].read_bytes())
    assert value_of("rx.det.header_okres", document) == okres
    assert value_of("rx.det.crumb_kraj", document) == kraj
    # No committed body carries a dash form, and none carries a subject-map data-address.
    assert value_of("rx.det.header_cast_obce", document) is None
    assert value_of("rx.det.map_address", document) is None


# ------------------------------------------------------- the header, form by form

@pytest.mark.parametrize("header, obec, cast_obce", [
    # A numbered městský obvod is NEVER the town: RÚIAN has no obec "Praha 3", so a claim
    # carrying it resolves to nothing and reads as a portal that publishes no town.
    ("ulice Roháčova, Praha 3 – Žižkov", "Praha", "Žižkov"),
    ("Praha 5 - Stodůlky", "Praha", "Stodůlky"),
    ("Praha 10 - Uhříněves", "Praha", "Uhříněves"),
    ("Plzeň 3 – Skvrňany", "Plzeň", "Skvrňany"),
    ("Pardubice II", "Pardubice", None),
    ("Brno-Židenice", "Brno", None),
    # …and a hyphen is not enough on its own: these two are towns, not obvody.
    ("Frýdek-Místek", "Frýdek-Místek", None),
    ("Kostelec nad Černými Lesy", "Kostelec nad Černými Lesy", None),
    # The dash form the portal renders on 7 of 12 mined pages.
    ("Bílovec – část obce Ohrada", "Bílovec", "Ohrada"),
    ("Opava – městská část Vávrovice", "Opava", "Vávrovice"),
    ("Merklín – část obce Pstruží u Merklína", "Merklín", "Pstruží u Merklína"),
    ("Ostrava - Poruba", "Ostrava", "Poruba"),
    ("Liberec - Vratislavice nad Nisou", "Liberec", "Vratislavice nad Nisou"),
])
def test_the_header_splits_the_town_from_the_obvod_it_is_written_with(
    header: str, obec: str, cast_obce: str | None,
):
    """R4, through the shipped entries. The town entry publishes the CITY and the část-obce
    entry keeps the obvod, so nothing is lost and no claim carries a wrong admin level."""
    document = body_with_header(header)
    assert value_of("rx.det.header_obec", document) == obec
    assert value_of("rx.det.header_cast_obce", document) == cast_obce


@pytest.mark.parametrize("header, obec, okres", [
    ("Bobrůvka, okres Žďár nad Sázavou", "Bobrůvka", "Žďár nad Sázavou"),
    ("Velké Losiny, okr. Šumperk", "Velké Losiny", "Šumperk"),
    ("ulice Pod Slovany, Úvaly", "Úvaly", None),
])
def test_the_okres_is_keyed_on_its_qualifier_and_never_on_a_position(
    header: str, obec: str, okres: str | None,
):
    """`okres X` / `okr. X`, both spellings, and the town entry steps back one segment when
    the qualifier is there. A positional read would type `Úvaly` as an okres."""
    document = body_with_header(header)
    assert value_of("rx.det.header_obec", document) == obec
    assert value_of("rx.det.header_okres", document) == okres


def test_the_header_read_drops_the_nested_jump_link():
    """The defect the `html_own_text` read exists to avoid: the h2 nests `<a>mapa</a>` on
    12/12 mined pages, so a deep read states the address as "… Úvaly mapa"."""
    document = scoped(ARCHIVED_BODY.read_bytes())
    node = document.css_first("h2.pd-header__address")
    assert node is not None and "mapa" in (node.text() or "")
    assert value_of("rx.det.header_obec", document) == "Úvaly"
    assert value_of("rx.det.header_okres", document) is None


def test_the_towns_evidence_resolves_to_the_uncollapsed_source():
    """The quote is what the page said, the span indexes the real bytes — so a value that
    stopped coming out of the element it claims to would be visible, not plausible."""
    document = scoped(ARCHIVED_BODY.read_bytes())
    claim = only(run_entry(entry_named("rx.det.header_obec"), document)).claim
    assert claim.value_text == "Úvaly"
    assert claim.span_start is not None
    assert span_text(document, claim) == claim.evidence_quote
    assert "Úvaly" in claim.evidence_quote


# ------------------------------------------------------------------ the kraj crumb

def test_the_kraj_comes_from_the_crumb_whose_own_href_carries_the_slug():
    """Keyed on the LINK, never on a crumb position: remax's chain is
    Reality REMAX / <category> / Prodej / <kraj> / <okres>, and the category half is one or
    two levels deep depending on the property type, so `positions: [4]` is wrong half the
    time. Both the kraj and its okres child carry the kraj slug in their href; the kraj crumb
    is the parent path and therefore always the first match."""
    document = scoped(ARCHIVED_BODY.read_bytes())
    anchors = document.css('.breadcrumbs a[href*="kraj"] span[itemprop="name"]')
    assert [(node.text(deep=False) or "").strip() for node in anchors] == [
        "Středočeský kraj", "Praha-východ"]
    assert value_of("rx.det.crumb_kraj", document) == "Středočeský kraj"


def test_the_kraj_entry_abstains_where_the_page_names_no_kraj():
    """"kraj only if the page states it" — the hand-built fixture carries no breadcrumbs at
    all, and Prague's chain names `Hlavní město Praha`, whose slug has no `kraj` in it."""
    assert value_of("rx.det.crumb_kraj", scoped(PINNED_BODY.read_bytes())) is None
    body = ('<html><body><div class="breadcrumbs"><ul>'
            '<li><a href="/reality/byty/prodej/praha/"><span itemprop="name">'
            'Hlavní město Praha</span></a></li></ul></div></body></html>')
    assert value_of("rx.det.crumb_kraj", scoped(body)) is None


# --------------------------------------------------------------------- the pin

def test_the_pin_is_licensed_portal_from_its_own_declared_branch():
    """The ladder, applied exactly as the lane applies it: the entry declares the branch, the
    ladder stamps the class, and the reader's own `licence_class` is discarded on the way."""
    document = scoped(ARCHIVED_BODY.read_bytes())
    entry = entry_named("rx.det.gps")
    read = only(run_entry(entry, document))
    assert read.position_branch == "portal_pin"
    stamped = stamp_page_claim(read.claim, payload(),
                               scope_version=document.scope_version)
    licensed, reason = _licensed_coordinate(stamped, listing_row(), entry,
                                            read.position_branch)
    assert licensed is not None and licensed.licence_class == "portal"
    assert reason == "archived_rx.det.gps"


def test_the_pins_geometry_round_trips_the_portals_own_dms_string():
    """The WKT is not a second parse of the page: it is `parse_dms_pair` on the same raw
    attribute the claim quotes, so a drift in the arithmetic shows up here rather than as a
    plausible pin a few hundred metres away."""
    document = scoped(ARCHIVED_BODY.read_bytes())
    read = only(run_entry(entry_named("rx.det.gps"), document))
    lat, lon = parse_dms_pair(read.claim.value_text)
    assert lat is not None and lon is not None
    assert read.claim.value_geom_wkt == page_readers.point_wkt(lat, lon)
    # The quote is the DECODED attribute and the span indexes the serialised source, where
    # the seconds mark is `&quot;`.
    assert unescape(span_text(document, read.claim)) == read.claim.evidence_quote


def test_the_pin_is_scoped_by_element_id_so_a_neighbour_card_can_never_win_it():
    """First-`[data-gps]`-in-the-document is what put a neighbour's position on the subject
    [live-B §3.5.1]. Both ids carry the identical subject pin on the committed bodies."""
    document = scoped(ARCHIVED_BODY.read_bytes())
    # The register already removed the carousel's `[data-gps]` cards; what survives is the
    # subject's own pair, and both carry the identical pin.
    assert {node.attributes.get("id") for node in document.css("[data-gps]")} == {
        "printMap", "listingMap"}
    subject = only(run_entry(entry_named("rx.det.gps"), document)).claim.value_text
    for element_id in ("#printMap", "#listingMap"):
        node = document.css_first(f"{element_id}[data-gps]")
        assert node is not None and unescape(node.attributes["data-gps"]) == subject


def test_the_mapy_inventory_veto_outranks_the_pin():
    """§6.4's gate joins on `listing_id`, not on `surface` — re-reading the same position out
    of an archived page is the same position."""
    document = scoped(ARCHIVED_BODY.read_bytes())
    entry = entry_named("rx.det.gps")
    read = only(run_entry(entry, document))
    stamped = stamp_page_claim(read.claim, payload(),
                               scope_version=document.scope_version)
    licensed, reason = _licensed_coordinate(
        stamped, listing_row(in_mapy_inventory=True), entry, read.position_branch)
    assert licensed is None
    assert reason == "listing_in_mapy_affected_inventory"


def test_a_pin_outside_the_cz_envelope_is_dropped_by_the_reader_itself():
    """The envelope is INTRINSIC to `parse_dms_pair`, which returns (None, None) outside it.
    This is the test that the removed `guards:` declaration removed nothing."""
    body = ('<html><body><div id="printMap" data-gps='
            '"48°51\'29.6&quot;N,2°17\'40.2&quot;E"></div></body></html>')
    assert run_entry(entry_named("rx.det.gps"), scoped(body)) == []


# ------------------------------------------------- the ruled street source (R8)

def test_the_street_is_claimed_from_the_subject_map_and_from_nowhere_else():
    """THE OPERATOR RULING (W2-6, 2026-09-05; W1-c R8): remax's street comes from the map
    code, not from the headline, "because we do not know how the headline is built and it
    could include misleading data, such as hotel name". One `street_name` entry, and its
    selector is the two subject-map ids — never `h2.pd-header__address`."""
    street_entries = [e for e in CONTRACT.entries if e.claim_type == "street_name"]
    assert [e.entry_id for e in street_entries] == ["rx.det.map_address"]
    entry = street_entries[0]
    assert entry.locator["css"] == "#printMap[data-address], #listingMap[data-address]"
    assert entry.locator["attr"] == "data-address"
    assert entry.subject_scope == {"subject_scoped": True, "kind": "subject_map"}
    assert "pd-header" not in entry.locator["css"]


def test_the_headline_would_have_typed_a_hotel_name_as_a_street():
    """Why the ruling is a ruling and not a preference. The headline's leading segment reads
    `ulice <X>` whatever X is; on this shape the don't-fabricate trio saves it, but the
    contract does not rely on that — it simply never claims a street from the header."""
    document = body_with_header("ulice Hotel Slunce, Úvaly")
    assert value_of("rx.det.header_obec", document) == "Úvaly"
    assert value_of("rx.det.map_address", document) is None


def test_the_subject_map_entry_reads_nothing_on_any_body_in_this_repo():
    """MEASURED ABSENCE, and the expected steady state: `#printMap`/`#listingMap` carry
    `data-gps` and no `data-address` on any committed body or either live recon sample
    (recon §5.7). A zero-yield readout here is not an alarm; a value matching a carousel
    card's would be."""
    for name, path in sorted(COMMITTED_BODIES.items()):
        assert value_of("rx.det.map_address", scoped(path.read_bytes())) is None, name


def test_the_subject_map_entry_reads_the_subject_the_day_the_portal_renders_it():
    """The ruled shape, proven without fabricating a claim in the golden: the subject map
    carries the attribute, a carousel card carries a different one, and the read is the
    subject's with a span that resolves."""
    body = (
        '<html><body>'
        '<div id="listingMap" class="smap-defaults" '
        'data-gps="50°04\'54.0&quot;N,14°27\'01.0&quot;E" '
        'data-address="Roháčova, Praha 3 - Žižkov, Praha,"></div>'
        '<div class="area-listings">'
        '<div class="area-listings__item" data-address="V Horní Stromce, Praha 3, '
        'Vinohrady, okres Hlavní město Praha"></div>'
        '</div></body></html>')
    document = scoped(body)
    claim = only(run_entry(entry_named("rx.det.map_address"), document)).claim
    assert claim.value_text == "Roháčova"
    assert claim.evidence_quote == "Roháčova, Praha 3 - Žižkov, Praha,"
    assert claim.subject_scoped is True
    assert span_text(document, claim) == claim.evidence_quote
    assert document.contains(
        "V Horní Stromce, Praha 3, Vinohrady, okres Hlavní město Praha") is False


@pytest.mark.parametrize("address, street", [
    # The leading segment is a street only when it survives `clean_street` /
    # `reject_as_town` / `looks_like_czech_street`; otherwise the entry claims nothing.
    ("Křimická 655/31, Plzeň 3, Plzeň", "Křimická"),
    ("Stráň, Merklín, okres Plzeň-jih", None),
    ("Úvaly", None),
])
def test_the_street_transform_refuses_to_fabricate(address: str, street: str | None):
    body = f'<html><body><div id="printMap" data-address="{address}"></div></body></html>'
    assert value_of("rx.det.map_address", scoped(body)) == street


def test_no_entry_can_reach_a_neighbour_card():
    """The contamination class the whole contract is shaped around, asserted against the
    register the contract itself declares. The decoys are unreachable twice over: the zones
    strip every `.area-listings__item[data-address]` before a reader sees the tree, and every
    subject entry names an element id or the subject header anyway."""
    document = scoped(ARCHIVED_BODY.read_bytes())
    for value in CAROUSEL_ADDRESSES:
        assert document.contains(value) is False, value
    assert document.css("[data-address]") == []
    for entry_id in EXPECTED_ENTRIES:
        read = value_of(entry_id, document)
        assert read not in CAROUSEL_ADDRESSES


def test_the_subject_map_entry_ignores_the_neighbourhood_map():
    """`#areaMap` is the neighbourhood map, not the subject's. It is excluded by id rather
    than by a zone, which is why an id-scoped selector is the narrowest expressible form of
    02 §2.2.6's permission to read `data-address` outside `.area-listings__item`."""
    body = ('<html><body><div id="areaMap" '
            'data-address="Úvaly, okres Praha-východ"></div></body></html>')
    assert run_entry(entry_named("rx.det.map_address"), scoped(body)) == []


# ------------------------------- the PII rail on the captured control body (R14)

def test_the_captured_control_listing_is_a_real_body_and_carries_no_pii():
    """437234 is the operator's control (the pre-W2 resolver said 'Bukovická 297' and a human
    confirmed it). Its body under `regressions/remax/` was CAPTURED from the live page through
    `scraper.remax_parser.parse_detail`, not written by hand — and the two keys that parse
    produces which have no business in a committed fixture (`broker`, an agent's name and
    e-mail; `image_urls`) are removed rather than blanked.

    Carried forward from the deleted `test_page_readers_remax.py`: after that delete this is
    the ONLY test in the repo that reads this fixture, and the golden gate still scores it."""
    doc = json.loads(_REGRESSION.read_text(encoding="utf-8"))
    raw = doc["raw_json"]
    assert raw["id"] == "437234"
    assert raw["display_address"] == "ulice Bukovická, Velké Losiny"
    assert "broker" not in raw and "image_urls" not in raw
    serialised = json.dumps(doc, ensure_ascii=False)
    assert "@" not in serialised and "re-max.cz" not in serialised


def test_the_captured_body_states_the_subject_and_the_carousel_separately():
    """W0 item 0d's rename, on a page captured three weeks later: the subject's header is
    `display_address` and the neighbour card's line is `carousel_address` — two towns 20 km
    apart, in one body, which is the contamination the whole contract is shaped around."""
    raw = json.loads(_REGRESSION.read_text(encoding="utf-8"))["raw_json"]
    assert raw["carousel_address"] == "Petrov nad Desnou, okres Šumperk"
    assert raw["carousel_address"] != raw["display_address"]
    # And the banned key is simply GONE from a post-W0-0d payload.
    assert "address" not in raw


# ------------------------- the omission that has to stay measured, not asserted

# Every hedge word a Czech portal uses when it blurs a position. remax uses none of them
# ABOUT A POSITION — which is why remax@4 declares no `precision_declaration` entry.
_HEDGES = re.compile(r"(?i)p[řr]ibli[žz]n|orienta[čc]n|nep[řr]esn|zhruba\s+v\s+lokalit")

# The one hit in this repo, and what it is about. It qualifies a LAND AREA in free prose, so
# an entry over these words would stamp a square-metre hedge as a location precision label.
_KNOWN_AREA_HEDGE = "Celková výměra pozemků činí přibližně 9 047 m²"


def test_no_committed_body_states_a_precision_about_the_location():
    """The reason `precision_declaration` is omitted, kept as a measurement.

    Twice now an omission in this contract's report rested on a negative that did not hold on
    the real bytes. This pins the negative instead: if remax ever ships a blur/approximate
    flag about the POSITION, a hedge word appears somewhere that is not the known area
    sentence and this test fails, which is the prompt to add the entry."""
    assert "precision_declaration" not in {e.claim_type for e in CONTRACT.entries}
    for name, path in COMMITTED_BODIES.items():
        text = path.read_text(encoding="utf-8")
        for match in _HEDGES.finditer(text):
            start = text.rfind(">", 0, match.start()) + 1
            sentence = text[start:text.find("<", match.end())]
            assert _KNOWN_AREA_HEDGE in sentence, (
                f"{name} hedges about something new at offset {match.start()}: "
                f"{sentence.strip()[:160]!r}. If it is about the POSITION, remax needs a "
                f"precision_declaration entry (W1-c R5 makes the label stick to the value).")


def test_the_known_hedge_is_an_area_and_lives_in_the_description():
    """The counter-example the test above is calibrated against, stated once so nobody has to
    re-derive it: one hit, on one body, in the seller's prose, about square metres."""
    body = COMMITTED_BODIES["location_w2a_refetch/remax_b1.html"].read_text(encoding="utf-8")
    assert body.count("přibližně") == 1
    assert _KNOWN_AREA_HEDGE in body
    assert sum(1 for p in COMMITTED_BODIES.values()
               if _HEDGES.search(p.read_text(encoding="utf-8"))) == 1
