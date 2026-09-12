"""realitymix@5 — the slim location contract (rule 25 / W1-c), scored against real bodies.

Every test runs the SHIPPED entries (no hand-written locators) through the real readers, the
real scoper and the real licence ladder. Five committed bodies:
  * `tests/fixtures/location_w2/realitymix_detail.html` — the MODELLED page, the one the
    fixture-diff golden scores. A selector that works on a page written from the contract
    proves the shape, not the population, so it is never the only evidence for a carrier.
  * `tests/fixtures/portal_html/realitymix_detail.html` — the real capture of 8662169
    (Potůčky), which the contract pins. PII-scrubbed by
    `scripts/fetch_and_anonymize_fixtures.py --scrub-contacts`, so the agent name in it is
    the placeholder and not a real broker.
  * `tests/fixtures/location_w2a_refetch/realitymix_{a1,a2,b1}.html` — real refetches. a1/a2
    are one Praha-Smíchov flat and carry the ONE-SEGMENT `data-address="Plzeňská"` that
    decided v5's town carrier; b1 (Děčín) is the only committed body with a house number.

Supersedes tests/location_data/test_contract_realitymix_w2.py; its realitymix-specific
coverage is carried forward here in full (the PII/zone rail, the entry-id rung of the licence
ladder, the Mapy veto, the blank-attribute under-claim, the fail-closed breadcrumb) minus the
entries v5 deletes.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from location_data import contracts
from location_data.claims_intake import Entry, ListingRow, extract_listing
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from location_data.page_readers import (
    ARCHIVED_COORDINATE_RULES,
    PAGE_READERS,
    ArchivedPayload,
    _licensed_coordinate,
    extract_page,
    stamp_page_claim,
)
from location_data.resolver.normalize import normalize_match_key
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
MODELLED = _ROOT / "tests" / "fixtures" / "location_w2" / "realitymix_detail.html"
CAPTURED = _ROOT / "tests" / "fixtures" / "portal_html" / "realitymix_detail.html"
_REFETCH = _ROOT / "tests" / "fixtures" / "location_w2a_refetch"
A1, A2, B1 = (_REFETCH / f"realitymix_{name}.html" for name in ("a1", "a2", "b1"))
EVERY_BODY = (MODELLED, CAPTURED, A1, A2, B1)

NATIVE = "8662169"
FETCHED_AT = datetime(2026, 8, 13, 4, 30, tzinfo=UTC)

CONTRACT = {c.source: c for c in contracts.load_all()}["realitymix"]
ENTRIES = {e.entry_id: e for e in fx.entries_for("realitymix")}
REGISTER = ScopeRegister.from_zones("realitymix", CONTRACT.exclusion_zones)

TOWN_ENTRY = "rm.det.slug"
TOWN_READER = "html_attr_regex"

# The entry's own carrier, read back out of a whole page so the corpus measurement below
# scans exactly what the reader would see. `_OBVOD_SHAPED` is the ASCII form R4's transform
# cannot see: a trailing ordinal ("praha-5") or roman numeral ("pardubice-ii"). The
# HYPHENATED obvod form ("brno-zidenice") is deliberately NOT matched — as a slug it is
# indistinguishable from the real obec slugs "karlovy-vary" / "benesov-nad-ploucnici".
_DETAIL_SLUG = re.compile(r"/detail/([^/\"'\s]+)/")
_OBVOD_SHAPED = re.compile(r"^.+-(?:\d+|[ivx]{1,4})$", re.IGNORECASE)

# The whole contract, one line per entry. Rule 25 caps this at one entry per claim type and
# makes the obec entry mandatory, so the map IS the contract.
ENTRY_TYPES: dict[str, str] = {
    "rm.det.slug": "obec_name",
    "rm.det.gps": "coordinate",
    "rm.det.agency_est_flag": "precision_declaration",
    "rm.det.breadcrumb_kraj": "kraj_name",
    "rm.det.map_okres": "okres_name",
    "rm.det.breadcrumb_quarter": "cast_obce_name",
    "rm.det.map_street": "street_name",
    "rm.det.og_house_number_cp": "house_number_cp",
    "rm.det.og_house_number_co": "house_number_co",
    "rm.det.og_psc": "psc",
}


# ------------------------------------------------------------------------ helpers

def document(body: bytes | str | Path | None = None) -> ScopedDocument:
    body = CAPTURED if body is None else body
    if isinstance(body, Path):
        body = body.read_bytes()
    if isinstance(body, str):
        body = body.encode("utf-8")
    return scope_html(body, register=REGISTER)


def row() -> ListingRow:
    return fx.listing("realitymix", {}, native=NATIVE)


def payload(body: Path | bytes | None = None) -> ArchivedPayload:
    body = CAPTURED if body is None else body
    if isinstance(body, Path):
        body = body.read_bytes()
    return ArchivedPayload(
        id=9001, source="realitymix", source_id_native=NATIVE, page_kind="detail",
        payload_sha256="ab" * 32, first_observed_at=FETCHED_AT, body=body)


def read(entry_id: str, doc: ScopedDocument | None = None, *,
         listing: ListingRow | None = None, entry: Entry | None = None):
    entry = entry or ENTRIES[entry_id]
    doc = document() if doc is None else doc
    return PAGE_READERS[str(entry.reader)](entry, listing or row(), payload(), doc)


def value(entry_id: str, doc: ScopedDocument | None = None) -> str | None:
    reads = read(entry_id, doc)
    assert len(reads) <= 1, f"{entry_id} produced {len(reads)} reads; every v5 entry is one"
    return reads[0].claim.value_text if reads else None


def og_title(title: str) -> ScopedDocument:
    return document(f'<html><head><meta property="og:title" content="{title}">'
                    f'</head><body></body></html>')


# ------------------------------------------------------------------ the contract shape

def test_the_version_is_five():
    assert CONTRACT.version == 5


def test_the_entry_id_set_is_exactly_these_ten():
    """Rule 25's shrink is the deliverable: v4 shipped 20 entries, five of them reading
    `listings` columns that are being deleted and four of them claiming types the eleven-type
    vocabulary does not carry."""
    assert {e.entry_id for e in CONTRACT.entries} == set(ENTRY_TYPES)
    assert {e.entry_id: e.claim_type for e in CONTRACT.entries} == ENTRY_TYPES


def test_there_is_at_most_one_entry_per_claim_type():
    types = [e.claim_type for e in CONTRACT.entries]
    assert len(types) == len(set(types)), sorted(types)


def test_every_claim_type_is_a_member_of_the_eleven_type_vocabulary():
    assert set(ENTRY_TYPES.values()) <= contracts.CLAIM_TYPES


def test_every_entry_names_a_reader_the_one_lane_executes():
    """No readerless entries (rule 25), every reader is a PAGE reader (this portal's whole
    contract is on the stored body), and no entry declares a transform or guard the runtime
    does not implement."""
    for entry in CONTRACT.entries:
        assert entry.reader, entry.entry_id
        assert entry.reader in PAGE_READERS, entry.entry_id
        assert entry.page_kind == "detail", entry.entry_id
        assert entry.surface != "legacy_column", entry.entry_id
        for spec in entry.transform:
            assert spec.partition(":")[0] in contracts.IMPLEMENTED_TRANSFORMS, entry.entry_id
        for guard in entry.guards:
            assert guard in contracts.IMPLEMENTED_GUARDS, entry.entry_id


def test_the_town_entry_is_mandatory_named_and_reads_the_canonical_slug():
    """Rule 25: the obec entry is the one entry a contract may not omit."""
    town = next(e for e in CONTRACT.entries if e.claim_type == "obec_name")
    assert town.entry_id == TOWN_ENTRY
    assert town.reader == TOWN_READER
    assert town.extraction_method == "url_slug_parse"
    assert town.locator["css"] == "link[rel='canonical']"
    # R4: a numbered/hyphenated městský obvod is never the town, even off a slug.
    assert list(town.transform) == ["statutory_city_obec"]


def test_the_coordinate_entry_declares_its_cap_and_its_ladder_rung():
    assert ARCHIVED_COORDINATE_RULES["realitymix"].entry_id == "rm.det.gps"
    assert ENTRIES["rm.det.gps"].precision_map["precision_cap"] == {
        "granularity_max": "address_point", "position_source_max": "portal_pin"}
    assert ENTRIES["rm.det.agency_est_flag"].precision_map["blurred_labels"] == ["estimated"]


def test_the_payload_lane_produces_nothing_for_this_portal():
    """The five v4 legacy-column entries went with the columns they read and `locality_text`
    typed `address_line_verbatim`, which the eleven-type vocabulary does not carry. So the
    raw_json half of the lane is silent for realitymix by construction — the intended state,
    stated here so it is never read as a regression, and the reason the town is a PAGE claim.
    """
    for blob, native in ((fx.REALITYMIX_GEOCODE, "8375963"),
                         (fx.REALITYMIX_PAGE, "8460367"),
                         (fx.REALITYMIX_NULL_LOCALITY, "8590773")):
        result = extract_listing(fx.listing("realitymix", blob, native=native),
                                 list(ENTRIES.values()))
        assert result.claims == [] and not result.refusals, native


# ------------------------------------------------------- the town, on every real body

@pytest.mark.parametrize("body,expected", [
    (MODELLED, "plzen"), (CAPTURED, "potucky"),
    (A1, "praha"), (A2, "praha"), (B1, "decin")])
def test_the_town_is_claimed_from_the_canonical_slug_on_every_committed_body(body, expected):
    """100% of the committed corpus, which is the property the carrier was chosen for:
    `_town_from_url` (scraper/realitymix_parser.py) calls the `/detail/{obec}/` segment
    "present on EVERY realitymix detail URL", against the ~28% of pages that carry no map."""
    assert value(TOWN_ENTRY, document(body)) == expected


@pytest.mark.parametrize("slug,accented", [
    ("plzen", "Plzeň"), ("praha", "Praha"), ("decin", "Děčín"), ("potucky", "Potůčky"),
    ("frenstat-pod-radhostem", "Frenštát pod Radhoštěm")])
def test_the_ascii_slug_folds_to_the_same_gazetteer_key_as_the_accented_name(slug, accented):
    """The slug is ASCII-folded and hyphenated; `normalize_match_key` (the join key against
    `ruian_name_index.name_norm`) folds diacritics and punctuation, so it binds exactly like
    an accented claim. This is why the carrier costs no accuracy."""
    assert normalize_match_key(slug) == normalize_match_key(accented)


def canonical(slug: str) -> ScopedDocument:
    return document(f'<html><head><link rel="canonical" '
                    f'href="https://realitymix.cz/detail/{slug}/x.html">'
                    f'</head><body></body></html>')


def test_a_statutory_city_obvod_slug_is_folded_to_its_city():
    """R4's chain: a numbered or hyphenated městský obvod is never the town. The transform
    folds the DISPLAY spelling, which is the form `address_part_obec` feeds it elsewhere."""
    for slug, city in (("Plzeň 3", "Plzeň"), ("Praha 8", "Praha"),
                       ("Brno-Židenice", "Brno"), ("Pardubice II", "Pardubice")):
        assert value(TOWN_ENTRY, canonical(slug)) == city, slug


def test_an_ascii_obvod_slug_would_NOT_be_folded_and_this_is_the_entry_s_one_hole():
    """The negative half, pinned rather than narrated — the ONE way this mandatory entry can
    go wrong with no other test noticing. `_STATUTORY_CITY_ORDINAL_RE` /
    `_STATUTORY_CITY_HYPHEN_RE` (claims_common.py) are accent- AND case-sensitive, so an
    ASCII URL slug is invisible to them: if realitymix ever serves `/detail/praha-5/`, the
    R4 rail on the town does nothing and the obvod is published as the obec. Asserted as the
    CURRENT behaviour, so the day the transform is taught the slug form this test fails and
    is updated deliberately instead of the fold silently starting to matter.

    The same insensitivity is why the fold is safe here: `karlovy-vary` and
    `benesov-nad-ploucnici` are real committed obec slugs with a hyphen, and a slug-aware
    transform that folded on the hyphen alone would truncate them to "karlovy" / "benesov".
    """
    for slug in ("praha-5", "plzen-3", "brno-zidenice"):
        assert value(TOWN_ENTRY, canonical(slug)) == slug, slug
    for slug in ("karlovy-vary", "benesov-nad-ploucnici", "frenstat-pod-radhostem"):
        assert value(TOWN_ENTRY, canonical(slug)) == slug, slug


def test_no_committed_slug_is_an_obvod_so_the_hole_above_is_unreachable_today():
    """The measurement that makes the hole acceptable, run in CI instead of quoted. Across
    the four REAL captures every `/detail/{slug}/` href — 83 of them, 44 praha, 14 decin,
    8 touzim, 5 potucky, 4 each rumburk / karlovy-vary / benesov-nad-ploucnici — is a plain
    obec slug, Prague included: realitymix serves `/detail/praha/`, never `/detail/praha-5/`.
    The modelled page is excluded because its own header comment writes `/detail/{obec}/`."""
    seen: set[str] = set()
    for body in (CAPTURED, A1, A2, B1):
        slugs = _DETAIL_SLUG.findall(body.read_text(encoding="utf-8", errors="replace"))
        assert slugs, body.name
        seen.update(slugs)
    assert seen == {"potucky", "touzim", "karlovy-vary", "praha", "decin", "rumburk",
                    "benesov-nad-ploucnici"}
    for slug in seen:
        assert not _OBVOD_SHAPED.match(slug), slug
        assert value(TOWN_ENTRY, canonical(slug)) == slug, slug


def test_the_one_segment_address_line_is_a_street_and_is_never_claimed_as_the_town():
    """THE defect v5 exists to fix. Both Praha-Smíchov refetches serve
    `data-address="Plzeňská"` — a STREET — while the same page states the town three other
    ways. v4's `rm.det.map_obec` ran `address_part_obec`, which has no arity condition, and
    published "Plzeňská" as the obec: the 8595551 class, where a street name that IS an obec
    name elsewhere binds the wrong obec and the pin-in-town check then yields `disputed`.
    No v5 entry reads that segment as an obec."""
    for body in (A1, A2):
        doc = document(body)
        pin = doc.css_first("div#print-map, div#map")
        assert pin is not None and pin.attributes["data-address"] == "Plzeňská"
        assert value(TOWN_ENTRY, doc) == "praha"
        assert "Plzeňská" not in {value(entry_id, doc) for entry_id in ENTRY_TYPES}


def test_the_breadcrumb_cannot_carry_the_town_on_a_prague_page():
    """The other carrier considered and rejected: on a1 the ld+json chain anchors on the
    `praha` kraj slug and ends at "Praha 5", so a breadcrumb obec (two levels down) reads
    past the end of the chain and claims nothing at all — and a breadcrumb OKRES would type
    the městský obvod "Praha 5" as an okres."""
    doc = document(A1)
    assert value("rm.det.breadcrumb_kraj", doc) == "Praha"
    assert read("rm.det.map_okres", doc) == []


# --------------------------------------------------- one extraction per shipped entry

@pytest.mark.parametrize("entry_id,body,expected", [
    # the pin: both map divs carry identical attributes, and b1 has ONLY `div#map`
    ("rm.det.gps", MODELLED, "49.73561,13.39051"),
    ("rm.det.gps", CAPTURED, "50.427238,12.742544"),
    ("rm.det.gps", B1, "50.740624066389,14.194566771667"),
    # the kraj, from the breadcrumb chain anchored on its kraj slug
    ("rm.det.breadcrumb_kraj", CAPTURED, "Karlovarský kraj"),
    ("rm.det.breadcrumb_kraj", B1, "Ústecký kraj"),
    # the okres, keyed on the `okres X` qualifier
    ("rm.det.map_okres", MODELLED, "Plzeň-město"),
    ("rm.det.map_okres", CAPTURED, "Karlovy Vary"),
    # the quarter, three levels under the kraj anchor
    ("rm.det.breadcrumb_quarter", CAPTURED, "Stráň"),
    ("rm.det.breadcrumb_quarter", B1, "Děčín XXXII-Boletice nad Labem"),
    # the street, only through the don't-fabricate gates
    ("rm.det.map_street", MODELLED, "Slovanská alej"),
    # the house number and the PSČ, both out of og:title
    ("rm.det.og_house_number_cp", B1, "378"),
    ("rm.det.og_psc", B1, "40711"),
    ("rm.det.og_psc", MODELLED, "32600"),
])
def test_each_entry_extracts_its_value_from_a_committed_body(entry_id, body, expected):
    assert value(entry_id, document(body)) == expected


def test_the_two_entries_no_committed_body_fires_are_exercised_on_their_own_shape():
    """`rm.det.agency_est_flag` and `rm.det.og_house_number_co` claim nothing on all five
    committed bodies — every one of them carries a `data-gps-*` pair, and not one publishes a
    čp/čo PAIR in og:title. Both are silent no-ops on today's corpus and correct the instant
    the shape appears, so the shape is what is asserted; no fixture was edited to manufacture
    one. The markup below is the map JS's own predicate (`if (gpsLat && gpsLon) …`) and the
    production og:title shape `scraper/realitymix_parser.py::_HOUSE_NO_RE` documents."""
    blurred = read("rm.det.agency_est_flag",
                   document('<html><body><div id="map" data-address="Zlín"></div>'
                            '</body></html>'))
    assert len(blurred) == 1
    assert blurred[0].claim.value_text == "estimated"
    assert blurred[0].claim.blur_evidence == "declared"

    doc = og_title("Prodej bytu, Luční 1793/3, 301 00 Plzeň")
    assert value("rm.det.og_house_number_cp", doc) == "1793"
    assert value("rm.det.og_house_number_co", doc) == "3"
    assert value("rm.det.og_psc", doc) == "30100"


@pytest.mark.parametrize("title,cp,psc", [
    # the b1 shape: a house number, a 5-digit PSČ, and a second 5-digit run in "[ID …]"
    ("Prodej, byty/3+1, 77 m2, Čsl. partyzánů 378, 40711 Děčín, Děčín [ID 84581]",
     "378", "40711"),
    # a title that ENDS at the PSČ. The cp pattern used to demand a literal trailing space,
    # so this shape yielded a PSČ and silently lost its house number with no absence.
    ("Prodej bytu, Slovanská alej 12, 32600", "12", "32600"),
    ("Prodej bytu 3+1 82 m2, Slovanská alej, 326 00 Plzeň 2-Slovany", None, "32600"),
    # no address at all: a1/a2's real og:title
    ("Prodej SPV byt 2+1 53m² s výtahem v atraktivní lokalitě Praha - Smíchov", None, None),
])
def test_the_og_title_pair_agrees_about_where_the_string_ends(title, cp, psc):
    doc = og_title(title)
    assert value("rm.det.og_house_number_cp", doc) == cp
    assert value("rm.det.og_psc", doc) == psc


def test_the_psc_regex_cannot_fire_on_the_pages_other_digit_runs():
    """b1's og:title carries "77 m2", the house number "378", the PSČ "40711" and a trailing
    "[ID 84581]" — a second 5-digit run. The comma anchor is what picks the PSČ; every other
    5-digit hit in this corpus is a broker office, an IČO fragment or a Kč/m² value."""
    assert value("rm.det.og_psc", document(B1)) == "40711"
    assert "84581" in document(B1).html


# ------------------------------------------------------------------ the quarter carrier

def test_the_quarter_is_never_the_town_restated():
    """The correctness rail on `cast_obce_name`. `data-form-address` — v4's carrier and the
    one this draft rejected — stamps "Praha" on both Smíchov refetches, so it would publish
    the OBEC as the quarter on the biggest city in the corpus while the page's real quarter
    is Smíchov / Praha 5. The breadcrumb tail fails closed there instead: a 6-crumb Prague
    chain has no level three, so nothing is claimed."""
    for body in (A1, A2):
        doc = document(body)
        form = doc.css_first("[data-advert-detail-contact-form][data-form-address]")
        assert form is not None and form.attributes["data-form-address"] == "Praha"
        assert read("rm.det.breadcrumb_quarter", doc) == []
        town = value(TOWN_ENTRY, doc)
        quarters = [value("rm.det.breadcrumb_quarter", doc)]
        assert not [q for q in quarters
                    if q and normalize_match_key(q) == normalize_match_key(town)]


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
    assert read("rm.det.breadcrumb_quarter", doc) == []


def test_an_unknown_kraj_slug_costs_coverage_and_never_correctness():
    """Eleven of the fifteen anchor slugs are unverified. The reader fails closed, so a wrong
    one drops the chain instead of mis-typing it — and a kraj with zero breadcrumb claims and
    non-zero listings is how it gets found."""
    doc = document(
        '<html><head><script type="application/ld+json">'
        '{"@type":"BreadcrumbList","itemListElement":['
        '{"@type":"ListItem","position":4,"item":'
        '{"@id":"https://realitymix.cz/reality/domy/pronajem/neverland",'
        '"name":"Neverland"}},'
        '{"@type":"ListItem","position":5,"item":'
        '{"@id":"https://realitymix.cz/reality/domy/pronajem/neverland/x",'
        '"name":"X"}}]}</script></head><body></body></html>')
    for entry_id in ("rm.det.breadcrumb_kraj", "rm.det.breadcrumb_quarter"):
        assert read(entry_id, doc) == []


# ------------------------------------------------------------------ the coordinate rails

def test_the_gps_pair_is_a_portal_pin_through_the_real_licence_ladder():
    reads = read("rm.det.gps")
    assert len(reads) == 1 and reads[0].position_branch == "portal_pin"
    claim = stamp_page_claim(reads[0].claim, payload(),
                             scope_version=document().scope_version)
    licensed, reason = _licensed_coordinate(claim, row(), ENTRIES["rm.det.gps"],
                                            reads[0].position_branch)
    assert licensed is not None, reason
    assert licensed.value_geom_wkt == "POINT(12.742544 50.427238)"
    assert licensed.licence_class == "portal"


def test_a_coordinate_from_any_other_entry_id_is_refused_by_the_ladder():
    """Carried over from the superseded suite. `ARCHIVED_COORDINATE_RULES` names ONE
    realitymix entry; the rung exists so a future entry cannot license a position merely by
    declaring a branch. The positive case above is not evidence for this one."""
    reads = read("rm.det.gps")
    claim = stamp_page_claim(reads[0].claim, payload(),
                             scope_version=document().scope_version)
    impostor = replace(ENTRIES["rm.det.gps"], entry_id="rm.det.not_the_rule")
    licensed, reason = _licensed_coordinate(claim, row(), impostor, "portal_pin")
    assert licensed is None and reason


def test_an_unruled_coordinate_locator_counts_a_refusal_not_a_silence():
    """The Mapy veto used to sit above the substrate branch; the entry id is the whole gate
    now. A refusal must still be COUNTED, or a refused pin is indistinguishable from a page
    that carries none."""
    impostor = replace(ENTRIES["rm.det.gps"], entry_id="rm.det.not_the_rule")
    result = extract_page(payload(), row(), [impostor], register=REGISTER)
    assert dict(result.refusals) == {"unrecognised_archived_coordinate_locator": 1}
    assert result.claims == []


def test_a_blank_gps_attribute_produces_no_declaration_at_all():
    """Carried over from the superseded suite. `data-gps-lat=""` is neither branch: the
    attribute is PRESENT, so the `:not()` selector does not match, and the coordinate reader
    has no number — an honest under-claim rather than a guess about which branch ran."""
    doc = document('<html><body><div id="print-map" data-gps-lat="" data-gps-lon="" '
                   'data-address="Zlín"></div></body></html>')
    assert read("rm.det.agency_est_flag", doc) == []
    assert read("rm.det.gps", doc) == []


def test_the_declaration_is_silent_wherever_the_portal_supplied_a_pin():
    """The honest pairing with `rm.det.gps`, which claims the pin on exactly those pages."""
    for body in EVERY_BODY:
        assert read("rm.det.agency_est_flag", document(body)) == []


# ------------------------------------------------------------------ the address line

def test_the_captured_page_quarter_is_not_published_as_a_street():
    """8662169's `data-address` segment 0 is "Stráň", a ČÁST OBCE — corroborated by
    `data-form-address` and by the breadcrumb tail — so a positional street read would
    fabricate a street here. The gate cuts both ways and the direction is deliberate: b1's
    real street "Čsl. partyzánů" fails `looks_like_czech_street` and is under-claimed, and a
    one-segment line ("Plzeňská") is refused for arity."""
    assert value("rm.det.map_street") is None
    assert value("rm.det.map_street", document(B1)) is None
    assert value("rm.det.map_street", document(A1)) is None


@pytest.mark.parametrize("address,street,okres", [
    ("Křimická, Plzeň 3, Plzeň, okres Plzeň-město", "Křimická", "Plzeň-město"),
    ("Křimická 655/31, Plzeň 3, Plzeň, okr. Plzeň-město", "Křimická", "Plzeň-město"),
    ("Plzeň 3, Plzeň, okres Plzeň-město", None, "Plzeň-město"),
    ("Zlín", None, None)])
def test_the_address_shapes_this_portal_serves(address, street, okres):
    """The okres role is keyed on the QUALIFIER, which is why `okr.` reads and a line with no
    tail claims nothing — `strip_prefix:"okres "` would have published "okr. Plzeň-město"."""
    doc = document(f'<html><body><div id="print-map" data-gps-lat="49.7" '
                   f'data-gps-lon="13.3" data-address="{address}"></div></body></html>')
    assert value("rm.det.map_street", doc) == street
    assert value("rm.det.map_okres", doc) == okres


# --------------------------------------------------------- the PII / exclusion-zone rail

def test_the_zones_strip_the_agent_the_agency_and_the_carousel_but_not_the_subject():
    """Carried over verbatim from the superseded suite — the one PII rail this portal has.
    The agency office address is the 5-digit decoy [mine-realitymix finding 8] names, the
    agent name is the page's only personal data, and `Vysočany` is the operator's own seat in
    the footer. The second half matters as much: a scoper that strips the page also makes the
    decoys unreachable and is not a fix."""
    doc = document()
    assert doc.is_complete and doc.nodes_removed == 6
    for decoy in ("Karla Čapka 1357", "35601", "Jan Novák", "Vysočany"):
        assert not doc.contains(decoy), decoy
    # The two v3 zones are RETAINED and match nothing on the served page — a zone that
    # compiles and matches nothing is not a hole, and `zones_unmatched` is what reports it.
    assert {".broker-contact", ".contact-box"} <= set(doc.zones_unmatched)
    # NOT claimed: the operator's seat also appears in the GDPR consent prose, outside the
    # footer. No entry can reach it, so it is recorded here rather than silently zoned.
    assert doc.contains("Na Harfě")
    pin = doc.css_first("div#print-map")
    assert pin is not None and pin.attributes["data-gps-lat"] == "50.427238"
    assert doc.css_first("link[rel='canonical']") is not None


# ------------------------------------------------------------------ the whole lane

def test_the_lane_over_the_captured_body_yields_exactly_these_claims():
    result = extract_page(payload(), row(), list(ENTRIES.values()), register=REGISTER)
    assert {(c.extractor_id, c.value_geom_wkt or c.value_text) for c in result.claims} == {
        ("rm.det.slug", "potucky"),
        ("rm.det.gps", "POINT(12.742544 50.427238)"),
        ("rm.det.breadcrumb_kraj", "Karlovarský kraj"),
        ("rm.det.breadcrumb_quarter", "Stráň"),
        ("rm.det.map_okres", "Karlovy Vary"),
    }
    assert not result.refusals
    assert all(c.surface == "archived_html" for c in result.claims)
    assert all(c.licence_class == "portal" for c in result.claims)


def test_every_committed_body_yields_a_town_and_every_claim_carries_its_evidence():
    """Rule 25's invariant at fixture grain: a body with no town is the red line. Migration
    382's `loc_claim_text_evidence` is the other half — a span that does not contain its
    quote is worse than no span."""
    for body in EVERY_BODY:
        doc = document(body)
        result = extract_page(payload(body), row(), list(ENTRIES.values()),
                              register=REGISTER)
        towns = [c.value_text for c in result.claims if c.claim_type == "obec_name"]
        assert len(towns) == 1 and towns[0], body.name
        for claim in result.claims:
            assert claim.evidence_quote, (body.name, claim.extractor_id)
            assert claim.span_start is not None, (body.name, claim.extractor_id)
            assert doc.html[claim.span_start:claim.span_end] == claim.evidence_quote
