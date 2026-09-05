"""bazos@2 — the four deterministic entries the W2 activation turns on, run for real.

Every assertion below drives the SHIPPED contract (`contracts.load_all()`, not a test-built
entry) through the SHIPPED readers over `tests/fixtures/portal_html/bazos_detail.html`, the
first genuinely archived bazos body in this repo (ad 222916664, captured 2026-09-05). That
substrate choice is the point of the file: the pinned `tests/fixtures/location_w2/
bazos_detail.html` is a hand-written page modelled on this very contract, so a selector that
works on it proves the SHAPE and not the population — and bazos' whole defect is a shape
nobody looked at, an anchor whose text is the okres and whose href is the obec.

What bazos@2 asserts, and what it refuses to assert:
  * obec_name + psc come out of ONE href, as two entries reading two capture groups;
  * map_zoom and blur_hint come out of the maps anchor and record only what the portal
    DECLARES about its own pin;
  * the pin's VALUE is claimed nowhere. bazos has no row in `ARCHIVED_COORDINATE_RULES`, so
    an archived bazos coordinate is refused by construction rather than by policy.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from location_data import claims_remine_archive as archive
from location_data import contracts
from location_data.claims_intake import (
    ARCHIVED_COORDINATE_RULES,
    ARCHIVE_ONLY_READERS,
    DEFAULT_MAX_CLAIM_VALUE_BYTES,
    Entry,
    ListingRow,
    extract_listing,
)
from location_data.claims_remine_archive import (
    ARCHIVE_READERS,
    ArchivedPayload,
    _licensed_coordinate,
)
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
_ARCHIVED = _ROOT / "tests" / "fixtures" / "portal_html" / "bazos_detail.html"

FETCHED_AT = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)

# The live capture's own values. Written here once so a re-capture that moves any of them
# fails loudly instead of quietly re-teaching the test whatever the new page says.
NATIVE = "222916664"
OBEC_SLUG_ENCODED = "fren%C5%A1t%C3%A1t-pod-radho%C5%A1t%C4%9Bm"
OBEC_SLUG_DECODED = "frenštát-pod-radhoštěm"
PSC = "74401"
ZOOM = "12"
OKRES_ANCHOR_TEXT = "Nový Jičín"
DECOY_HREF = "https://reality.bazos.cz/inzeraty/prodej-byt/"
PORTAL_WORDING = "Přibližná lokalita"
CONTRACT_LABEL = "approximate_location"

CONTRACT = {c.source: c for c in contracts.load_all()}["bazos"]
ENTRIES = {e.entry_id: e for e in fx.entries_for("bazos")}
ACTIVATED = ("bzs.det.obec_slug", "bzs.det.psc", "bzs.det.zoom", "bzs.det.blur_hint")


# ------------------------------------------------------------------ harness

def document() -> ScopedDocument:
    """The archived body through bazos' OWN shipped exclusion register — the decoys a reader
    must not reach are the ones the contract declares, never ones a test invents."""
    register = ScopeRegister.from_zones("bazos", CONTRACT.exclusion_zones)
    return scope_html(_ARCHIVED.read_bytes(), register=register)


def scoped(body: str) -> ScopedDocument:
    register = ScopeRegister.from_zones("bazos", CONTRACT.exclusion_zones)
    return scope_html(body.encode("utf-8"), register=register)


def row() -> ListingRow:
    return ListingRow(
        listing_id=4242, source="bazos", source_id_native=NATIVE, raw_json={},
        lat=None, lon=None, observed_at=FETCHED_AT, in_mapy_inventory=False,
        legacy_columns=dict(archive._DUMMY_LEGACY_COLUMNS))


def payload() -> ArchivedPayload:
    return ArchivedPayload(
        id=9001, source="bazos", source_id_native=NATIVE, page_kind="detail",
        payload_sha256="ab" * 32, first_observed_at=FETCHED_AT,
        body=_ARCHIVED.read_bytes())


def run(entry: Entry, doc: ScopedDocument | None = None) -> list[archive.ArchiveRead]:
    doc = document() if doc is None else doc
    return ARCHIVE_READERS[entry.reader](entry, row(), payload(), doc)


def claim_of(entry_id: str, doc: ScopedDocument | None = None) -> Any:
    reads = run(ENTRIES[entry_id], doc)
    assert len(reads) == 1, f"{entry_id}: expected one read, got {len(reads)}"
    return reads[0].claim


def relocator(entry_id: str, **locator: Any) -> Entry:
    """The shipped entry with ONE locator key overridden — for the rails that ask what would
    happen if the contract said something else, without inventing a whole entry."""
    entry = ENTRIES[entry_id]
    return replace(entry, locator=dict(entry.locator, **locator))


def span_text(doc: ScopedDocument, claim: Any) -> str | None:
    if claim.span_start is None or claim.span_end is None:
        return None
    return doc.html[claim.span_start:claim.span_end]


# ------------------------------------------------------------------ the substrate itself

def test_the_archived_body_shows_the_defect_this_contract_version_fixes():
    """Not a tautology and not decoration: it pins that the fixture still CARRIES the fault,
    so the four entries below are proven against the real failure rather than against a page
    that quietly stopped exhibiting it. The anchor's visible text is the OKRES while its href
    names the obec — how 29,546 active rows ended up on 90 distinct `locality` values."""
    doc = document()
    anchors = {n.attributes.get("href"): (n.text() or "").strip()
               for n in doc.css("a[href*='/inzeraty/']")}
    subject = f"https://reality.bazos.cz/inzeraty/{OBEC_SLUG_ENCODED}/{PSC}/"
    assert anchors[subject] == OKRES_ANCHOR_TEXT
    assert OBEC_SLUG_DECODED not in doc.html, "the obec is on the page only percent-encoded"
    # And the page carries a REAL category link with the same prefix — the adversarial node
    # the `/<5 digits>/` tail has to discriminate against, live rather than synthesised.
    assert DECOY_HREF in anchors


def test_the_exclusion_zones_still_reach_this_portals_neighbour_rail():
    """The unscoped `a[href*='/inzeraty/']` selector leans on the zones to keep the neighbour
    block out, so a zone that silently stopped matching would silently widen every entry
    below. `.podobne` is the one that carries other listings' addresses on bazos."""
    doc = document()
    assert ".podobne" not in doc.zones_unmatched


# ------------------------------------------------------------------ the four entries

def test_obec_slug_claims_the_municipality_out_of_the_town_anchor_href():
    doc = document()
    claim = claim_of("bzs.det.obec_slug", doc)
    assert claim.claim_type == "obec_name"
    assert claim.value_text == OBEC_SLUG_DECODED
    # `decode: percent` is the whole reason this joins: the encoded form normalises through
    # `location_value_norm` to a run of hex bytes that matches no gazetteer row.
    assert "%C5" not in claim.value_text
    assert claim.licence_class == "portal" and claim.blur_evidence == "none"


def test_obec_slug_lets_the_pattern_and_not_the_selector_pick_the_node():
    """THE behaviour that separates `html_attr_regex` from `html_attr`: the selector matches
    the live category link too, and only the `/<5 digits>/` tail tells them apart."""
    doc = document()
    assert len(doc.css("a[href*='/inzeraty/']")) > 1
    claim = claim_of("bzs.det.obec_slug", doc)
    assert claim.value_text != "prodej-byt"


def test_psc_reads_a_second_group_of_the_SAME_href():
    """One href, two entries, two claims — which is why `group` is contract data and never
    "the only group"."""
    claim = claim_of("bzs.det.psc")
    assert claim.claim_type == "psc" and claim.value_text == PSC
    # `psc_normalise` is the rail that keeps this byte-identical in shape to the raw_json
    # mirror `bzs.det.legacy_psc`, so W1 and W2 agree on `value_norm` for the same fact.
    assert ENTRIES["bzs.det.psc"].transform == ("psc_normalise",)
    assert claim.value_text == claim.value_text.strip() and len(claim.value_text) == 5


def test_the_two_slug_entries_read_one_node_and_quote_it_identically():
    doc = document()
    obec, psc = claim_of("bzs.det.obec_slug", doc), claim_of("bzs.det.psc", doc)
    assert obec.evidence_quote == psc.evidence_quote
    assert (obec.span_start, obec.span_end) == (psc.span_start, psc.span_end)


def test_zoom_reads_the_token_out_of_the_maps_anchor():
    claim = claim_of("bzs.det.zoom")
    assert claim.claim_type == "map_zoom" and claim.value_text == ZOOM
    # A signal that caps NOTHING — the cap lives on bzs.det.blur_hint (open question O13:
    # deriving precision from a zoom level needs an operator ruling, not a later PR).
    assert ENTRIES["bzs.det.zoom"].precision_map == {}


def test_blur_hint_claims_the_contracts_label_and_quotes_the_portals_words():
    """The portal TELLS YOU its pin is approximate and the pipeline has been discarding that
    while minting a point from the pin beside it. The VALUE is this contract's canonical
    label and the EVIDENCE is bazos' own wording — two fields for exactly this case."""
    doc = document()
    claim = claim_of("bzs.det.blur_hint", doc)
    assert claim.claim_type == "blur_hint"
    assert claim.value_text == CONTRACT_LABEL
    assert claim.declared_precision_label == CONTRACT_LABEL
    assert claim.evidence_quote == PORTAL_WORDING
    assert claim.blur_evidence == "declared"


def test_blur_hint_caps_the_pin_at_town_tier():
    """The operator's ruling made structural: this pin is permanently approximate and never
    convertible to an address, so the entry carries both axes of the cap."""
    cap = ENTRIES["bzs.det.blur_hint"].precision_map["precision_cap"]
    assert cap["granularity_max"] == "obec"
    assert cap["position_source_max"] == "portal_pin_blurred"


@pytest.mark.parametrize("entry_id", ACTIVATED)
def test_every_activated_entry_resolves_an_evidence_span_in_the_archived_body(entry_id):
    """A claim asserting evidence it cannot point at is worse than one with no span, so the
    span must both exist and index the quote it names."""
    doc = document()
    claim = claim_of(entry_id, doc)
    assert claim.span_start is not None and claim.span_end > claim.span_start
    assert span_text(doc, claim) == claim.evidence_quote
    assert claim.subject_scoped is True


# ------------------------------------------------------------------ the refusals

def test_a_reworded_marker_stops_asserting_instead_of_restating():
    """The rail that makes a portal wording change visible: `contains:` restates bazos' own
    sentence, so a reword yields NO claim rather than the same label over different words."""
    body = ('<html><body><table class="listadvalues"><tr><td>'
            '<a href="https://www.google.com/maps/place/49.5,18.2/@49.5,18.2,12z/data=x" '
            'title="Orientační poloha" rel="nofollow">744 01</a>'
            "</td></tr></table></body></html>")
    assert run(ENTRIES["bzs.det.blur_hint"], scoped(body)) == []
    # ... while the zoom entry, which asks a different question of the same node, still reads.
    assert claim_of("bzs.det.zoom", scoped(body)).value_text == ZOOM


def test_an_unlisted_label_is_recorded_without_asserting_declared_blur():
    """Which label means "blurred" is `precision_cap.blurred_labels`, i.e. a contract version
    bump — never a code constant, and never a default."""
    entry = replace(ENTRIES["bzs.det.blur_hint"], precision_map={})
    reads = ARCHIVE_READERS[entry.reader](entry, row(), payload(), document())
    assert len(reads) == 1
    assert reads[0].claim.value_text == CONTRACT_LABEL
    assert reads[0].claim.blur_evidence == "none"


def test_a_page_without_the_lokalita_row_claims_nothing_at_all():
    """A subject miss on this portal is silence, not a wrong answer: every entry addresses the
    Lokalita cell's own anchors, so a page that does not carry them yields no claim."""
    body = ('<html><body><h1 class="nadpisdetail">Prodej bytu 2+1</h1>'
            '<div class="popisdetail">Bez lokality.</div></body></html>')
    doc = scoped(body)
    for entry_id in ACTIVATED:
        assert run(ENTRIES[entry_id], doc) == [], entry_id


def test_a_slug_without_the_five_digit_tail_is_not_an_obec():
    """The discriminator, isolated: strip the PSČ tail from the subject anchor and the entry
    falls silent rather than claiming the category slug it can still see."""
    body = ('<html><body><a href="https://reality.bazos.cz/inzeraty/prodej-byt/">x</a>'
            '<a href="https://reality.bazos.cz/inzeraty/frenstat-pod-radhostem/">y</a>'
            "</body></html>")
    assert run(ENTRIES["bzs.det.obec_slug"], scoped(body)) == []


def test_an_archived_bazos_pin_is_unlicensable_by_construction():
    """The operator's ruling, enforced structurally rather than by policy: the maps-link pin
    is permanently approximate and never converted to an address, so `bzs.det.link_pin` stays
    a reserved, unminted id and bazos has NO row in `ARCHIVED_COORDINATE_RULES`. A future
    entry that named `claim_type: coordinate` would fail here, per row, at runtime."""
    assert "bazos" not in ARCHIVED_COORDINATE_RULES
    claim = claim_of("bzs.det.obec_slug")
    coordinate = replace(claim, claim_type="coordinate", value_geom_wkt="POINT(18.2 49.5)")
    licensed, reason = _licensed_coordinate(
        coordinate, row(), ENTRIES["bzs.det.obec_slug"], "portal_pin")
    assert licensed is None
    assert reason == "no_archived_coordinate_locator_on_this_portal"


# ------------------------------------------------------------------ the contract shape

def test_bazos_two_ships_shadowed():
    """`shadow` is HEADER-grain, so this freezes bazos' four already-live W1 legacy entries
    as well — the accepted price of activating the DOM contracts in one wave, cleared with
    `python -m location_data.contracts --unshadow bazos@2`."""
    assert (CONTRACT.version, CONTRACT.shadow) == (2, True)


def test_the_activation_appended_one_id_and_edited_none():
    """Entries are immutable per VERSION, not per id: v2 restates three v1 ids WITH a reader
    (the only legal way an entry gains one) and mints exactly one new id."""
    ids = [e.entry_id for e in CONTRACT.entries]
    assert ids.index("bzs.det.psc") == ids.index("bzs.det.obec_slug") + 1
    assert {e.entry_id for e in CONTRACT.entries if e.locator.get("reader")
            in ARCHIVE_ONLY_READERS} == set(ACTIVATED)


@pytest.mark.parametrize("entry_id", [
    "bzs.det.okres_text", "bzs.det.street_text", "bzs.det.mm_trailer",
    "bzs.det.psc_sentinel", "bzs.det.title_country",
])
def test_the_entries_this_version_deliberately_left_alone_stay_inert(entry_id):
    """okres (`nth: 2` is honoured by no shipped reader), the two regex street entries (the
    LLM lane supersedes the regex path) and the llm/sentinel pair. Each is declared and
    unexecuted, which is what lets bazos@3 activate them without minting new ids."""
    assert "reader" not in ENTRIES[entry_id].locator


def test_the_hourly_w1_lane_skips_every_entry_this_version_activated():
    """The failure this prevents is not hypothetical: refusing an unknown reader here is what
    took remax@3's hourly intake down. W1's substrate is `listings.raw_json`, which carries no
    DOM, so these four are SKIPPED and bazos' live legacy claims are unaffected."""
    assert {ENTRIES[e].reader for e in ACTIVATED} <= ARCHIVE_ONLY_READERS
    result = extract_listing(
        fx.listing("bazos", fx.BAZOS_LINK, native="220059906", lat=48.8489, lon=17.1325),
        fx.entries_for("bazos"), max_value_bytes=DEFAULT_MAX_CLAIM_VALUE_BYTES)
    assert not {c.extractor_id for c in result.claims} & set(ACTIVATED)
    assert {c.extractor_id for c in result.claims}, "the legacy entries must still fire"
