"""The W2 reader canon — the ten archived-HTML readers the seven portal activations share.

Every test here runs a REAL reader over a REAL body through the REAL scoper. Where a portal
has a genuinely archived page in `tests/fixtures/portal_html/` that is the substrate used,
because the pinned `tests/fixtures/location_w2/*.html` fixtures are hand-written "modelled on
the contract" pages and a selector that works on one of those proves the shape, not the
population. The branches a single archived body cannot carry (a Circle geometry, an empty
`features` array, a percent-encoded slug, a foreign pin) are built as small in-test HTML
strings from the values the recon measured — never as new fixture files, since nothing scores
those.

What each family is FOR, in one line each, because the names are deliberately about the
question and not about the portal:
  * `html_own_text`  — what this element states, versus what its subtree contains.
  * `html_regex`     — a capture group of a pattern over one node's text.
  * `html_attr_regex`— the same over an attribute, with the PATTERN as the node discriminator.
  * `html_marker`    — a portal's own marker is present; the value is the contract's label.
  * `json_*`         — one JSON document the page carries, ONE acquisition layer, and one
                       subject-selection rule (`{kind: id_match, on_miss: fail}`) that
                       replaces every positional fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from location_data import claims_remine_archive as archive
from location_data import contracts
from location_data.claims_intake import (
    Absence,
    Entry,
    IntakeRefused,
    ListingRow,
    TRANSFORMS,
    apply_transforms,
)
from location_data.claims_remine_archive import (
    ARCHIVE_READERS,
    ArchivedPayload,
    SubjectNotFound,
    extract_payload,
)
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html

_ROOT = Path(__file__).resolve().parent.parent.parent
_ARCHIVED = _ROOT / "tests" / "fixtures" / "portal_html"
_PINNED = _ROOT / "tests" / "fixtures" / "location_w2"
_REFETCH = _ROOT / "tests" / "fixtures" / "location_w2a_refetch"

FETCHED_AT = datetime(2026, 8, 13, 4, 30, tzinfo=UTC)
CONTRACTS = {c.source: c for c in contracts.load_all()}

# The subject ids the two archived bodies are keyed by. An id-matched reader picks its object
# by this value, so a test that scored them under a synthetic native id would go green empty —
# which is the one failure mode the archived arm exists to prevent.
IDNES_NATIVE = "6a71888887e5da33ca081ad8"
MMREALITY_NATIVE = "951845"
MMREALITY_NEIGHBOUR = "950647"

ID_MATCH = {"json_pointer": "/properties/id", "equals_row_field": "source_id_native"}
BLOB_MATCH = {"json_pointer": "/id", "equals_row_field": "source_id_native"}
NOT_SIMILAR = {"json_pointer": "/properties/isSimilar", "equals": True}
ID_SCOPE = {"kind": "id_match", "on_miss": "fail", "subject_scoped": True}

MAXIMA_SCRIPT_MATCH = "JSON\\.parse\\('(?P<config>(?:[^'\\\\]|\\\\.)*)'\\)"
KRAJ_SLUGS = [
    "praha", "hlavni-mesto-praha", "stredocesky", "jihocesky", "plzensky", "karlovarsky",
    "ustecky", "liberecky", "kralovehradecky", "pardubicky", "vysocina", "jihomoravsky",
    "olomoucky", "zlinsky", "moravskoslezsky",
]


# ------------------------------------------------------------------ harness

def entry(
    source: str,
    locator: dict[str, Any],
    *,
    entry_id: str | None = None,
    claim_type: str = "street_name",
    extraction_method: str = "html_selector_parse",
    surface: str = "html_selector",
    subject_scope: dict[str, Any] | None = None,
    transform: tuple[str, ...] = (),
    precision_map: dict[str, Any] | None = None,
    blur_evidence: str = "none",
    guards: tuple[str, ...] = (),
) -> Entry:
    return Entry(
        id=8100, source=source, contract_id=1, contract_version=1,
        entry_id=entry_id or f"{source[:2]}.det.test", surface=surface, page_kind="detail",
        locator=locator, claim_type=claim_type, extraction_method=extraction_method,
        subject_scope=subject_scope or {}, transform=transform,
        precision_map=precision_map or {}, default_blur_evidence=blur_evidence,
        default_licence_class="portal", cardinality="one", guards=guards)


def listing_row(source: str, native: str) -> ListingRow:
    return ListingRow(
        listing_id=4242, source=source, source_id_native=native, raw_json={},
        lat=None, lon=None, observed_at=FETCHED_AT, in_mapy_inventory=False,
        legacy_columns=dict(archive._DUMMY_LEGACY_COLUMNS))


def payload(source: str, native: str, body: bytes | None = None) -> ArchivedPayload:
    return ArchivedPayload(
        id=9001, source=source, source_id_native=native, page_kind="detail",
        payload_sha256="ab" * 32, first_observed_at=FETCHED_AT, body=body)


def scoped(source: str, body: bytes | str) -> ScopedDocument:
    """The body through that portal's OWN shipped exclusion register — the decoys a reader
    must not be able to reach are the ones the contract declares, not ones a test invents."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    register = ScopeRegister.from_zones(source, CONTRACTS[source].exclusion_zones)
    return scope_html(body, register=register)


def archived(source: str) -> ScopedDocument:
    return scoped(source, (_ARCHIVED / f"{source}_detail.html").read_bytes())


def pinned(source: str) -> ScopedDocument:
    return scoped(source, (_PINNED / f"{source}_detail.html").read_bytes())


def read(
    name: str, document: ScopedDocument, item: Entry, *, native: str = "fixture",
) -> list[archive.ArchiveRead]:
    return ARCHIVE_READERS[name](
        item, listing_row(item.source, native), payload(item.source, native), document)


def one(reads: list[archive.ArchiveRead]) -> Any:
    assert len(reads) == 1, f"expected exactly one read, got {len(reads)}"
    return reads[0].claim


def span_text(document: ScopedDocument, claim: Any) -> str | None:
    if claim.span_start is None or claim.span_end is None:
        return None
    return document.html[claim.span_start:claim.span_end]


# ------------------------------------------------------------------ html_own_text

def test_html_own_text_reads_the_header_without_its_nested_jump_link():
    """The measured reason this reader exists. On 12/12 mined remax pages
    `h2.pd-header__address` nests `<a …>mapa <i></i></a>`, so `html_text`'s deep read states
    the subject's address as "ulice Pod Slovany, <15 tabs> Úvaly mapa" — the chrome's label
    plus a tab run, on every remax listing."""
    document = archived("remax")
    claim = one(read("html_own_text", document,
                     entry("remax", {"reader": "html_own_text",
                                     "css": "h2.pd-header__address"},
                           claim_type="address_line_verbatim")))
    assert claim.value_text == "ulice Pod Slovany, Úvaly"
    assert "mapa" not in claim.value_text and "\t" not in claim.value_text
    assert claim.span_start is not None
    assert span_text(document, claim).startswith("ulice Pod Slovany,")


def test_html_own_text_and_html_text_disagree_on_this_page():
    """A negative control. If remax ever drops the jump-link this test says the new reader
    stopped being load-bearing, instead of the two silently agreeing forever."""
    document = archived("remax")
    locator = {"css": "h2.pd-header__address"}
    deep = one(read("html_text", document,
                    entry("remax", dict(locator, reader="html_text"),
                          claim_type="address_line_verbatim")))
    own = one(read("html_own_text", document,
                   entry("remax", dict(locator, reader="html_own_text"),
                         claim_type="address_line_verbatim")))
    assert "mapa" in deep.value_text and "mapa" not in own.value_text
    # And the deep read cannot even be evidenced: "Úvaly mapa" is not contiguous in the
    # source (the link's tag sits between the two words), so it resolves to no span at all.
    assert deep.span_start is None
    assert own.span_start is not None


def test_html_own_text_captures_the_non_ulice_header_form():
    """7 of the 12 mined pages carry `<Obec> – část obce <X>` rather than `ulice <Street>`,
    a form the single archived body cannot show."""
    body = ('<html><body><h2 class="pd-header__address">Bílovec – část obce Ohrada '
            '<a href="#">mapa <i></i></a></h2></body></html>')
    claim = one(read("html_own_text", scoped("remax", body),
                     entry("remax", {"reader": "html_own_text",
                                     "css": "h2.pd-header__address"},
                           claim_type="address_line_verbatim")))
    assert claim.value_text == "Bílovec – část obce Ohrada"


# ------------------------------------------------------------------ html_regex

def _title_regex_entry(pattern: str, group: str, claim_type: str) -> Entry:
    return entry("ceskereality",
                 {"reader": "html_regex", "css": "title", "pattern": pattern,
                  "group": group},
                 claim_type=claim_type, extraction_method="regex_text")


def test_html_regex_reads_the_accented_street_from_a_real_archived_body():
    """The repair this reader ships for: 862 of 40,147 ceskereality streets carry diacritics
    (2.1%), and the `<title>` is where the accented form survives."""
    body = (_REFETCH / "ceskereality_b1.html").read_bytes()
    document = scoped("ceskereality", body)
    claim = one(read("html_regex", document,
                     _title_regex_entry(", ulice (?P<street>[^,]+),", "street",
                                        "street_name")))
    assert claim.value_text == "Májová" != "Majova"
    # The quote is the WHOLE MATCH, not the bare street: a street name occurs in several
    # places on a portal page and `find_span` takes the first occurrence inside the node, so
    # quoting the match is what keeps the span pointing at the pattern that produced it.
    assert claim.evidence_quote == ", ulice Májová,"
    assert span_text(document, claim) == claim.evidence_quote


def test_a_town_only_title_yields_no_street_claim():
    """The granularity axis, asserted rather than assumed: a title without `, ulice X,` is a
    town-tier page and must produce no street at all."""
    document = scoped("ceskereality", (_REFETCH / "ceskereality_a1.html").read_bytes())
    assert read("html_regex", document,
                _title_regex_entry(", ulice (?P<street>[^,]+),", "street",
                                   "street_name")) == []


@pytest.mark.parametrize(
    "fixture,expected",
    [("ceskereality_b1.html", "Karlovy Vary"), ("ceskereality_a1.html", "Trutnov")])
def test_html_regex_reads_the_declared_okres_from_a_real_archived_body(fixture, expected):
    document = scoped("ceskereality", (_REFETCH / fixture).read_bytes())
    claim = one(read("html_regex", document,
                     _title_regex_entry(
                         ",\\s*okres\\s+(?P<okres>[^,]+?)\\s+-\\s+ČESKÉREALITY\\.cz",
                         "okres", "okres_name")))
    assert claim.value_text == expected


@pytest.mark.parametrize("okres", ["Frýdek-Místek", "Praha-východ"])
def test_the_okres_pattern_terminates_on_the_branding_not_on_a_hyphen(okres):
    """Czech okres names carry UNSPACED hyphens, so the pattern has to stop on the spaced
    ` - ` plus the site suffix. A `[^-]` class would truncate both of these."""
    body = (f"<html><head><title>Prodej bytu 2+1, 59 m², Město, okres {okres} - "
            f"ČESKÉREALITY.cz inzerce realit</title></head><body></body></html>")
    claim = one(read("html_regex", scoped("ceskereality", body),
                     _title_regex_entry(
                         ",\\s*okres\\s+(?P<okres>[^,]+?)\\s+-\\s+ČESKÉREALITY\\.cz",
                         "okres", "okres_name")))
    assert claim.value_text == okres


def test_html_regex_refuses_a_missing_pattern_a_missing_group_or_an_undefined_group():
    document = scoped("ceskereality", "<html><head><title>x</title></head></html>")
    for locator in (
        {"reader": "html_regex", "css": "title", "group": "street"},
        {"reader": "html_regex", "css": "title", "pattern": ", ulice (?P<street>[^,]+),"},
        {"reader": "html_regex", "css": "title", "pattern": ", ulice (?P<street>[^,]+),",
         "group": "obec"},
        {"reader": "html_regex", "css": "title", "pattern": "(?P<x>", "group": "x"},
    ):
        with pytest.raises(IntakeRefused) as excinfo:
            read("html_regex", document,
                 entry("ceskereality", locator, entry_id="cr.det.title_line",
                       extraction_method="regex_text"))
        assert "cr.det.title_line" in str(excinfo.value), (
            "the refusal must name the ENTRY, so a contract typo is findable without "
            "reading a stack trace out of a batch that rolled back")


def test_html_regex_emits_no_claim_when_the_match_has_no_locatable_span(monkeypatch):
    """A span-less `regex_text` claim reaches `assert_evidence_complete`, which RAISES — and
    that refusal aborts the whole batch transaction. One page that will not yield a locatable
    span must be zero claims, never an outage."""
    document = scoped("ceskereality", (_REFETCH / "ceskereality_b1.html").read_bytes())
    monkeypatch.setattr(type(document), "find_span",
                        lambda self, value, within=None: None)
    assert read("html_regex", document,
                _title_regex_entry(", ulice (?P<street>[^,]+),", "street",
                                   "street_name")) == []


# ------------------------------------------------------------------ html_attr_regex

def _slug_entry(group: str, **overrides: Any) -> Entry:
    locator = {"reader": "html_attr_regex", "css": "a[href*='/inzeraty/']", "attr": "href",
               "pattern": "/inzeraty/(?P<obec_slug>[^/]+)/(?P<psc>\\d{5})/",
               "group": group, "decode": "percent"}
    locator.update(overrides.pop("locator", {}))
    return entry("bazos", locator, claim_type=overrides.pop("claim_type", "obec_name"),
                 extraction_method="url_slug_parse", surface="url_slug", **overrides)


def test_html_attr_regex_reads_the_obec_out_of_the_town_anchor_href():
    """bazos names the true municipality nowhere on the page except this href: the anchor's
    visible TEXT is the okres, which is how 29,546 active rows ended up on 90 distinct
    `locality` values."""
    document = pinned("bazos")
    claim = one(read("html_attr_regex", document, _slug_entry("obec_slug")))
    assert claim.value_text == "praha-8"
    assert span_text(document, claim) == claim.evidence_quote
    assert "/inzeraty/" in claim.evidence_quote


def test_html_attr_regex_lets_the_pattern_and_not_the_selector_pick_the_node():
    """THE behaviour that separates this from `html_attr`. The breadcrumb's
    `/inzeraty/praha/` category link comes FIRST in document order, so `css_first` would
    claim `praha`; the `/<5 digits>/` tail is the discriminator."""
    document = pinned("bazos")
    assert document.css("a[href*='/inzeraty/']")[0].attributes["href"] == "/inzeraty/praha/"
    claim = one(read("html_attr_regex", document, _slug_entry("obec_slug")))
    assert claim.value_text != "praha"


def test_html_attr_regex_reads_a_second_group_of_the_same_href():
    """One href, two entries, two claims — which is why the group is contract data and never
    "the only group"."""
    claim = one(read("html_attr_regex", pinned("bazos"),
                     _slug_entry("psc", claim_type="psc", transform=("psc_normalise",))))
    assert claim.value_text == "18600"


def test_html_attr_regex_percent_decodes_when_the_contract_says_so():
    """`ho%C5%99ice-v-podkrkono%C5%A1%C3%AD` normalises through `location_value_norm` to
    `ho c5 99ice v podkrkono c5 a1 c3 ad`, which joins to no gazetteer row; the decoded form
    normalises to `horice v podkrkonosi`, which does. Values are the live capture recorded
    for ad 222223928."""
    body = ('<html><body><table class="listadvalues"><tr><td>'
            '<a href="https://reality.bazos.cz/inzeraty/'
            'ho%C5%99ice-v-podkrkono%C5%A1%C3%AD/50801/">Jičín</a>'
            "</td></tr></table></body></html>")
    document = scoped("bazos", body)
    claim = one(read("html_attr_regex", document, _slug_entry("obec_slug")))
    assert claim.value_text == "hořice-v-podkrkonoší"
    assert "%C5" not in claim.value_text
    # And the QUOTE is the node's serialisation, because the decoded slug appears nowhere in
    # the body — the same call `html_point_attrs` makes about an assembled "lat,lon".
    assert span_text(document, claim) == claim.evidence_quote


def test_html_attr_regex_without_the_decode_leaves_the_slug_encoded():
    """Opt-in, and visibly so: percent-decoding is a property of a URL substrate, not of
    every attribute a pattern may be run over."""
    body = ('<html><body><a href="/inzeraty/ho%C5%99ice/50801/">x</a></body></html>')
    claim = one(read("html_attr_regex", scoped("bazos", body),
                     _slug_entry("obec_slug", locator={"decode": "none"})))
    assert claim.value_text == "ho%C5%99ice"


def test_html_attr_regex_refuses_an_unimplemented_decode():
    with pytest.raises(IntakeRefused) as excinfo:
        read("html_attr_regex", pinned("bazos"),
             _slug_entry("obec_slug", locator={"decode": "rot13"}))
    assert "rot13" in str(excinfo.value)


def test_html_attr_regex_reads_the_zoom_token_from_the_maps_anchor():
    claim = one(read("html_attr_regex", pinned("bazos"),
                     entry("bazos",
                           {"reader": "html_attr_regex", "css": "a[href*='/place/']",
                            "attr": "href", "pattern": ",(?P<zoom>\\d+)z", "group": "zoom"},
                           claim_type="map_zoom", extraction_method="url_slug_parse",
                           surface="url_slug")))
    assert claim.value_text == "15"


# ------------------------------------------------------------------ html_marker

def _marker_entry(source: str, locator: dict[str, Any], blurred: list[str]) -> Entry:
    return entry(source, dict(locator, reader="html_marker"),
                 claim_type="blur_hint", extraction_method="portal_declared_quality",
                 precision_map={"blurred_labels": blurred})


def test_html_marker_reads_an_attribute_marker_and_derives_declared_blur():
    """bazos' maps anchor carries `title="Přibližná lokalita"` — the portal states its own pin
    is approximate and the pipeline has been discarding that while minting a point from the
    pin beside it."""
    document = pinned("bazos")
    claim = one(read("html_marker", document,
                     _marker_entry("bazos",
                                   {"css": "a[href*='/place/']", "attr": "title",
                                    "value_label": "approximate_location"},
                                   ["approximate_location"])))
    assert claim.value_text == "approximate_location"
    assert claim.declared_precision_label == "approximate_location"
    assert claim.blur_evidence == "declared"
    # The VALUE is the contract's label; the EVIDENCE is the portal's own words.
    assert claim.evidence_quote == "Přibližná lokalita"
    assert span_text(document, claim) == claim.evidence_quote


def test_html_marker_does_not_assert_blur_for_an_unlisted_label():
    """The rail that stops a portal wording change asserting declared blur forever: which
    label means blurred is `precision_cap.blurred_labels`, i.e. a contract version bump."""
    claim = one(read("html_marker", pinned("bazos"),
                     _marker_entry("bazos",
                                   {"css": "a[href*='/place/']", "attr": "title",
                                    "value_label": "approximate_location"}, [])))
    assert claim.blur_evidence == "none"


def test_html_marker_matches_a_sentence_across_source_lines():
    """idnes' disclaimer is page prose, so the comparison is whitespace-collapsed on both
    sides — `str.split()` would miss the zero-width space a scrubbed archive body carries."""
    sentence = "Nemovitost nemá přesnou adresu, nachází se ve vyznačené oblasti."
    document = pinned("idnes")
    claim = one(read("html_marker", document,
                     _marker_entry("idnes",
                                   {"css": "body", "contains": sentence,
                                    "value_label": "no_exact_address"},
                                   ["no_exact_address"]),
                     native=IDNES_NATIVE))
    assert claim.value_text == "no_exact_address"
    assert claim.blur_evidence == "declared"
    assert span_text(document, claim) == sentence
    # And the same sentence broken across source lines still matches, which `str.split()`
    # would also manage — but a zero-width space it would not, and a scrubbed archive body
    # carries both. One normalisation, `html_scope.collapse_ws`, for the whole lane.
    broken = ("<html><body><p>Nemovitost nemá\n   přesnou adresu,​ nachází se\n"
              "   ve vyznačené oblasti.</p></body></html>")
    broken_document = scoped("idnes", broken)
    broken_claim = one(read("html_marker", broken_document,
                            _marker_entry("idnes",
                                          {"css": "body", "contains": sentence,
                                           "value_label": "no_exact_address"},
                                          ["no_exact_address"]), native=IDNES_NATIVE))
    # The quote is the contract's literal while the SPAN indexes the uncollapsed source, so
    # the span is longer than the quote. That is correct, not a defect.
    assert "​" in span_text(broken_document, broken_claim)


def test_html_marker_emits_nothing_when_the_marker_is_absent():
    body = "<html><body><p>Adresa je přesná.</p></body></html>"
    assert read("html_marker", scoped("idnes", body),
                _marker_entry("idnes",
                              {"css": "body",
                               "contains": "Nemovitost nemá přesnou adresu",
                               "value_label": "no_exact_address"},
                              ["no_exact_address"])) == []


def test_html_marker_reads_an_attribute_PAIR_as_one_presence_signal():
    """realitymix's map JS branches on `if (gpsLat && gpsLon) show('--gps') else
    nominatim(address)`, and BOTH sentence blocks are served on every page — so the honest
    discriminator is the portal's own predicate, the presence of the attribute pair."""
    document = archived("realitymix")
    marker = _marker_entry(
        "realitymix",
        {"css": "div#print-map", "attr": ["data-gps-lat", "data-gps-lon"],
         "value_label": "gps"}, ["estimated"])
    claim = one(read("html_marker", document, marker))
    assert claim.value_text == "gps" and claim.blur_evidence == "none"
    assert span_text(document, claim) == claim.evidence_quote
    # One attribute missing means the pair is not there, which is the OTHER branch — and this
    # entry says nothing about it rather than inventing the absent label.
    body = '<html><body><div id="print-map" data-gps-lat="50.4"></div></body></html>'
    assert read("html_marker", scoped("realitymix", body), marker) == []


def test_html_marker_refuses_an_entry_with_no_value_label():
    with pytest.raises(IntakeRefused) as excinfo:
        read("html_marker", pinned("bazos"),
             entry("bazos", {"reader": "html_marker", "css": "a[href*='/place/']"},
                   claim_type="blur_hint", extraction_method="portal_declared_quality"))
    assert "value_label" in str(excinfo.value)


# ------------------------------------------------------------------ json_scalar

def _blob_entry(pointer: str, **overrides: Any) -> Entry:
    locator = {"reader": "json_scalar", "css": "[\\:property]", "attr": ":property",
               "json_pointer": pointer, "match": BLOB_MATCH}
    locator.update(overrides.pop("locator", {}))
    return entry("mmreality", locator, surface="embedded_json",
                 extraction_method=overrides.pop("extraction_method",
                                                 "portal_structured_field"),
                 subject_scope=ID_SCOPE, **overrides)


def test_json_scalar_reads_the_subject_blobs_scalar_and_quotes_the_member_source():
    """mmreality JSON-escapes every accented value, so the DECODED value ("Andělská Hora") is
    NOT a substring of the scoped payload — a claim quoting it would carry no span, and for a
    `regex_text` sibling that is an aborted batch. The quote is therefore the JSON MEMBER
    SOURCE SLICE, which `find_span` resolves through its `&quot;` tolerance."""
    document = archived("mmreality")
    claim = one(read("json_scalar", document, _blob_entry("/municipality",
                                                          claim_type="obec_name"),
                     native=MMREALITY_NATIVE))
    assert claim.value_text == "Andělská Hora"
    assert claim.evidence_quote.startswith('"municipality":')
    assert "\\u011b" in claim.evidence_quote, "the SOURCE spelling, not the decoded one"
    assert claim.span_start is not None and claim.span_end > claim.span_start
    assert "&quot;" in span_text(document, claim)


def test_json_scalar_fills_value_num_when_the_contract_says_the_value_is_a_number():
    claim = one(read("json_scalar", archived("mmreality"),
                     _blob_entry("/municipalityId", claim_type="obec_code",
                                 locator={"value_kind": "num"}),
                     native=MMREALITY_NATIVE))
    assert claim.value_text == "551929" and claim.value_num == 551929.0


def test_json_scalar_reads_a_plain_pointer_with_no_subject_match_at_all():
    """The same reader, un-narrowed: idnes' map config carries the zoom and the portal's own
    precision sentence at fixed pointers, and forcing a match onto those would be a rule with
    nothing to select from."""
    document = pinned("idnes")
    zoom = one(read("json_scalar", document,
                    entry("idnes",
                          {"reader": "json_scalar", "css": "script[data-maptiler-json]",
                           "json_pointer": "/mtMapOptions/zoom", "value_kind": "num"},
                          claim_type="map_zoom", extraction_method="map_widget_parse",
                          surface="embedded_json"), native=IDNES_NATIVE))
    assert zoom.value_text == "14" and zoom.value_num == 14.0
    assert zoom.evidence_quote == '"zoom": 14'
    info = one(read("json_scalar", document,
                    entry("idnes",
                          {"reader": "json_scalar", "css": "script[data-maptiler-json]",
                           "json_pointer": "/infoText"},
                          claim_type="precision_declaration",
                          extraction_method="portal_declared_quality",
                          surface="embedded_json"), native=IDNES_NATIVE))
    assert info.value_text.startswith("Nemovitost nemá přesnou adresu")
    # A generic scalar reader must NOT invent a label: idnes writes two different Czech
    # SENTENCES here, and mapping a sentence onto a label is contract calibration.
    assert info.declared_precision_label is None


def test_json_scalar_reads_the_subject_features_address_and_not_a_neighbours():
    """idnes ships 20 neighbour features per page, each with a complete address. Positional
    selection is precisely how a neighbour's address becomes this listing's street."""
    claim = one(read("json_scalar", pinned("idnes"),
                     entry("idnes",
                           {"reader": "json_scalar", "css": "script[data-maptiler-json]",
                            "then": "/geojson/features", "match": ID_MATCH,
                            "exclude_where": NOT_SIMILAR,
                            "json_pointer": "/properties/address"},
                           claim_type="address_line_verbatim",
                           extraction_method="map_widget_parse", surface="embedded_json",
                           subject_scope=ID_SCOPE), native=IDNES_NATIVE))
    assert claim.value_text.startswith("Na Balkáně")
    assert "Krkonošská" not in claim.value_text


# ------------------------------------------------------------------ json_regex

_ESCAPED_BLOB = (
    '<html><body><vue-property-detail :property=\'{"id":"fixture",'
    '"originalTitle":"Prodej bytu 3+kk 68 m2, ul. K\\u0159i\\u017e\\u00edkova",'
    '"title":"Prodej, Byt 3+kk, 68 m\\u00b2, Praha",'
    '"street":"K\\u0159i\\u017e\\u00edkova","accurate":false,'
    '"point":{"latitude":50.09239,"longitude":14.45118}}\'>'
    "</vue-property-detail></body></html>"
)


def test_json_regex_claims_the_capture_and_quotes_the_member_it_ran_over():
    """`raw_json.street` is populated on 1/12 sampled mmreality rows while `originalTitle`
    carries `ul. <Street>` on 5/12. The regex runs over the DECODED string — a pattern must
    not have to know the portal's escaping — while the quote is the member's SOURCE slice."""
    document = scoped("mmreality", _ESCAPED_BLOB)
    claim = one(read("json_regex", document,
                     entry("mmreality",
                           {"reader": "json_regex", "css": "[\\:property]",
                            "attr": ":property", "json_pointer": "/originalTitle",
                            "pattern": ",\\s*ul\\.\\s*(?P<street>[^,]+)$",
                            "group": "street", "match": BLOB_MATCH},
                           extraction_method="regex_text", surface="embedded_json",
                           subject_scope=ID_SCOPE)))
    assert claim.value_text == "Křižíkova"
    assert claim.evidence_quote.startswith('"originalTitle":')
    assert claim.span_start is not None and claim.span_end > claim.span_start


def test_json_regex_emits_nothing_rather_than_raising_when_the_span_misses(monkeypatch):
    document = scoped("mmreality", _ESCAPED_BLOB)
    monkeypatch.setattr(type(document), "find_span",
                        lambda self, value, within=None: None)
    assert read("json_regex", document,
                entry("mmreality",
                      {"reader": "json_regex", "css": "[\\:property]",
                       "attr": ":property", "json_pointer": "/originalTitle",
                       "pattern": ",\\s*ul\\.\\s*(?P<street>[^,]+)$", "group": "street",
                       "match": BLOB_MATCH},
                      extraction_method="regex_text", surface="embedded_json",
                      subject_scope=ID_SCOPE)) == []


# ------------------------------------------------------------------ json_bool

def _bool_entry(blurred: list[str]) -> Entry:
    return entry("mmreality",
                 {"reader": "json_bool", "css": "[\\:property]", "attr": ":property",
                  "json_pointer": "/accurate",
                  "labels": {"true": "accurate", "false": "not_accurate"},
                  "match": BLOB_MATCH},
                 claim_type="precision_declaration",
                 extraction_method="portal_declared_quality", surface="embedded_json",
                 subject_scope=ID_SCOPE, precision_map={"blurred_labels": blurred})


def test_json_bool_maps_the_flag_to_the_contracts_label():
    claim = one(read("json_bool", archived("mmreality"), _bool_entry(["not_accurate"]),
                     native=MMREALITY_NATIVE))
    assert claim.value_text == "accurate" and claim.value_num == 1.0
    assert claim.declared_precision_label == "accurate"
    assert claim.blur_evidence == "none"
    assert claim.evidence_quote == '"accurate":true'


def test_json_bool_declares_blur_on_the_false_branch_when_the_contract_says_so():
    """3,917 of 10,538 active mmreality rows are `accurate:false`, and that cohort shares a
    pin 49.8% of the time against 13.2% for `true` — the profile of a centroid fallback."""
    claim = one(read("json_bool", scoped("mmreality", _ESCAPED_BLOB),
                     _bool_entry(["not_accurate"])))
    assert claim.value_text == "not_accurate" and claim.value_num == 0.0
    assert claim.blur_evidence == "declared"


def test_json_bool_refuses_an_entry_that_names_only_one_label():
    with pytest.raises(IntakeRefused):
        read("json_bool", scoped("mmreality", _ESCAPED_BLOB),
             entry("mmreality",
                   {"reader": "json_bool", "css": "[\\:property]", "attr": ":property",
                    "json_pointer": "/accurate", "labels": {"true": "accurate"},
                    "match": BLOB_MATCH},
                   claim_type="precision_declaration",
                   extraction_method="portal_declared_quality", surface="embedded_json",
                   subject_scope=ID_SCOPE))


# ------------------------------------------------------------------ json_point

def _point_pair_entry(**overrides: Any) -> Entry:
    locator = {"reader": "json_point", "css": "[\\:property]", "attr": ":property",
               "lat_pointer": "/point/latitude", "lon_pointer": "/point/longitude",
               "position_branch": "portal_pin", "match": BLOB_MATCH}
    locator.update(overrides.pop("locator", {}))
    return entry("mmreality", locator, claim_type="coordinate",
                 extraction_method="portal_structured_field", surface="embedded_json",
                 subject_scope=overrides.pop("subject_scope", ID_SCOPE),
                 guards=("reject_outside_cz_bbox",), **overrides)


def test_json_point_reads_the_subject_blob_and_never_the_largest_one():
    """The measured cause of the mmreality bump: the committed archived body carries THREE
    `:property` blobs and the NEIGHBOUR's (950647, 29,386 source chars) is LARGER than the
    subject's, so `mmreality_parser`'s largest-blob fallback returns Ludvíkov's position for
    an Andělská Hora listing."""
    document = archived("mmreality")
    blobs = document.css("[\\:property]")
    assert len(blobs) == 3
    assert len(blobs[2].attributes[":property"]) > len(blobs[0].attributes[":property"])
    reads = read("json_point", document, _point_pair_entry(), native=MMREALITY_NATIVE)
    claim = one(reads)
    assert claim.value_text == "50.060813844,17.389086312"
    assert claim.value_geom_wkt == "POINT(17.389086312 50.060813844)"
    assert reads[0].position_branch == "portal_pin"
    assert claim.evidence_quote.startswith('"point":{')
    assert claim.span_start is not None


def test_json_point_selection_is_genuinely_id_driven_and_not_positional():
    """Scored under the NEIGHBOUR's id the same locator returns the neighbour's blob. If the
    selector were positional this test would return the subject either way."""
    claim = one(read("json_point", archived("mmreality"), _point_pair_entry(),
                     native=MMREALITY_NEIGHBOUR))
    assert claim.value_text == "50.113874456,17.347457655"


def test_json_point_reads_a_geojson_feature_in_rfc_7946_order():
    """A pointer PAIR takes its axis order from the contract because nothing in the document
    states it. GeoJSON does state it — RFC 7946 fixes `[lon, lat]` — so there the order is the
    FORMAT and must not be taken as data."""
    document = pinned("idnes")
    reads = read("json_point", document, _geojson_entry(), native=IDNES_NATIVE)
    claim = one(reads)
    assert claim.value_text == "50.74437214,15.31331632"
    assert claim.value_geom_wkt == "POINT(15.31331632 50.74437214)"
    # The quote is the array literal as written (~26 chars), not the 13 KB config: an
    # evidence quote rides in the same jsonb array as the claim and is size-capped with it.
    assert claim.evidence_quote == "[15.31331632, 50.74437214]"
    assert span_text(document, claim) == claim.evidence_quote


def _geojson_entry(**overrides: Any) -> Entry:
    locator = {"reader": "json_point", "css": "script[data-maptiler-json]",
               "then": "/geojson/features", "match": ID_MATCH,
               "exclude_where": NOT_SIMILAR, "feature": "/geometry",
               "position_branch": "portal_pin"}
    locator.update(overrides.pop("locator", {}))
    return entry("idnes", locator, claim_type="coordinate",
                 extraction_method="map_widget_parse", surface="embedded_json",
                 subject_scope=ID_SCOPE, guards=("reject_outside_cz_bbox",), **overrides)


def test_json_point_refuses_a_geometry_that_is_not_a_point():
    """A marked area is a different claim type; reading its first vertex as a pin would be a
    fabrication."""
    body = _idnes_body(geometry='{"type":"Polygon","coordinates":[[[15.3,50.7],[15.4,50.8]]]}')
    assert read("json_point", scoped("idnes", body), _geojson_entry(),
                native=IDNES_NATIVE) == []


def _idnes_body(
    *, geometry: str = '{"type":"Point","coordinates":[15.31331632, 50.74437214]}',
    native: str = IDNES_NATIVE, similar: str = "false", extra: str = "",
) -> str:
    return (
        '<html><body><script type="application/json" data-maptiler-json>'
        '{"mtMapOptions": {"zoom": 14}, "geojson": {"type": "FeatureCollection",'
        f'"features": [{{"type": "Feature", "geometry": {geometry},'
        f'"properties": {{"id": "{native}", "address": "Na Balkáně",'
        f'"isSimilar": {similar}}}}}{extra}]}}}}'
        "</script></body></html>")


@pytest.mark.parametrize(
    "point,admitted",
    [("[16.61109, 49.19186]", False),   # the declared Brno-centre junk pin, at 5 dp
     ("[16.61209, 49.19286]", True),    # 0.001° away — a different place, admitted
     ("[14.12853, 50.12413]", True)])   # a legitimate development cluster, NOT on the list
def test_json_point_rejects_only_the_pins_the_contract_enumerates(point, admitted):
    """Junk pins are calibration DATA on the contract, never a code constant and never
    inferred from pin-sharing: 119 idnes rows sit on 49.19186,16.61109 (Brno centre, street
    NULL) while 58 rows on 50.12413,14.12853 are a real development cluster."""
    body = _idnes_body(geometry=f'{{"type":"Point","coordinates":{point}}}')
    reads = read("json_point", scoped("idnes", body),
                 _geojson_entry(locator={"reject_points": ["49.19186,16.61109",
                                                           "49.19752,16.65812",
                                                           "49.81150,15.61824"]}),
                 native=IDNES_NATIVE)
    assert bool(reads) is admitted


def test_json_point_refuses_a_malformed_reject_point_rather_than_skipping_it():
    """A junk pin readmitted by a typo is exactly what the list exists to stop."""
    with pytest.raises(IntakeRefused) as excinfo:
        read("json_point", scoped("idnes", _idnes_body()),
             _geojson_entry(locator={"reject_points": ["nonsense"]}), native=IDNES_NATIVE)
    assert "reject_points" in str(excinfo.value)


def test_json_point_genuinely_evaluates_the_cz_envelope():
    """16,833 active idnes rows sit outside the CZ bbox with obec/okres/region all NULL, so
    the guard is not decoration — and `consults_guards=True` is only honest because the
    reader body calls `guard_admits` itself."""
    body = _idnes_body(geometry='{"type":"Point","coordinates":[-3.70379, 40.41678]}')
    assert read("json_point", scoped("idnes", body), _geojson_entry(),
                native=IDNES_NATIVE) == []


def test_json_point_refuses_an_entry_that_names_no_coordinate_shape():
    with pytest.raises(IntakeRefused) as excinfo:
        read("json_point", scoped("idnes", _idnes_body()),
             _geojson_entry(locator={"feature": None}), native=IDNES_NATIVE)
    assert "lat_pointer" in str(excinfo.value)


def test_json_point_refuses_a_coordinate_entry_with_no_position_branch():
    """Which branch of the portal's map produced a position IS its licence class (C6), and it
    is never inferred from what the reader stamped."""
    with pytest.raises(IntakeRefused):
        read("json_point", scoped("idnes", _idnes_body()),
             _geojson_entry(locator={"position_branch": None}), native=IDNES_NATIVE)


# ------------------------------------------------------------------ json_geometry

def _maxima_body(config: str) -> str:
    return ("<html><body><div class=\"locality\">Brno, Brno-střed, Veveří</div>"
            f"<script>var mapConfig = JSON.parse('{config}');</script></body></html>")


def _geometry_entry(claim_type: str, **overrides: Any) -> Entry:
    locator = {"reader": "json_geometry", "css": "script",
               "script_match": MAXIMA_SCRIPT_MATCH, "decode": "js_string",
               "then": "/features/0", "geometry_reader": "openlayers"}
    if claim_type == "coordinate":
        locator["position_branch"] = "portal_pin"
        locator["reject_zoom_at_or_below"] = 12
    locator.update(overrides.pop("locator", {}))
    return entry("maxima", locator, claim_type=claim_type,
                 extraction_method="map_widget_parse", surface="map_config",
                 guards=("reject_outside_cz_bbox",), **overrides)


def test_json_geometry_types_a_point_feature_as_a_pin_and_no_shape():
    document = pinned("maxima")
    reads = read("json_geometry", document, _geometry_entry("coordinate"))
    claim = one(reads)
    assert claim.value_geom_wkt == "POINT(16.60411 49.20256)"
    assert claim.declared_precision_label == "point" and claim.blur_evidence == "none"
    assert reads[0].position_branch == "portal_pin"
    assert span_text(document, claim) == claim.evidence_quote
    # A Point declares no uncertainty shape: migration 383's class default is the honest
    # bound there, and inventing a radius would be worse than having none.
    assert read("json_geometry", document,
                _geometry_entry("uncertainty_geometry")) == []


def test_json_geometry_gives_a_linestring_its_midpoint_and_half_its_length():
    """The recon's real a10070727 line (Jeseniova, Praha 3). Half the polyline length is the
    radius the shape declares; the position is the linear-referenced midpoint, never an
    endpoint."""
    line = ('{"type":"LineString","coordinates":'
            '[[14.4817548,50.0907159],[14.4723199,50.0889962]]}')
    body = _maxima_body('{"center":[14.47,50.09],"zoom":15.679,"features":[' + line + "]}")
    document = scoped("maxima", body)
    pin = one(read("json_geometry", document, _geometry_entry("coordinate")))
    assert "POINT(14.477" in pin.value_geom_wkt
    assert pin.declared_precision_label == "linestring"
    shape = one(read("json_geometry", document, _geometry_entry("uncertainty_geometry")))
    assert shape.value_text == "LineString"
    assert shape.value_shape_wkt.startswith("LINESTRING(")
    assert shape.declared_radius_m == pytest.approx(350.0, abs=10.0)
    assert shape.value_jsonb["radius_basis"] == "half_segment_length"


@pytest.mark.parametrize("radius,metres", [(0.01225, 1359.75), (0.02032, 2255.52)])
def test_json_geometry_converts_a_circle_radius_and_declares_the_blur(radius, metres):
    """maxima ships the radius in DEGREES and the contract names the conversion
    (`radius_deg_times_111000`), reproducing the recon's own readout: 0.01225° -> "1.36 km",
    0.02032° -> "2.26 km". A Circle is the one sanctioned case where blur rides on the
    coordinate itself — the portal is drawing its own imprecision."""
    circle = ('{"type":"Circle","coordinates":[14.404440,50.009601],'
              f'"radius":{radius}}}')
    body = _maxima_body('{"center":[14.40,50.00],"zoom":13.27,"features":[' + circle + "]}")
    document = scoped("maxima", body)
    pin = one(read("json_geometry", document, _geometry_entry("coordinate")))
    assert pin.blur_evidence == "declared" and pin.declared_precision_label == "circle"
    shape = one(read("json_geometry", document, _geometry_entry("uncertainty_geometry")))
    assert shape.declared_radius_m == pytest.approx(metres, abs=0.1)
    assert shape.value_shape_wkt.startswith("POINT(")
    assert shape.value_jsonb["radius_basis"] == "radius_deg_times_111000"
    assert shape.blur_evidence == "declared"


def test_an_empty_features_array_yields_no_claim_and_the_view_centre_is_never_read():
    """`d40031686` serves a coarse regional centre with `features: []`, ~9.2 km from its
    stored pin and in a different okres. "No feature, no coordinate" is STRUCTURAL here — the
    entry's own `then` pointer misses — which is why the never-implemented
    `reject_empty_geometry` guard is dropped rather than written."""
    body = _maxima_body('{"center":[14.972620,49.989445],"zoom":10.20,"features":[]}')
    document = scoped("maxima", body)
    for claim_type in ("coordinate", "uncertainty_geometry"):
        assert read("json_geometry", document, _geometry_entry(claim_type)) == []


def test_json_geometry_refuses_a_feature_drawn_at_a_regional_zoom():
    """The second rail, and a different failure from the first: a page that DOES carry a
    feature but is drawn at zoom 10.2. Contract data, so the threshold is reviewable."""
    point = '{"type":"Point","coordinates":[14.972620,49.989445]}'
    body = _maxima_body('{"center":[14.97,49.98],"zoom":10.20,"features":[' + point + "]}")
    document = scoped("maxima", body)
    assert read("json_geometry", document, _geometry_entry("coordinate")) == []
    # Without the rail the same page yields a pin — so the test is about the rail, not about
    # the page happening to be unreadable.
    assert read("json_geometry", document,
                _geometry_entry("coordinate",
                                locator={"reject_zoom_at_or_below": 5})) != []


def test_json_geometry_decodes_a_js_string_literal_before_parsing():
    """maxima serves the config as a JS single-quoted literal with backslash-escaped quotes,
    which is the whole reason `decode: js_string` exists. Decoded IN FULL and only then
    handed to `json.loads`, because that is what the browser does."""
    escaped = ('{\\"center\\":[16.60,49.20],\\"zoom\\":15,\\"features\\":'
               '[{\\"type\\":\\"Point\\",\\"coordinates\\":[16.60411,49.20256]}]}')
    document = scoped("maxima", _maxima_body(escaped))
    claim = one(read("json_geometry", document, _geometry_entry("coordinate")))
    assert claim.value_geom_wkt == "POINT(16.60411 49.20256)"
    assert span_text(document, claim) == claim.evidence_quote


def test_json_scalar_reads_the_zoom_out_of_the_same_script_config():
    """Recorded so a REFUSAL is legible: without it, "the coordinate was rejected at zoom
    10.2" and "the config key moved" produce the same empty result."""
    claim = one(read("json_scalar", pinned("maxima"),
                     entry("maxima",
                           {"reader": "json_scalar", "css": "script",
                            "script_match": MAXIMA_SCRIPT_MATCH, "decode": "js_string",
                            "json_pointer": "/zoom", "value_kind": "num"},
                           claim_type="map_zoom", extraction_method="map_widget_parse",
                           surface="map_config")))
    assert claim.value_text == "15" and claim.value_num == 15.0


# ------------------------------------------------------------------ json_breadcrumb

def _breadcrumb_entry(level: str, claim_type: str, **overrides: Any) -> Entry:
    locator = {"reader": "json_breadcrumb", "css": "script[type='application/ld+json']",
               "type": "BreadcrumbList", "level": level, "anchor_slugs": KRAJ_SLUGS}
    locator.update(overrides.pop("locator", {}))
    return entry("realitymix", locator, claim_type=claim_type,
                 extraction_method="breadcrumb_parse", surface="jsonld", **overrides)


@pytest.mark.parametrize(
    "level,claim_type,expected",
    [("kraj", "kraj_name", "Karlovarský kraj"), ("okres", "okres_name", "Karlovy Vary"),
     ("obec", "obec_name", "Potůčky"), ("quarter", "quarter_name", "Stráň")])
def test_json_breadcrumb_anchors_the_chain_on_the_kraj_slug(level, claim_type, expected):
    """On the pinned archived body (a two-level `domy/pronajem` category path) the geo chain
    starts at position 4, while the recon's three-level `byty/2+1/pronajem` sample puts it at
    5 — so an entry declaring absolute positions is wrong on one of them. The kraj slug is
    the anchor and the level is counted forward from it."""
    document = archived("realitymix")
    claim = one(read("json_breadcrumb", document, _breadcrumb_entry(level, claim_type)))
    assert claim.value_text == expected
    assert span_text(document, claim) == expected


def test_json_breadcrumb_reads_the_flat_schema_org_shape_too():
    """Both shapes are legal schema.org: the live realitymix page nests name/@id under an
    `item` object, and the flat form spells them on the element itself. The reader reads the
    element when there is no nested `item`, so one entry serves both."""
    body = ('<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":['
            '{"@type":"ListItem","position":5,'
            '"@id":"https://realitymix.cz/reality/byty/pronajem/plzensky",'
            '"name":"Plzeňský kraj"},'
            '{"@type":"ListItem","position":6,'
            '"@id":"https://realitymix.cz/reality/byty/pronajem/plzensky/plzen-mesto",'
            '"name":"okres Plzeň-město"}]}'
            "</script></head><body></body></html>")
    claim = one(read("json_breadcrumb", scoped("realitymix", body),
                     _breadcrumb_entry("kraj", "kraj_name")))
    assert claim.value_text == "Plzeňský kraj"


def test_json_breadcrumb_needs_the_slug_the_chain_is_anchored_on():
    """The pinned realitymix fixture states the four levels as NAMES with no `@id` at all, so
    there is no slug to anchor on and the reader stays silent. That is the fail-closed rule
    doing its job — and the reason the activation PR has to score the real captured body."""
    assert read("json_breadcrumb", pinned("realitymix"),
                _breadcrumb_entry("kraj", "kraj_name")) == []


def test_json_breadcrumb_fails_closed_when_no_declared_slug_anchors_the_chain():
    """An unverified kraj slug then costs COVERAGE, never correctness — and a kraj with zero
    breadcrumb claims and non-zero listings is how a wrong slug is found."""
    assert read("json_breadcrumb", archived("realitymix"),
                _breadcrumb_entry("kraj", "kraj_name",
                                  locator={"anchor_slugs": ["neverland"]})) == []


def test_json_breadcrumb_stops_short_rather_than_claiming_a_level_the_chain_lacks():
    body = ('<html><head><script type="application/ld+json">'
            '{"@type":"BreadcrumbList","itemListElement":['
            '{"@type":"ListItem","position":1,"item":{"@id":"https://x/reality/domy",'
            '"name":"Domy"}},'
            '{"@type":"ListItem","position":2,"item":'
            '{"@id":"https://x/reality/domy/karlovarsky","name":"Karlovarský kraj"}}]}'
            "</script></head><body></body></html>")
    document = scoped("realitymix", body)
    assert one(read("json_breadcrumb", document,
                    _breadcrumb_entry("kraj", "kraj_name"))).value_text == "Karlovarský kraj"
    assert read("json_breadcrumb", document, _breadcrumb_entry("obec", "obec_name")) == []


def test_json_breadcrumb_refuses_a_level_or_an_anchor_set_it_cannot_execute():
    document = archived("realitymix")
    for locator in ({"level": "mestsky_obvod"}, {"anchor_slugs": []}, {"type": None}):
        with pytest.raises(IntakeRefused):
            read("json_breadcrumb", document,
                 _breadcrumb_entry("kraj", "kraj_name", locator=locator))


# ------------------------------------------------- subject scope: id_match / on_miss

def test_a_subject_miss_raises_rather_than_returning_a_silent_zero():
    """`on_miss: fail` means NO CLAIM — but not a silent one. Without the raise, "the portal
    changed its id scheme fleet-wide" and "this page genuinely carries no address" are the
    same green zero-claim sweep, and the batch still stamps 'ok' and moves the watermark."""
    with pytest.raises(SubjectNotFound) as excinfo:
        read("json_point", archived("mmreality"), _point_pair_entry(), native="999999")
    assert "on_miss=fail" in str(excinfo.value)


def test_two_objects_carrying_the_subjects_id_are_not_evidence_either():
    body = _idnes_body(extra=(',{"type":"Feature",'
                              '"geometry":{"type":"Point","coordinates":[15.4,50.8]},'
                              f'"properties":{{"id":"{IDNES_NATIVE}","isSimilar":false}}}}'))
    with pytest.raises(SubjectNotFound):
        read("json_point", scoped("idnes", body), _geojson_entry(), native=IDNES_NATIVE)


def test_exclude_where_honours_an_exclusion_zone_no_pointer_can_pop():
    """idnes' zone is `then: /geojson/features[isSimilar=true]` — a PREDICATE, which
    `html_scope` cannot execute and therefore defers to the reader. A feature carrying the
    subject's id AND `isSimilar: true` must yield nothing."""
    body = _idnes_body(similar="true")
    with pytest.raises(SubjectNotFound):
        read("json_point", scoped("idnes", body), _geojson_entry(), native=IDNES_NATIVE)


def test_an_on_miss_other_than_fail_is_refused_by_name():
    """The largest-blob fallback must not be reachable by re-declaring it: `mmreality_parser`
    picks the biggest `:property` when no id matches, and on the pinned body the biggest is
    the neighbour's."""
    with pytest.raises(IntakeRefused) as excinfo:
        read("json_point", archived("mmreality"),
             _point_pair_entry(subject_scope={"kind": "id_match",
                                              "on_miss": "largest_blob"}),
             native=MMREALITY_NATIVE)
    assert "on_miss" in str(excinfo.value)


def test_a_match_against_a_row_field_the_extractor_does_not_know_is_refused():
    with pytest.raises(IntakeRefused) as excinfo:
        read("json_point", archived("mmreality"),
             _point_pair_entry(locator={"match": {"json_pointer": "/id",
                                                  "equals_row_field": "listing_id"}}),
             native=MMREALITY_NATIVE)
    assert "equals_row_field" in str(excinfo.value)


def test_an_unparseable_blob_is_a_subject_miss_and_never_an_exception_out_of_the_lane():
    """The repo's only captured idnes page has had its map JSON destroyed by the fixture
    anonymiser (`_PHONE_RE` rewrote every 9-digit run inside the coordinate arrays), so it is
    a free, honest regression for "a body whose blob will not parse"."""
    import json

    from selectolax.lexbor import LexborHTMLParser
    body = (_ARCHIVED / "idnes_detail.html").read_bytes()
    node = LexborHTMLParser(body.decode("utf-8")).css_first("script[data-maptiler-json]")
    assert node is not None
    with pytest.raises(ValueError):
        json.loads(node.text())
    with pytest.raises(SubjectNotFound):
        read("json_point", scoped("idnes", body), _geojson_entry(), native=IDNES_NATIVE)


def test_extract_payload_turns_a_subject_miss_into_one_absence_and_no_claims():
    """The lane's half of `on_miss: fail`: a per-row portal fact (a re-id, a redirect, an
    interstitial saved under the wrong key) must never roll back a batch of thousands, and it
    must never be indistinguishable from "we looked and it was not there" (03 §3.2 rule 4)."""
    body = (_ARCHIVED / "mmreality_detail.html").read_bytes()
    item = _point_pair_entry()
    register = ScopeRegister.from_zones("mmreality", CONTRACTS["mmreality"].exclusion_zones)
    result = extract_payload(
        payload("mmreality", "999999", body), listing_row("mmreality", "999999"), [item],
        register=register)
    assert result.claims == []
    assert [(a.field_, a.reason) for a in result.absences] == [
        ("coordinate", "not_attempted")]
    assert "on_miss=fail" in (result.absences[0].detail or "")


def test_extract_payload_still_produces_the_claim_for_the_matching_subject():
    """The non-vacuity half: the same call over the same body under the SUBJECT's id has to
    produce the claim, or the test above would pass on a lane that reads nothing."""
    body = (_ARCHIVED / "mmreality_detail.html").read_bytes()
    register = ScopeRegister.from_zones("mmreality", CONTRACTS["mmreality"].exclusion_zones)
    result = extract_payload(
        payload("mmreality", MMREALITY_NATIVE, body),
        listing_row("mmreality", MMREALITY_NATIVE),
        [_blob_entry("/municipality", claim_type="obec_name")],
        register=register)
    assert [c.value_text for c in result.claims] == ["Andělská Hora"]
    assert result.absences == []
    assert result.claims[0].surface == "archived_html"
    assert result.claims[0].payload_scope_version.startswith("html_scope@1:mmreality:")


# ------------------------------------------------------------------ transforms

@pytest.mark.parametrize(
    "name,value,expected",
    [
        # The four shapes realitymix's `data-address` takes, measured on the archived body
        # and on the recon's samples.
        ("address_part_okres", "Křimická, Plzeň 3, Plzeň, okres Plzeň-město", "Plzeň-město"),
        ("address_part_obec", "Křimická, Plzeň 3, Plzeň, okres Plzeň-město", "Plzeň"),
        ("address_part_street", "Křimická, Plzeň 3, Plzeň, okres Plzeň-město", "Křimická"),
        ("address_part_house_number",
         "Křimická 655/31, Plzeň 3, Plzeň, okres Plzeň-město", "655/31"),
        ("address_part_okres", "Stráň, Potůčky, okres Karlovy Vary", "Karlovy Vary"),
        ("address_part_obec", "Stráň, Potůčky, okres Karlovy Vary", "Potůčky"),
        # `Stráň` is a část obce the same page also states as `data-form-address` and as the
        # breadcrumb tail — typing it a street would fabricate.
        ("address_part_street", "Stráň, Potůčky, okres Karlovy Vary", None),
        ("address_part_house_number", "Stráň, Potůčky, okres Karlovy Vary", None),
        # A single-segment line has an obec and nothing else.
        ("address_part_obec", "Zlín", "Zlín"),
        ("address_part_okres", "Zlín", None),
        ("address_part_street", "Zlín", None),
        # An abbreviated qualifier is still an okres; `strip_prefix:"okres "` would publish
        # "okr. Karlovy Vary" as an okres name.
        ("address_part_okres", "Potůčky, okr. Karlovy Vary", "Karlovy Vary"),
        # A parenthetical qualifier is a different vocabulary and is left alone.
        ("split_paren_okres", "Ostrov (okres Karlovy Vary)", "Ostrov"),
        ("split_paren_okres", "Praha (okres Praha)", "Praha"),
        ("split_paren_okres", "Ostrov (část obce X)", "Ostrov (část obce X)"),
    ])
def test_the_address_part_transforms_state_what_they_select(name, value, expected):
    assert apply_transforms(value, (name,)) == expected


def test_split_paren_okres_can_take_the_qualifier_half_instead():
    assert apply_transforms("Ostrov (okres Karlovy Vary)",
                            ("split_paren_okres:okres",)) == "Karlovy Vary"
    assert apply_transforms("Ostrov", ("split_paren_okres:okres",)) is None


@pytest.mark.parametrize(
    "arg,value,expected",
    [
        ("1@2+", "Praha 3, Žižkov, Jeseniova", "Praha 3"),
        ("2@3", "Praha 3, Žižkov, Jeseniova", "Žižkov"),
        ("-1@2+", "Praha 3, Žižkov, Jeseniova", "Jeseniova"),
        # A one-segment line's only token is the OBEC. Taking segment 1 unconditionally
        # would type an obec as a městský obvod on every village row — the arity is the
        # refusal that stops it, and a miss is not a wrong admin level.
        ("1@2+", "Kostelec nad Černými Lesy", None),
        ("2@3", "Liberec, Ruprechtice", None),
        ("-1@2+", "Kostelec nad Černými Lesy", None),
        ("1@*", "Kostelec nad Černými Lesy", "Kostelec nad Černými Lesy"),
        ("4@3", "Praha 3, Žižkov, Jeseniova", None),
        ("nonsense", "Praha 3, Žižkov, Jeseniova", None),
    ])
def test_comma_segment_refuses_rather_than_guessing_an_admin_level(arg, value, expected):
    assert apply_transforms(value, (f"comma_segment:{arg}",)) == expected


def test_a_transformed_value_quotes_the_literal_it_was_read_from():
    """`_evidenced` defaults the quote to the value, and with a transform that lets
    `find_span` anchor on some other occurrence of the shorter string inside the same node —
    measured: a `data-city` transformed to "České Budějovice" resolved its span into the
    node's `value="Nádražní 1067, České Budějovice"` attribute instead."""
    document = scoped("ceskereality", (_REFETCH / "ceskereality_b1.html").read_bytes())
    item = entry("ceskereality",
                 {"reader": "html_attr", "css": "input#driving_calculator_from",
                  "attr": "data-city"},
                 claim_type="obec_name", transform=("split_paren_okres",))
    claim = one(read("html_attr", document, item))
    assert claim.value_text == "Ostrov"
    assert claim.evidence_quote == "Ostrov (okres Karlovy Vary)"
    assert span_text(document, claim) == claim.evidence_quote


def test_an_untransformed_read_still_quotes_its_own_value():
    """The behaviour-preserving half: no DOM entry in any shipped contract declares a
    transform, so for every one of them the quote is exactly what it was before."""
    document = scoped("ceskereality", (_REFETCH / "ceskereality_b1.html").read_bytes())
    claim = one(read("html_attr", document,
                     entry("ceskereality",
                           {"reader": "html_attr", "css": "input#driving_calculator_from",
                            "attr": "data-city"}, claim_type="obec_name")))
    assert claim.value_text == claim.evidence_quote == "Ostrov (okres Karlovy Vary)"


def test_every_new_transform_is_registered_under_the_name_the_contract_gate_enumerates():
    """`contracts.IMPLEMENTED_TRANSFORMS` is pure data and the runtime registry is the truth;
    a name in one and not the other is either a refused entry or a silent no-op."""
    for name in ("address_part_street", "address_part_obec", "address_part_okres",
                 "address_part_house_number", "split_paren_okres", "comma_segment"):
        assert name in TRANSFORMS
        assert name in contracts.IMPLEMENTED_TRANSFORMS


# ------------------------------------------------------------------ registration

def test_every_archive_reader_may_be_stamped_with_the_surface_the_lane_stamps():
    """C9: the entry keeps its published `locator_kind`, the lane STAMPS `archived_html`. A
    reader whose `ReaderContract` does not admit `archived_html` could never be executed by
    this lane at all — the projection would refuse every entry naming it."""
    for name in ARCHIVE_READERS:
        assert "archived_html" in contracts.READER_CONTRACTS[name].substrates, name


def test_no_archive_reader_claims_a_method_the_lane_cannot_evidence():
    """`llm_text` needs a model and a prompt version this lane has no way to supply, and
    `assert_evidence_complete` refuses such a claim before the write — so no DOM reader may
    declare it."""
    for name in ARCHIVE_READERS:
        assert "llm_text" not in contracts.READER_CONTRACTS[name].methods, name


# One representative entry per canonical reader, in the shape a portal activation will write
# it. These are what the seven W2-6..W2-12 PRs paste, so the locator keys the canon fixed and
# the keys `_check_executable` demands have to agree HERE — a mismatch discovered in an
# activation PR is discovered inside a nine-portal, one-transaction projection.
CANONICAL_ENTRIES: dict[str, dict[str, Any]] = {
    "html_own_text": {
        "source": "remax", "id": "rx.det.header_address", "locator_kind": "html_selector",
        "extraction_method": "html_selector_parse", "claim_type": "address_line_verbatim",
        "locator": {"reader": "html_own_text", "css": "h2.pd-header__address"},
    },
    "html_regex": {
        "source": "ceskereality", "id": "cr.det.title_line", "locator_kind": "html_selector",
        "extraction_method": "regex_text", "claim_type": "street_name",
        "locator": {"reader": "html_regex", "css": "title",
                    "pattern": ", ulice (?P<street>[^,]+),", "group": "street"},
    },
    "html_attr_regex": {
        "source": "bazos", "id": "bzs.det.obec_slug", "locator_kind": "url_slug",
        "extraction_method": "url_slug_parse", "claim_type": "obec_name",
        "locator": {"reader": "html_attr_regex", "css": "a[href*='/inzeraty/']",
                    "attr": "href", "decode": "percent",
                    "pattern": "/inzeraty/(?P<obec_slug>[^/]+)/(?P<psc>\\d{5})/",
                    "group": "obec_slug"},
    },
    "html_marker": {
        "source": "bazos", "id": "bzs.det.blur_hint", "locator_kind": "html_selector",
        "extraction_method": "portal_declared_quality", "claim_type": "blur_hint",
        "blur_evidence": "declared",
        "precision_cap": {"granularity_max": {"_default": "obec"},
                          "blurred_labels": ["approximate_location"]},
        "locator": {"reader": "html_marker", "css": "a[href*='/place/']", "attr": "title",
                    "value_label": "approximate_location"},
    },
    "json_scalar": {
        "source": "mmreality", "id": "mm.det.blob_municipality",
        "locator_kind": "embedded_json", "extraction_method": "portal_structured_field",
        "claim_type": "obec_name",
        "subject_scope": {"kind": "id_match", "on_miss": "fail"},
        "locator": {"reader": "json_scalar", "css": "[\\:property]", "attr": ":property",
                    "json_pointer": "/municipality", "match": BLOB_MATCH},
    },
    "json_regex": {
        "source": "mmreality", "id": "mm.det.original_title_street",
        "locator_kind": "embedded_json", "extraction_method": "regex_text",
        "claim_type": "street_name",
        "subject_scope": {"kind": "id_match", "on_miss": "fail"},
        "locator": {"reader": "json_regex", "css": "[\\:property]", "attr": ":property",
                    "json_pointer": "/originalTitle", "group": "street",
                    "pattern": ",\\s*ul\\.\\s*(?P<street>[^,]+)$", "match": BLOB_MATCH},
    },
    "json_bool": {
        "source": "mmreality", "id": "mm.det.blob_accurate",
        "locator_kind": "embedded_json", "extraction_method": "portal_declared_quality",
        "claim_type": "precision_declaration",
        "precision_cap": {"granularity_max": {"_default": "obec"},
                          "blurred_labels": ["not_accurate"]},
        "subject_scope": {"kind": "id_match", "on_miss": "fail"},
        "locator": {"reader": "json_bool", "css": "[\\:property]", "attr": ":property",
                    "json_pointer": "/accurate",
                    "labels": {"true": "accurate", "false": "not_accurate"},
                    "match": BLOB_MATCH},
    },
    "json_point": {
        "source": "idnes", "id": "id.det.subject_feature",
        "locator_kind": "embedded_json", "extraction_method": "map_widget_parse",
        "claim_type": "coordinate", "guards": ["reject_outside_cz_bbox"],
        "precision_cap": {"granularity_max": {"_default": "address_point"}},
        "subject_scope": {"kind": "id_match", "on_miss": "fail"},
        "locator": {"reader": "json_point", "css": "script[data-maptiler-json]",
                    "then": "/geojson/features", "match": ID_MATCH,
                    "exclude_where": NOT_SIMILAR, "feature": "/geometry",
                    "position_branch": "portal_pin",
                    "reject_points": ["49.19186,16.61109"]},
    },
    "json_geometry": {
        "source": "maxima", "id": "mx.det.map_features", "locator_kind": "map_config",
        "extraction_method": "map_widget_parse", "claim_type": "coordinate",
        "guards": ["reject_outside_cz_bbox"],
        "precision_cap": {"granularity_max": {"_default": "address_point"}},
        "locator": {"reader": "json_geometry", "css": "script",
                    "script_match": MAXIMA_SCRIPT_MATCH, "decode": "js_string",
                    "then": "/features/0", "geometry_reader": "openlayers",
                    "position_branch": "portal_pin", "reject_zoom_at_or_below": 12},
    },
    "json_breadcrumb": {
        "source": "realitymix", "id": "rm.det.breadcrumb_geo", "locator_kind": "jsonld",
        "extraction_method": "breadcrumb_parse", "claim_type": "okres_name",
        "locator": {"reader": "json_breadcrumb", "level": "okres",
                    "css": "script[type='application/ld+json']", "type": "BreadcrumbList",
                    "anchor_slugs": KRAJ_SLUGS},
    },
}


@pytest.mark.parametrize("reader", sorted(CANONICAL_ENTRIES))
def test_a_representative_entry_for_every_canonical_reader_projects(reader):
    """The projection gate and the reader have to agree about the SAME locator, and the
    consequence of them not agreeing is not a red test in the activation PR — it is the
    hourly claim-intake lane wedging fleet-wide an hour after merge, because `project()` is
    one transaction for all nine portals."""
    raw = dict(CANONICAL_ENTRIES[reader])
    parsed = contracts.parse_entry(dict(raw, page_kind="detail"),
                                   source=raw.pop("source"), index=0)
    assert parsed.reader == reader


def test_the_projection_still_refuses_a_canonical_reader_on_the_wrong_axis():
    """Every clause `_check_executable` enforces is one thing the reader does with the entry;
    the point of the new rows is that declaring past them stays impossible."""
    raw = dict(CANONICAL_ENTRIES["json_point"], page_kind="detail")
    source = raw.pop("source")
    # A surface the reader does not read.
    with pytest.raises(contracts.ContractError):
        contracts.parse_entry(dict(raw, locator_kind="legacy_column"), source=source,
                              index=0)
    # A method whose provenance it does not perform.
    with pytest.raises(contracts.ContractError):
        contracts.parse_entry(dict(raw, extraction_method="legacy_column"), source=source,
                              index=0)
    # A required locator key it indexes.
    with pytest.raises(contracts.ContractError):
        contracts.parse_entry(
            dict(raw, locator={k: v for k, v in raw["locator"].items()
                               if k != "position_branch"}),
            source=source, index=0)
    # A transform on a reader that applies none: `json_point` states a position, and a
    # normaliser over it would silently not run.
    with pytest.raises(contracts.ContractError):
        contracts.parse_entry(dict(raw, transform=["strip_prefix:x"]), source=source,
                              index=0)
    # A guard on a reader that evaluates none.
    breadcrumb = dict(CANONICAL_ENTRIES["json_breadcrumb"], page_kind="detail")
    breadcrumb.pop("source")
    with pytest.raises(contracts.ContractError):
        contracts.parse_entry(dict(breadcrumb, guards=["reject_outside_cz_bbox"]),
                              source="realitymix", index=0)


def test_the_absence_a_subject_miss_writes_is_the_shape_the_writer_expects():
    """`Absence.to_row` is what lands in `location_claim_absences`, whose reason vocabulary is
    CHECK-constrained to four values; `not_attempted` is the one every declined-completion
    case in this lane already uses."""
    absence = Absence(
        listing_id=1, surface=archive.ARCHIVE_SURFACE, field_="coordinate",
        reason="not_attempted", extraction_method="map_widget_parse", detail="x")
    row = absence.to_row(archive.REMINE_VERSION)
    assert row["surface"] == "archived_html" and row["reason"] == "not_attempted"
    assert row["extractor_version"] == "claims_remine_archive@1"
