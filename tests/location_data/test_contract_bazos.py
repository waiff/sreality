"""bazos@5 — the slim contract, driven through the shipped readers over real bodies.

Every assertion runs `contracts.load_all()`'s own entries (never a test-built one) against
bodies this repo already holds: the genuine capture
`tests/fixtures/portal_html/bazos_detail.html` (ad 222916664, 2026-09-05), the hand-written
`tests/fixtures/location_w2/bazos_detail.html` the fixture-diff golden scores, and the three
Lokalita layouts recorded in `tests/scraper/test_bazos_parser.py`.

THE ONE FACT THIS FILE EXISTS TO PIN: bazos' town anchor states the OKRES in its text and
the obec in its href, so the town is read from the href and never from the text. The capture
is the proof — its anchor reads "Nový Jičín" while the ad is in Frenštát pod Radhoštěm.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import tests.scraper.test_bazos_parser as bp
from location_data import contracts
from location_data.claims_common import (
    ARCHIVED_COORDINATE_RULES,
    IntakeRefused,
    apply_transforms,
)
from location_data.claims_intake import (
    DEFAULT_MAX_CLAIM_VALUE_BYTES,
    Entry,
    ListingRow,
    extract_listing,
)
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from location_data.page_readers import (
    PAGE_READERS,
    ArchivedPayload,
    _licensed_coordinate,
    stamp_page_claim,
)
from location_data.resolver.normalize import normalize_match_key
from tests.location_data import claim_intake_fixtures as fx
from tests.location_data.mini_mirror import MiniMirror, _unit

_ROOT = Path(__file__).resolve().parents[2]
_ARCHIVED = _ROOT / "tests" / "fixtures" / "portal_html" / "bazos_detail.html"
_GOLDEN_BODY = _ROOT / "tests" / "fixtures" / "location_w2" / "bazos_detail.html"

FETCHED_AT = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)

# The capture's own values, written once so a re-capture that moves any of them fails loudly
# instead of quietly re-teaching this file whatever the new page says.
NATIVE = "222916664"
LIVE_OBEC = "frenštát-pod-radhoštěm"          # the href slug, percent-decoded
LIVE_TOWN = "Frenštát pod Radhoštěm"          # what that slug IS, per the ad's og:title
LIVE_OKRES_ANCHOR_TEXT = "Nový Jičín"         # the anchor's TEXT — the okres, not the town
LIVE_PSC = "74401"
OBEC_SLUG_ENCODED = "fren%C5%A1t%C3%A1t-pod-radho%C5%A1t%C4%9Bm"
DECOY_HREF = "https://reality.bazos.cz/inzeraty/prodej-byt/"
PORTAL_WORDING = "Přibližná lokalita"
CONTRACT_LABEL = "approximate_location"

CONTRACT = {c.source: c for c in contracts.load_all()}["bazos"]
ENTRIES = {e.entry_id: e for e in fx.entries_for("bazos")}
TOWN_ENTRY = "bzs.det.obec_slug"
ENTRY_IDS = {TOWN_ENTRY, "bzs.det.psc", "bzs.det.blur_hint", "bzs.det.link_pin"}
FIRING = (TOWN_ENTRY, "bzs.det.psc", "bzs.det.blur_hint")


# ------------------------------------------------------------------ harness

def scoped(body: bytes | str) -> ScopedDocument:
    """A body through bazos' OWN shipped exclusion register — the decoys a reader must not
    reach are the ones the contract declares, never ones a test invents."""
    register = ScopeRegister.from_zones("bazos", CONTRACT.exclusion_zones)
    return scope_html(body.encode("utf-8") if isinstance(body, str) else body,
                      register=register)


def document() -> ScopedDocument:
    return scoped(_ARCHIVED.read_bytes())


def row() -> ListingRow:
    return ListingRow(
        listing_id=4242, source="bazos", source_id_native=NATIVE, raw_json={},
        lat=None, lon=None, observed_at=FETCHED_AT,
    )


def payload() -> ArchivedPayload:
    return ArchivedPayload(
        id=9001, source="bazos", source_id_native=NATIVE, page_kind="detail",
        payload_sha256="ab" * 32, first_observed_at=FETCHED_AT,
        body=_ARCHIVED.read_bytes())


def run(entry: Entry, doc: ScopedDocument | None = None) -> list[Any]:
    doc = document() if doc is None else doc
    return PAGE_READERS[entry.reader](entry, row(), payload(), doc)


def claim_of(entry_id: str, doc: ScopedDocument | None = None) -> Any:
    doc = document() if doc is None else doc
    reads = run(ENTRIES[entry_id], doc)
    assert len(reads) == 1, f"{entry_id}: expected one read, got {len(reads)}"
    return stamp_page_claim(reads[0].claim, payload(), scope_version=doc.scope_version)


# ------------------------------------------------------------------ the contract shape

def test_the_contract_is_bazos_at_version_5():
    assert (CONTRACT.source, CONTRACT.version) == ("bazos", 5)


def test_the_entry_ids_are_exactly_the_four_this_version_ships():
    assert {e.entry_id for e in CONTRACT.entries} == ENTRY_IDS


def test_one_entry_per_claim_type_and_the_town_is_among_them():
    types = [e.claim_type for e in CONTRACT.entries]
    assert sorted(types) == sorted(set(types))
    assert set(types) == {"obec_name", "psc", "precision_declaration", "coordinate"}
    assert contracts.MANDATORY_CLAIM_TYPE in types


def test_the_town_entry_reads_the_town_anchors_href():
    """The whole ruling in one assertion: the carrier is the ATTRIBUTE, not the node's text.
    `decode: percent` is what makes the value join — the encoded slug normalises to a run of
    hex bytes that matches no gazetteer row."""
    entry = ENTRIES[TOWN_ENTRY]
    assert entry.claim_type == "obec_name"
    assert entry.reader == "html_attr_regex"
    assert entry.locator["attr"] == "href"
    assert entry.locator["decode"] == "percent"
    assert entry.locator["group"] == "obec"
    # v4's prior, kept (W1-c R3): the resolver reads `default_granularity` off the
    # projection and a NULL there is a town claim that states nothing about its own grain.
    assert entry.precision_map["prior"]["granularity"] == "obec"


def test_every_entry_runs_on_the_page_lane_and_none_on_the_payload_one():
    """Rule 25 leaves one lane over two substrates, and every bazos fact this version claims
    is on the page: `raw_json` carries only the coarse Lokalita text (see the payload arm at
    the bottom), and one entry per claim type forbids a second carrier."""
    assert {e.reader for e in ENTRIES.values()} <= set(PAGE_READERS)
    assert {e.page_kind for e in ENTRIES.values()} == {"detail"}


# ------------------------------------------------------------------ the substrate itself

def test_the_archived_body_still_shows_the_defect_this_portal_has():
    """Not decoration: it pins that the capture still CARRIES the fault the town rule is
    about. The anchor's visible text is the OKRES while its href names the obec — how 29,546
    active rows ended up on 90 distinct `locality` values — and the page also carries a real
    category link with the same prefix, the adversarial node the `/<5 digits>/` tail has to
    discriminate against."""
    doc = document()
    anchors = {n.attributes.get("href"): (n.text() or "").strip()
               for n in doc.css("a[href*='/inzeraty/']")}
    subject = f"https://reality.bazos.cz/inzeraty/{OBEC_SLUG_ENCODED}/74401/"
    assert anchors[subject] == LIVE_OKRES_ANCHOR_TEXT
    assert DECOY_HREF in anchors
    assert LIVE_OBEC not in doc.html, "the obec is on the page only percent-encoded"
    # The unscoped selector leans on the zones to keep the neighbour block out, so a zone
    # that silently stopped matching would silently widen every entry below.
    assert ".podobne" not in doc.zones_unmatched


def test_the_capture_is_still_scrubbed_of_the_sellers_identity():
    """The PII rail on the one real body this portal has. The scrub is recorded in the
    fixture's own header (seller name -> 'Inzerent', phone teaser -> '000…', `idphone=0`),
    and a re-capture that forgets it must fail here rather than land in git."""
    body = _ARCHIVED.read_text(encoding="utf-8").split("-->", 1)[1]
    assert set(re.findall(r"odeslatakci\('rating'[^)]*\)", body)) == {
        "odeslatakci('rating','0','0','Inzerent')"}
    assert set(re.findall(r"(\d{3})\.\.\. zobraz", body)) == {"000"}
    assert set(re.findall(r"idphone=(\d*)", body)) <= {"0"}
    assert "mailto:" not in body and "tel:" not in body


# ------------------------------------------------------------------ per-entry extraction

def test_the_town_and_the_psc_come_off_one_href_on_the_live_capture():
    doc = document()
    town, psc = claim_of(TOWN_ENTRY, doc), claim_of("bzs.det.psc", doc)
    assert (town.claim_type, town.value_text) == ("obec_name", LIVE_OBEC)
    assert (psc.claim_type, psc.value_text) == ("psc", LIVE_PSC)
    assert town.licence_class == "portal" and town.blur_evidence == "none"
    assert "%C5" not in town.value_text


def test_the_claimed_town_is_the_ads_town_and_not_its_okres():
    """The regression this version is a fix for, stated as the thing a reader can check.

    The capture's own `og:title` names both places — "… Frenštát pod Radhoštěm, Školská
    čtvrť - Nový Jičín" — and the okres is the half the anchor TEXT publishes. The claim has
    to normalise onto the TOWN."""
    claimed = claim_of(TOWN_ENTRY).value_text
    assert normalize_match_key(claimed) == normalize_match_key(LIVE_TOWN)
    assert normalize_match_key(claimed) != normalize_match_key(LIVE_OKRES_ANCHOR_TEXT)
    head = _ARCHIVED.read_text(encoding="utf-8")
    assert f"{LIVE_TOWN}, Školská čtvrť - {LIVE_OKRES_ANCHOR_TEXT}" in head


def test_the_town_and_psc_come_off_the_body_the_golden_scores():
    """The hand-written page the fixture-diff gate scores, kept in step with the contract:
    its town anchor is `/inzeraty/praha-8/18600/`, the obvod spelling the pattern folds."""
    doc = scoped(_GOLDEN_BODY.read_bytes())
    assert claim_of(TOWN_ENTRY, doc).value_text == "praha"
    assert claim_of("bzs.det.psc", doc).value_text == "18600"


@pytest.mark.parametrize("body,town,psc", [
    (bp.LIVE_LOKALITA_DETAIL_HTML, "plzen", "32600"),   # live 3-cell, slug `plzen-26`
])
def test_the_live_lokalita_layout_yields_the_town_and_the_psc(body, town, psc):
    doc = scoped(body)
    assert claim_of(TOWN_ENTRY, doc).value_text == town
    assert claim_of("bzs.det.psc", doc).value_text == psc


@pytest.mark.parametrize("body", [bp.DETAIL_HTML, bp.DOHODOU_DETAIL_HTML])
def test_the_two_cell_layouts_carry_no_town_and_the_name_entries_stay_silent(body):
    """MEASURED, not assumed, and the size of the W1-c R6 escalation: the 2-cell Lokalita
    shapes recorded in `tests/scraper/test_bazos_parser.py` carry no
    `/inzeraty/<slug>/<psc5>/` anchor, so the two entries that read it — the TOWN and the
    PSČ — are silent. Their cell text names a place, but on the live layout that same text
    is the OKRES, so a text read would buy coverage here by publishing a wrong town
    everywhere else. Silence is the correct answer until a real body of that layout is
    captured; both constants above are hand-authored and that file records them as having
    diverged from live.

    The PIN is a different carrier and is NOT silent: where the layout still writes the maps
    anchor (`DETAIL_HTML` does, `DOHODOU_DETAIL_HTML` does not), `bzs.det.link_pin` reads it,
    so this layout yields a position without a name rather than nothing at all. That is
    coverage the pattern arm bought — before it the entry was inert on every layout."""
    doc = scoped(body)
    assert doc.css("a[href*='/inzeraty/']") == []
    for entry_id in (TOWN_ENTRY, "bzs.det.psc"):
        assert run(ENTRIES[entry_id], doc) == [], entry_id
    has_map_link = doc.css_first("a[href*='/place/']") is not None
    pin = run(ENTRIES["bzs.det.link_pin"], doc)
    assert bool(pin) is has_map_link
    # The blur hint needs the portal's own "Přibližná lokalita" title, which neither
    # hand-authored constant carries — a marker entry states the label or says nothing.
    assert run(ENTRIES["bzs.det.blur_hint"], doc) == []


def test_a_numbered_postal_district_is_folded_to_the_city_it_belongs_to():
    """R4: a numbered obvod is never the town. RÚIAN has no obec "Praha 8", so claiming the
    portal's spelling verbatim is a town-coverage hole that reads as a portal publishing no
    town — the resolver binds a town ONLY through an exact
    `admin_units_by_name(..., levels=('obec',))` lookup."""
    mirror = MiniMirror(units=[
        _unit(900, "obec", 554782, "Praha", "praha", "k19.o1100.ob554782"),
        _unit(901, "obec", 554791, "Plzeň", "plzen", "k32.o3202.ob554791"),
    ])
    praha = claim_of(TOWN_ENTRY, scoped(_GOLDEN_BODY.read_bytes())).value_text
    plzen = claim_of(TOWN_ENTRY, scoped(bp.LIVE_LOKALITA_DETAIL_HTML)).value_text
    assert mirror.admin_units_by_name(normalize_match_key(praha), levels=("obec",))
    assert mirror.admin_units_by_name(normalize_match_key(plzen), levels=("obec",))
    for unfolded in ("praha-8", "plzen-26", "Praha 8"):
        assert not mirror.admin_units_by_name(
            normalize_match_key(unfolded), levels=("obec",))


def test_the_okres_label_can_never_reach_the_statutory_city_transform():
    """The hazard this carrier retires. `statutory_city_obec` is chained per R4 because the
    entry is slug-fed, and on an OKRES label it would manufacture a big-city town out of a
    rural district — "Brno-venkov" -> "Brno", 30 km of villages claimed as the city. bazos
    publishes exactly those 76 labels as the anchor's TEXT, so the rail is that the entry
    reads the href: a page whose anchor text is "Brno-venkov" claims the href's own obec.

    TWO rails now, and the second closes the class rather than this instance: the transform
    itself refuses every hyphenated OKRES name (`_HYPHENATED_OKRES_NAMES`), so an okres that
    reaches it by any other route — `address_part_obec` runs on lines that carry one — is
    returned untouched instead of folded."""
    assert apply_transforms("Brno-venkov", ENTRIES[TOWN_ENTRY].transform) == "Brno-venkov"
    body = ('<html><body><table><tr><td>Lokalita:</td><td>'
            '<a href="https://www.google.com/maps/place/49.30,16.62/@49.30,16.62,12z" '
            'title="Přibližná lokalita" rel="nofollow">664 51</a> '
            '<a href="https://reality.bazos.cz/inzeraty/slapanice/66451/">Brno-venkov</a>'
            "</td></tr></table></body></html>")
    assert claim_of(TOWN_ENTRY, scoped(body)).value_text == "slapanice"


def test_the_pattern_and_not_the_selector_picks_the_node():
    """THE behaviour that separates `html_attr_regex` from `html_attr`: the selector matches
    the live category link too, and only the `/<5 digits>/` tail tells them apart."""
    doc = document()
    assert len(doc.css("a[href*='/inzeraty/']")) > 1
    assert claim_of(TOWN_ENTRY, doc).value_text != "prodej-byt"


def test_a_slug_without_the_five_digit_tail_is_not_an_obec():
    body = ('<html><body><a href="https://reality.bazos.cz/inzeraty/prodej-byt/">x</a>'
            '<a href="https://reality.bazos.cz/inzeraty/frenstat-pod-radhostem/">y</a>'
            "</body></html>")
    assert run(ENTRIES[TOWN_ENTRY], scoped(body)) == []


def test_the_two_slug_entries_read_one_node_and_quote_it_identically():
    """One href, two entries, two claims — which is why `group` is contract data and never
    "the only group". Reading both facts off one node is also what keeps them consistent: a
    PSČ and a town from different carriers could disagree about which place the ad is in."""
    doc = document()
    town, psc = claim_of(TOWN_ENTRY, doc), claim_of("bzs.det.psc", doc)
    assert town.evidence_quote == psc.evidence_quote
    assert (town.span_start, town.span_end) == (psc.span_start, psc.span_end)


def test_the_psc_is_normalised_to_the_five_digit_shape():
    assert ENTRIES["bzs.det.psc"].transform == ("psc_normalise",)
    claim = claim_of("bzs.det.psc")
    assert claim.value_text.isdigit() and len(claim.value_text) == 5


def test_blur_hint_claims_the_contracts_label_and_quotes_the_portals_words():
    """The portal TELLS YOU its pin is approximate. The VALUE is this contract's canonical
    label and the EVIDENCE is bazos' own wording — two fields for exactly this case, so a
    reword stops asserting instead of silently restating a different fact. R5: the page
    readers stamp `declared_precision_label` = the value for this claim type."""
    claim = claim_of("bzs.det.blur_hint")
    assert claim.claim_type == "precision_declaration"
    assert claim.value_text == CONTRACT_LABEL
    assert claim.declared_precision_label == CONTRACT_LABEL
    assert claim.evidence_quote == PORTAL_WORDING
    assert claim.blur_evidence == "declared"


def test_blur_hint_caps_the_pin_at_town_tier():
    """The operator's ruling made structural: this pin is permanently approximate and never
    convertible to an address, and `blurred_labels` is the calibration set that decides it."""
    cap = ENTRIES["bzs.det.blur_hint"].precision_map["precision_cap"]
    assert cap["granularity_max"] == "obec"
    assert cap["position_source_max"] == "portal_pin_blurred"
    assert ENTRIES["bzs.det.blur_hint"].precision_map["blurred_labels"] == [CONTRACT_LABEL]


def test_a_reworded_marker_stops_asserting_instead_of_restating():
    body = ('<html><body><table><tr><td>Lokalita:</td><td>'
            '<a href="https://www.google.com/maps/place/49.5,18.2/@49.5,18.2,12z/data=x" '
            'title="Orientační poloha" rel="nofollow">744 01</a> '
            '<a href="https://reality.bazos.cz/inzeraty/kop%C5%99ivnice/74221/">'
            "Nový Jičín</a></td></tr></table></body></html>")
    doc = scoped(body)
    assert run(ENTRIES["bzs.det.blur_hint"], doc) == []
    # ... while the town entry, which asks a different question of the same row, still reads.
    assert claim_of(TOWN_ENTRY, doc).value_text == "kopřivnice"


def test_an_unlisted_label_is_recorded_without_asserting_declared_blur():
    """Which label means "blurred" is `precision_cap.blurred_labels`, i.e. a contract version
    bump — never a code constant, and never a default."""
    entry = replace(ENTRIES["bzs.det.blur_hint"], precision_map={})
    reads = PAGE_READERS[entry.reader](entry, row(), payload(), document())
    assert len(reads) == 1
    assert reads[0].claim.value_text == CONTRACT_LABEL
    assert reads[0].claim.blur_evidence == "none"


@pytest.mark.parametrize("entry_id", FIRING)
def test_every_claim_resolves_a_span_that_indexes_its_own_quote(entry_id):
    """A claim asserting evidence it cannot point at is worse than one with no span."""
    doc = document()
    claim = claim_of(entry_id, doc)
    assert claim.span_start is not None and claim.span_end > claim.span_start
    assert doc.html[claim.span_start:claim.span_end] == claim.evidence_quote
    assert claim.subject_scoped is True


def test_a_page_without_a_lokalita_row_claims_nothing_at_all():
    """A miss on this portal is silence, not a wrong answer: every entry addresses the town
    anchor or the map link beside it, so a page carrying neither yields no claim."""
    doc = scoped('<html><body><h1 class="nadpisdetail">Prodej bytu 2+1</h1>'
                 '<div class="popisdetail">Bez lokality.</div></body></html>')
    for entry_id in ENTRY_IDS:
        assert run(ENTRIES[entry_id], doc) == [], entry_id


# ------------------------------------------------------------------ the pin

def test_the_pin_is_licensable_only_through_the_id_the_ladder_names():
    """C6 is decided in ONE place. `ARCHIVED_COORDINATE_RULES['bazos']` names
    `bzs.det.link_pin` and nothing else, so a later PR cannot license a second, unruled
    locator by simply declaring `claim_type: coordinate`."""
    rule = ARCHIVED_COORDINATE_RULES["bazos"]
    assert (rule.entry_id, rule.licence_class) == ("bzs.det.link_pin", "portal")
    assert rule.entry_id in ENTRY_IDS
    assert rule.geocoded_licence_class is None      # bazos has no geocoded map branch

    coordinate = replace(claim_of(TOWN_ENTRY), claim_type="coordinate",
                         value_geom_wkt="POINT(18.210526 49.539246)")
    licensed, reason = _licensed_coordinate(
        coordinate, row(), ENTRIES[TOWN_ENTRY], "portal_pin")
    assert licensed is None and reason == "unrecognised_archived_coordinate_locator"

    licensed, reason = _licensed_coordinate(
        coordinate, row(), ENTRIES["bzs.det.link_pin"], "portal_pin")
    assert licensed is not None and licensed.licence_class == "portal"
    assert reason == "archived_bzs.det.link_pin"


def test_the_pin_entry_reads_the_maps_link_on_every_committed_body():
    """bazos publishes its pin as a decimal pair inside ONE attribute
    (`/maps/place/49.539246,18.210526/@…`), which is what `locator.pattern` exists for: the
    `lat`/`lon` groups separate the two halves of the href that `float()` could never parse.

    It fires on BOTH committed bodies, and that is the assertion, not a shape check. The
    entry shipped for one round with a reader that ignored its pattern — `float(href)` raised
    ValueError, the reader returned silently, and bazos had no pin at all while the contract
    read as if it published one (the same PR deleted `geom_column`, the only other path).
    An entry that cannot fire is the defect this test exists to keep out."""
    entry = ENTRIES["bzs.det.link_pin"]
    assert entry.claim_type == "coordinate"
    assert entry.reader == "html_point_attrs"
    assert entry.guards == ("reject_outside_cz_bbox",)
    assert entry.precision_map["precision_cap"]["position_source_max"] == "portal_pin_blurred"
    assert "(?P<lat>" in entry.locator["pattern"] and "(?P<lon>" in entry.locator["pattern"]

    for name, body in (("portal_html", _ARCHIVED.read_bytes()),
                       ("location_w2", _GOLDEN_BODY.read_bytes())):
        doc = scoped(body)
        assert doc.css_first(entry.locator["css"]) is not None, name
        reads = run(entry, doc)
        assert len(reads) == 1, name
        read = reads[0]
        assert read.position_branch == "portal_pin", name
        lat, lon = (float(x) for x in read.claim.value_text.split(","))
        # Inside the CZ envelope the entry's own guard declares, which is what makes it a
        # pin rather than a number that happened to parse.
        assert 48.5 <= lat <= 51.1 and 12.0 <= lon <= 18.9, (name, lat, lon)
        assert read.claim.value_geom_wkt == f"POINT({lon} {lat})", name


def test_a_pin_pattern_that_names_no_lat_or_lon_group_is_refused():
    """Which capture is the latitude is contract data. A positional pattern would make the
    hemisphere depend on the order the groups happen to be written in — the same reason
    `_entry_pattern` refuses a defaulted group."""
    entry = replace(
        ENTRIES["bzs.det.link_pin"],
        locator=dict(ENTRIES["bzs.det.link_pin"].locator,
                     pattern=r"/place/(-?\d+\.\d+),(-?\d+\.\d+)"))
    with pytest.raises(IntakeRefused) as excinfo:
        run(entry, document())
    assert "lat" in str(excinfo.value) and "lon" in str(excinfo.value)


def test_an_href_the_pattern_does_not_match_is_silence_not_an_exception():
    """One page changing shape must not abort a batch of thousands."""
    doc = scoped('<html><body><table><tr><td>Lokalita:</td><td>'
                 '<a href="https://www.google.com/maps/place/Praha">Přibližná lokalita</a>'
                 '</td></tr></table></body></html>')
    assert run(ENTRIES["bzs.det.link_pin"], doc) == []


# ------------------------------------------------------------------ the payload half

@pytest.mark.parametrize("raw_json", [fx.BAZOS_LINK, fx.BAZOS_STREET_GEOCODE,
                                      fx.BAZOS_LOCALITY_GEOCODE])
def test_the_payload_half_of_the_lane_claims_nothing_on_bazos(raw_json):
    """Deliberate, and the reason the town entry is a page entry. `raw_json.locality_text` is
    the string `scraper.bazos_parser._locality` lifts out of the Lokalita CELL — the okres
    label on the live layout ("Nový Jičín" for an ad in Frenštát pod Radhoštěm). v4 typed it
    `postal_town` precisely because it is not the obec (it "disagrees with the geo-derived
    obec on 57.0% of rows"; BAZOS_LINK below says Hodonín at 696 81, which is Bzenec), and
    `postal_town` is not one of R1's eleven types — so this string has no claim type left,
    and one entry per claim type forbids keeping it as a second carrier of the town."""
    result = extract_listing(
        fx.listing("bazos", raw_json, native=str(raw_json["id"])), fx.entries_for("bazos"),
        max_value_bytes=DEFAULT_MAX_CLAIM_VALUE_BYTES)
    assert result.claims == []
    assert "locality_text" in raw_json, "the town is on the row, claimed from the page"
