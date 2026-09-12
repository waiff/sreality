"""idnes@3 — the contract as it ships, run over every committed idnes body.

`test_page_reader_canon` proves the READERS. This file proves the CONTRACT: every assertion
loads `contracts/portals/idnes.yaml` through `fx.entries_for` and runs the shipped locator —
the real selector, the real pointer, the real `reject_points` list — against a real body. A
reader test that builds its own entry cannot catch a contract naming the wrong pointer,
declaring the wrong branch or forgetting an exclusion, which are the mistakes a portal
contract actually makes.

It absorbs `test_contract_idnes_activation.py` (deleted with idnes@2's entry set, W1-c R14):
the junk-pin and CZ-envelope rails, the id-matched subject selection, the licence ladder and
the Mapy veto, the anonymiser regression on the repo's real capture, and the evidence-span
promise all live on here against the entries that replaced it.

Five committed bodies, and the differences between them are the point:
  * `location_w2/idnes_detail.html` — the pinned page the golden scores; modelled on this
    contract and the only body carrying a subject map feature AND the blur disclaimer.
  * `portal_html/idnes_detail.html` — a REAL archived page whose map JSON the fixture
    anonymiser's phone sweep destroyed; the honest regression for "an unparseable blob
    yields no claim and no exception".
  * `location_w2a_refetch/idnes_a1.html` + `a2.html` — a real BLURRED listing: the portal
    maps only exact-address listings, so its own feature is absent from all 21 features.
  * `location_w2a_refetch/idnes_b1.html` — a real Prague listing: the town is "Praha" while
    the address line says "Praha 5 - Hlubočepy" and there is no okres at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from selectolax.lexbor import LexborHTMLParser

from location_data import contracts
from location_data.claims_intake import Entry, ListingRow
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from location_data.page_readers import (
    PAGE_READERS,
    ArchivedPayload,
    SubjectNotFound,
    _licensed_coordinate,
    extract_page,
    stamp_page_claim,
)
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
_PINNED = _ROOT / "tests" / "fixtures" / "location_w2" / "idnes_detail.html"
_ARCHIVED = _ROOT / "tests" / "fixtures" / "portal_html" / "idnes_detail.html"
_REFETCH = _ROOT / "tests" / "fixtures" / "location_w2a_refetch"

CONTRACT = {c.source: c for c in contracts.load_all()}["idnes"]
CLOCK = datetime(2026, 1, 1, tzinfo=UTC)

VERSION = 3
NATIVE = "6a71888887e5da33ca081ad8"
NEIGHBOUR = "68badb8de7b021a4470fb87d"
DISCLAIMER = "Nemovitost nemá přesnou adresu, nachází se ve vyznačené oblasti."

# In file order, which is the order `contract_entry_id` is assigned in (02 §2.1.8 forbids
# reordering), and one per claim type.
ENTRY_IDS = [
    "id.det.subject_feature", "id.det.no_exact_disclaimer", "id.det.country",
    "id.det.kraj", "id.det.okres", "id.det.obec", "id.det.cast_obce",
    "id.det.street", "id.det.cp", "id.det.co",
]
CLAIM_TYPES = {
    "id.det.subject_feature": "coordinate",
    "id.det.no_exact_disclaimer": "precision_declaration",
    "id.det.country": "country",
    "id.det.kraj": "kraj_name",
    "id.det.okres": "okres_name",
    "id.det.obec": "obec_name",
    "id.det.cast_obce": "cast_obce_name",
    "id.det.street": "street_name",
    "id.det.cp": "house_number_cp",
    "id.det.co": "house_number_co",
}

# (body, the listing it is keyed by, the town it must yield). The town line: a body that
# stops yielding one is a `location_town_coverage` regression, not a fixture nit.
TOWN_BODIES = [
    (_PINNED, NATIVE, "Tanvald"),
    (_ARCHIVED, NATIVE, "Tanvald"),
    (_REFETCH / "idnes_a1.html", "6a304242115442e33b0dbd24", "Králův Dvůr"),
    (_REFETCH / "idnes_a2.html", "6a304242115442e33b0dbd24", "Králův Dvůr"),
    (_REFETCH / "idnes_b1.html", "6a5a34e1f582fb9b160859ec", "Praha"),
]


# ------------------------------------------------------------------ harness

def entries() -> dict[str, Entry]:
    return {e.entry_id: e for e in fx.entries_for("idnes")}


def entry(entry_id: str) -> Entry:
    return entries()[entry_id]


def row(native: str = NATIVE) -> ListingRow:
    return fx.listing("idnes", {}, native=native)


def payload(body: bytes, native: str = NATIVE) -> ArchivedPayload:
    return ArchivedPayload(
        id=9001, source="idnes", source_id_native=native, page_kind="detail",
        payload_sha256="ab" * 32, first_observed_at=CLOCK, body=body)


def register() -> ScopeRegister:
    """The portal's OWN shipped exclusion register — the decoys a reader must not reach are
    the ones the contract declares, never ones a test invents."""
    return ScopeRegister.from_zones("idnes", CONTRACT.exclusion_zones)


def scoped(body: bytes | str) -> ScopedDocument:
    if isinstance(body, str):
        body = body.encode("utf-8")
    return scope_html(body, register=register())


def read(entry_id: str, document: ScopedDocument, *, native: str = NATIVE) -> list[Any]:
    item = entry(entry_id)
    return PAGE_READERS[str(item.reader)](item, row(native), payload(b"", native), document)


def one_claim(entry_id: str, document: ScopedDocument, *, native: str = NATIVE) -> Any:
    reads = read(entry_id, document, native=native)
    assert len(reads) == 1, f"{entry_id} produced {len(reads)} reads"
    return reads[0].claim


def feature(native: str, lon: float, lat: float, *, address: str = "Na Balkáně 1, Tanvald",
            similar: bool = False) -> dict[str, Any]:
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"id": native, "address": address, "isSimilar": similar}}


def page(features: str = "[]", *, info: str = DISCLAIMER, disclaimer: bool = True,
         address: str = "Na Balkáně, Tanvald - Šumburk nad Desnou, okres Jablonec nad Nisou",
         data_layer: dict[str, Any] | None = None, native: str = NATIVE) -> str:
    """A minimal idnes detail page in the SHAPE the contract's selectors address, with the
    dataLayer push written the way the real page writes it (a nested handler included)."""
    fields = {"event": "viewDetail", "listing_id": native,
              "listing_localityRegion": "Liberecký kraj", "listing_localityCity": "Tanvald",
              "listing_localityCityArea": "Šumburk nad Desnou", "listing_localityState": "CZ"}
    fields.update(data_layer or {})
    return (
        "<!DOCTYPE html><html lang='cs'><body>"
        + (f"<p class='b-detail__disclaimer'>{DISCLAIMER}</p>" if disclaimer else "")
        + f"<div class='b-detail__info'>{address}</div>"
        + '<script type="application/json" data-maptiler-json>'
        + json.dumps({"mtMapOptions": {"zoom": 14}, "infoText": info,
                      "geojson": {"type": "FeatureCollection",
                                  "features": json.loads(features)}}, ensure_ascii=False)
        + "</script><script type='text/javascript'>if (typeof dataLayer == 'object') {\n"
        + "dataLayer.push(" + json.dumps(fields, ensure_ascii=False) + ");\n"
        + '$(".b-detail__mortgage").on("click", function () {\n'
        + 'dataLayer.push({"event": "GAEvent", "eventCategory": "Hypoteka"});\n});\n}'
        + "</script></body></html>"
    )


# ------------------------------------------------------------------ the shape

def test_the_contract_is_v3_with_one_entry_per_type_and_a_town_entry() -> None:
    """The three rails W1-c's loader enforces, pinned here against THIS portal's file so a
    silent renumber, a reorder or a second carrier for one type is a test failure and not a
    review catch."""
    assert CONTRACT.version == VERSION
    assert [e.entry_id for e in CONTRACT.entries] == ENTRY_IDS
    assert {e.entry_id: e.claim_type for e in CONTRACT.entries} == CLAIM_TYPES

    types = [e.claim_type for e in CONTRACT.entries]
    assert len(types) == len(set(types))
    assert set(types) <= contracts.CLAIM_TYPES
    assert contracts.MANDATORY_CLAIM_TYPE in types


def test_the_town_entry_is_the_data_layer_city_and_names_a_reader_this_lane_runs() -> None:
    """The address line states 'Tanvald - Šumburk nad Desnou' glued into one token that binds
    to no RÚIAN obec, so the town is read from the dataLayer's own split field. The R4 fold
    is chained because `listing_localityCity` is the only field that could ever arrive as a
    městský obvod — the portal's `listing_localityDistrict` already does."""
    town = entry("id.det.obec")
    assert town.claim_type == "obec_name"
    assert str(town.reader) == "json_scalar"
    assert town.locator["json_pointer"] == "/listing_localityCity"
    assert town.transform == ("statutory_city_obec",)
    assert town.subject_scope == {"kind": "id_match", "on_miss": "fail"}
    assert str(town.reader) in PAGE_READERS


@pytest.mark.parametrize("entry_id", ENTRY_IDS)
def test_every_entry_names_a_reader_the_page_lane_implements(entry_id: str) -> None:
    """Not a tautology: an entry may name a reader the CONTRACT gate knows
    (`READER_CONTRACTS`) that the page lane does not register, and the lane would then skip
    it while nothing else ran it — a coverage hole with no error anywhere."""
    assert str(entry(entry_id).reader) in PAGE_READERS
    assert entry(entry_id).page_kind == "detail"


def test_the_contract_states_no_psc_because_the_portal_publishes_none() -> None:
    """The one omitted claim type, measured rather than asserted: no committed idnes body
    carries a postcode-shaped token in its visible text after scoping — the only 5-digit runs
    in these files are map coordinates inside `<script>`, and the one real PSČ (the broker's
    own "110 00 Praha 1") sits in the `.broker` block the exclusion register strips. An entry
    that never fires is worse than no entry: it is counted as coverage."""
    assert "psc" not in {e.claim_type for e in CONTRACT.entries}
    postcode = re.compile(r"(?<![\d.])\d{3}[   ]?\d{2}(?![\d.])")
    for path, _, _ in TOWN_BODIES:
        tree = LexborHTMLParser(scoped(path.read_bytes()).html)
        for node in tree.css("script, style"):
            node.decompose()
        assert not postcode.findall(tree.body.text() if tree.body else ""), path.name


# ------------------------------------------------- the pinned body, entry by entry

def test_the_pinned_body_scopes_complete_and_keeps_the_map_script() -> None:
    """The second exclusion-zone shape: `script[data-maptiler-json]` is excluded only at
    `/geojson/features[isSimilar=true]`, a PREDICATE no RFC 6901 pointer can pop, so the
    script node itself must SURVIVE scoping and the exclusion is honoured by the reader."""
    document = scoped(_PINNED.read_bytes())
    assert document.is_complete
    assert document.css_first("script[data-maptiler-json]") is not None


@pytest.mark.parametrize("entry_id,value", [
    ("id.det.subject_feature", "50.74437214,15.31331632"),
    ("id.det.no_exact_disclaimer", "no_exact_address"),
    ("id.det.kraj", "Liberecký kraj"),
    ("id.det.okres", "Jablonec nad Nisou"),
    ("id.det.obec", "Tanvald"),
    ("id.det.cast_obce", "Šumburk nad Desnou"),
    ("id.det.street", "Na Balkáně"),
])
def test_each_entry_reads_its_value_off_the_pinned_body(entry_id: str, value: str) -> None:
    assert one_claim(entry_id, scoped(_PINNED.read_bytes())).value_text == value


@pytest.mark.parametrize("entry_id", ["id.det.country", "id.det.cp", "id.det.co"])
def test_the_three_conditional_entries_claim_nothing_on_a_czech_line_without_a_number(
    entry_id: str,
) -> None:
    """Absence is the right answer here and has to be asserted, or a transform that started
    firing on every row would look like new coverage: the pinned line ends in an okres
    qualifier (no country) and its street segment carries no house number."""
    assert read(entry_id, scoped(_PINNED.read_bytes())) == []


@pytest.mark.parametrize("entry_id", [
    "id.det.subject_feature", "id.det.no_exact_disclaimer", "id.det.kraj", "id.det.okres",
    "id.det.obec", "id.det.cast_obce", "id.det.street",
])
def test_every_evidence_quote_resolves_to_a_real_span(entry_id: str) -> None:
    """An `evidence_quote` is a promise the payload contains that text (01 §4.2 pairs it with
    `payload_sha256`). Migration 382's CHECK only tests that the quote is a substring, so a
    span pointing at the WRONG occurrence still passes — slicing it back is what makes the
    promise real."""
    document = scoped(_PINNED.read_bytes())
    claim = one_claim(entry_id, document)
    assert claim.span_start is not None and claim.span_end is not None, entry_id
    assert document.html[claim.span_start:claim.span_end] == claim.evidence_quote


def test_the_coordinate_quotes_the_array_and_not_the_thirteen_kilobyte_config() -> None:
    """"lat,lon" is assembled by the reader and appears nowhere in the body, and the node it
    came from is a whole map config. An evidence quote rides in the same jsonb array as the
    claim and is counted by `archived_claim_value_bytes`, so quoting the blob would put tens
    of KB on every idnes coordinate claim."""
    claim = one_claim("id.det.subject_feature", scoped(_PINNED.read_bytes()))
    assert claim.evidence_quote == "[15.31331632, 50.74437214]"
    assert claim.value_geom_wkt == "POINT(15.31331632 50.74437214)"


def test_the_disclaimer_claims_the_contracts_label_and_declares_blur() -> None:
    """The claim's VALUE is this contract's canonical label and its EVIDENCE is the portal's
    verbatim sentence — two different fields for exactly this case, so a reworded sentence
    stops matching instead of silently restating a different fact under the same label. W1-c
    R5: on a `precision_declaration` the stamped label IS the value, whatever the reader."""
    document = scoped(_PINNED.read_bytes())
    claim = one_claim("id.det.no_exact_disclaimer", document)
    assert claim.value_text == "no_exact_address"
    assert claim.blur_evidence == "declared"
    assert claim.evidence_quote == DISCLAIMER
    stamped = stamp_page_claim(claim, payload(_PINNED.read_bytes()),
                               scope_version=document.scope_version)
    assert stamped.declared_precision_label == "no_exact_address"


def test_a_page_without_the_sentence_declares_nothing() -> None:
    """The negative control for the `body` scope: `html_marker` fires off the map config's
    `infoText` too, so the only way to show the reader is not simply always-true is a page
    carrying neither occurrence."""
    assert read("id.det.no_exact_disclaimer",
                scoped(page(info="Na mapě zobrazujeme jen nemovitosti s přesnou adresou.",
                            disclaimer=False))) == []


# ------------------------------------------------------------ the town coverage line

@pytest.mark.parametrize("path,native,town", TOWN_BODIES,
                         ids=[p.name for p, _, _ in TOWN_BODIES])
def test_the_town_extracts_from_every_committed_idnes_body(
    path: Path, native: str, town: str,
) -> None:
    """Rule 25's invariant, measured rather than asserted: every committed body of this
    portal — modelled, real, blurred, Prague — yields its town through the lane's own entry
    point, with the scoper, the page-kind filter and both evidence validators applied."""
    result = extract_page(payload(path.read_bytes(), native), row(native),
                          fx.entries_for("idnes"), register=register())
    towns = [c.value_text for c in result.claims if c.claim_type == "obec_name"]
    assert towns == [town]


def test_the_prague_body_states_the_city_the_quarter_and_no_okres() -> None:
    """The R4 case on real bytes: the address line says "Praha 5 - Hlubočepy" and the
    dataLayer's `listing_localityDistrict` says "Praha 5", both of them městské obvody. The
    town claim is "Praha", the obvod is claimed as the quarter, and NO okres is claimed —
    Prague has none, and the district field would have manufactured one."""
    document = scoped((_REFETCH / "idnes_b1.html").read_bytes())
    native = "6a5a34e1f582fb9b160859ec"
    assert one_claim("id.det.obec", document, native=native).value_text == "Praha"
    assert one_claim("id.det.cast_obce", document, native=native).value_text == "Hlubočepy"
    assert read("id.det.okres", document, native=native) == []
    assert '"listing_localityDistrict":"Praha 5"' in (
        _REFETCH / "idnes_b1.html").read_text(encoding="utf-8")


def test_a_data_layer_city_that_arrives_as_an_obvod_is_folded_to_the_city() -> None:
    """The chained `statutory_city_obec` (R4), proved rather than trusted: RÚIAN has no obec
    called "Praha 5", so a town claim carrying the obvod would resolve to nothing at all and
    read as a portal that publishes no town."""
    body = page(data_layer={"listing_localityCity": "Praha 5",
                            "listing_localityCityArea": "Hlubočepy"})
    assert one_claim("id.det.obec", scoped(body)).value_text == "Praha"


def test_the_blurred_refetch_pair_yields_the_town_but_no_pin() -> None:
    """Why the address line carries street/okres/number instead of the map feature's own
    `properties.address`: idnes maps only exact-address listings ("Na mapě zobrazujeme jen
    nemovitosti s přesnou adresou"), so on this blurred listing NONE of the 21 features is
    the subject's and an id-matched address read would claim nothing at all."""
    native = "6a304242115442e33b0dbd24"
    document = scoped((_REFETCH / "idnes_a1.html").read_bytes())
    with pytest.raises(SubjectNotFound):
        read("id.det.subject_feature", document, native=native)
    assert one_claim("id.det.obec", document, native=native).value_text == "Králův Dvůr"
    assert one_claim("id.det.okres", document, native=native).value_text == "Beroun"
    assert one_claim("id.det.street", document, native=native).value_text == "Nad Stadionem"
    assert one_claim("id.det.no_exact_disclaimer", document,
                     native=native).value_text == "no_exact_address"


# --------------------------------------------------- the dataLayer capture pattern

def script_match() -> re.Pattern[str]:
    pattern = entry("id.det.obec").locator["script_match"]
    assert pattern == entry("id.det.kraj").locator["script_match"]
    assert pattern == entry("id.det.cast_obce").locator["script_match"]
    return re.compile(str(pattern))


# Real bodies write `listing_labels` as `null` (pinned + archived) or as a string (the three
# refetch bodies). Either way it is the field most likely to grow an object, so it is where
# nesting is injected — into the REAL bytes, never into a hand-written push.
_LABELS = re.compile(r'"listing_labels"\s*:\s*("[^"]*"|null)')

NESTED_SHAPES = [
    '{"badge":"discount"}',
    '{"badge":{"kind":"discount"}}',
    '{"badge":{"kind":{"code":"discount"}}}',
    '{"badge":{"kind":{"code":{"id":7}}}}',
    '{"ecommerce":{"items":[{"item_id":{"x":1}}]}}',
]


def with_nested_labels(path: Path, shape: str) -> str:
    body = path.read_text(encoding="utf-8")
    patched, count = _LABELS.subn(f'"listing_labels":{shape}', body, count=1)
    assert count == 1, f"{path.name} carries no listing_labels to nest into"
    return patched


@pytest.mark.parametrize("shape", NESTED_SHAPES)
@pytest.mark.parametrize("path,native,town", [(b, n, t) for b, n, t in TOWN_BODIES])
def test_the_capture_survives_any_nesting_depth_inside_the_view_detail_push(
        path: Path, native: str, town: str, shape: str) -> None:
    """The regression this pattern was rewritten for, pinned as the PROPERTY and not as a
    boundary. The day `listing_labels` becomes an object of ANY depth — the GA4
    `ecommerce.items[]` shape is the realistic one — a capture that enumerates brace levels
    stops matching. Silently: a subject-scoped miss is an absence, not an error, so the
    MANDATORY town entry (rule 25) would emit nothing on every idnes listing, taking kraj and
    část obce with it. The shipped pattern counts no braces — it ends on the push's own
    closing `});`."""
    nested = with_nested_labels(path, shape)

    found = script_match().search(nested)
    assert found is not None
    assert json.loads(found.group("config"))["listing_localityCity"] == town
    document = scoped(nested)
    assert one_claim("id.det.obec", document, native=native).value_text == town
    assert read("id.det.kraj", document, native=native)
    assert len(script_match().findall(nested)) == 1


def test_every_brace_counting_capture_this_pattern_replaced_breaks_on_real_nesting() -> None:
    """Why the shipped pattern counts no braces. Both earlier drafts of this line are exact
    regexes, and each one dies at the depth just past the one it enumerates — the second is
    the version a reviewer caught: it tolerated the two-level `badge` object its own test
    injected and broke on the three-level shape the portal is free to serve tomorrow."""
    depth_zero = re.compile(
        r'dataLayer\.push\(\s*(?P<config>\{\s*"event"\s*:\s*"viewDetail"[^{}]*\})')
    depth_two = re.compile(
        r'dataLayer\.push\(\s*(?P<config>\{\s*"event"\s*:\s*"viewDetail"'
        r'(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\})\s*\)')
    body = _PINNED.read_text(encoding="utf-8")
    at = {depth: body.replace('"listing_labels":null',
                              '"listing_labels":' + shape)
          for depth, shape in ((1, '{"a":1}'),
                               (2, '{"a":{"b":1}}'),
                               (3, '{"a":{"b":{"c":1}}}'))}

    assert depth_zero.search(at[1]) is None
    assert depth_two.search(at[1]) is not None
    assert depth_two.search(at[2]) is not None
    assert depth_two.search(at[3]) is None
    for depth, text in at.items():
        assert script_match().search(text) is not None, depth


def test_the_capture_is_dot_free_because_this_lane_compiles_it_without_dotall() -> None:
    r"""`page_readers.embedded_documents` compiles `script_match` with a bare `re.compile`,
    so `.` never crosses the newline the real push is written across. The pattern therefore
    spells its wildcard `[\s\S]`, and this rail fails if someone "simplifies" it back."""
    pattern = str(entry("id.det.obec").locator["script_match"])
    assert r"[\s\S]*?" in pattern
    assert "." not in pattern.replace(r"\.", "")

    naive = re.compile(pattern.replace(r"[\s\S]*?", ".*?"))
    assert naive.search(_ARCHIVED.read_text(encoding="utf-8")) is None
    assert script_match().search(_ARCHIVED.read_text(encoding="utf-8")) is not None


def test_the_capture_takes_the_view_detail_push_and_never_the_ga_event_one() -> None:
    """Both committed shapes carry a SECOND `dataLayer.push` — the mortgage-click GAEvent
    handler, nested inside a function — and a brace-tolerant pattern that swallowed it would
    capture text that is not JSON and drop the whole document."""
    for path in (_PINNED, _ARCHIVED):
        captured = [m.group("config")
                    for m in script_match().finditer(path.read_text(encoding="utf-8"))]
        assert len(captured) == 1, path.name
        assert "GAEvent" not in captured[0]
        assert json.loads(captured[0])["event"] == "viewDetail"


def test_the_pinned_bodys_push_block_is_the_real_pages_bytes() -> None:
    """Provenance rail for the block added to the modelled fixture: it is COPIED from
    `portal_html/idnes_detail.html`, the real archived page of this same listing, so the
    fixture cannot drift into stating a shape the portal does not serve."""
    captured = [m.group("config")
                for m in script_match().finditer(_PINNED.read_text(encoding="utf-8"))]
    original = [m.group("config")
                for m in script_match().finditer(_ARCHIVED.read_text(encoding="utf-8"))]
    assert captured == original
    assert json.loads(captured[0])["listing_id"] == NATIVE


# ------------------------------------------------- subject selection is id-driven

def test_the_pin_is_the_subjects_and_never_the_neighbours() -> None:
    """idnes ships up to 20 neighbour features per page, each with a complete address, so a
    positional pick is precisely how a neighbour's pin becomes this listing's. Here the
    SUBJECT is written second, so `features[0]` would be the wrong answer."""
    body = page(json.dumps([
        feature(NEIGHBOUR, 15.31840, 50.75120, address="Krkonošská 512, Desná", similar=True),
        feature(NATIVE, 15.31331632, 50.74437214),
    ]))
    assert one_claim("id.det.subject_feature", scoped(body)).value_text == (
        "50.74437214,15.31331632")


def test_a_page_carrying_no_object_for_this_listing_claims_nothing() -> None:
    """`on_miss: fail` is the only mode implemented and it means NO CLAIM — specifically not
    `features[0]`'s coordinate, and not another listing's dataLayer block. Raised rather than
    returned empty so the lane can COUNT the cohort instead of reading a fleet-wide id-scheme
    change as a green zero-claim sweep."""
    body = page(json.dumps([feature(NEIGHBOUR, 15.31840, 50.75120)]))
    for entry_id in ("id.det.subject_feature", "id.det.obec", "id.det.kraj",
                     "id.det.cast_obce"):
        with pytest.raises(SubjectNotFound):
            read(entry_id, scoped(body), native="999999")


def test_two_features_carrying_this_listings_id_are_not_evidence_either() -> None:
    body = page(json.dumps([feature(NATIVE, 15.31331632, 50.74437214),
                            feature(NATIVE, 14.42076, 50.08804)]))
    with pytest.raises(SubjectNotFound):
        read("id.det.subject_feature", scoped(body))


def test_exclude_where_is_load_bearing_on_the_shipped_locator() -> None:
    """The contract's own exclusion zone is `features[isSimilar=true]`, a predicate
    `html_scope` cannot execute and therefore defers to the reader. A feature carrying the
    subject's id AND `isSimilar: true` is a neighbour card the portal keyed wrong; admitting
    it would import exactly what the zone exists to strip."""
    body = page(json.dumps([feature(NATIVE, 15.31331632, 50.74437214, similar=True)]))
    with pytest.raises(SubjectNotFound):
        read("id.det.subject_feature", scoped(body))


def test_a_subject_whose_geometry_is_not_a_point_yields_no_coordinate() -> None:
    """A marked area is not a pin, and reading its first vertex as one would be a
    fabrication. The eleven-type vocabulary has no shape claim to fall back to (W1-c R1), so
    the honest outcome is no claim."""
    subject = feature(NATIVE, 15.31331632, 50.74437214)
    subject["geometry"] = {"type": "Polygon",
                           "coordinates": [[[15.3, 50.7], [15.4, 50.7], [15.4, 50.8],
                                            [15.3, 50.7]]]}
    assert read("id.det.subject_feature", scoped(page(json.dumps([subject])))) == []


def test_a_subject_miss_costs_only_the_id_matched_entries() -> None:
    """The accepted asymmetry, asserted so it can never become an accident: the four
    id-matched entries abstain on a body keyed to another listing and are COUNTED, while the
    five `.b-detail__info` entries claim from whatever body the archive handed them — which
    is sound because a payload row is keyed (source, source_id_native) and the line is the
    subject's own header block, and is why they declare `subject_scoped: true` explicitly."""
    result = extract_page(payload(_PINNED.read_bytes(), native="999999"), row("999999"),
                          fx.entries_for("idnes"), register=register())
    assert sorted(c.extractor_id for c in result.claims) == [
        "id.det.no_exact_disclaimer", "id.det.okres", "id.det.street"]
    assert dict(result.refusals) == {"subject_not_found:idnes": 4}
    assert all(c.subject_scoped is True for c in result.claims)


@pytest.mark.parametrize("entry_id", [
    "id.det.no_exact_disclaimer", "id.det.country", "id.det.okres", "id.det.street",
    "id.det.cp", "id.det.co",
])
def test_the_page_scoped_entries_declare_their_scope_rather_than_defaulting(
    entry_id: str,
) -> None:
    """`subject_scoped` reaches the claim from the CONTRACT (`subject_scope.subject_scoped`),
    and `resolver/survivorship` refuses only an explicit False — so a defaulted True is a
    rankable claim nobody declared. Declared here, because these entries are the ones a
    mis-keyed body could contaminate."""
    assert entry(entry_id).subject_scope == {"subject_scoped": True}


def test_the_exclusion_register_strips_the_two_blocks_that_carry_other_addresses() -> None:
    """The other half of the subject-scoping argument for the address line. On live markup
    the foreign addresses sit in `div.grid-similar-offers` (the recommendation rail, 20
    neighbour listings) and in `.b-detail-contact` (the agency's own street), plus the map
    features the predicate zone excludes. Both DOM blocks are the register gaps
    `tests/location_data/test_html_scope.py` pinned for idnes; v3 closes them. The two zones
    idnes@2 declared match NOTHING on the real page — the portal renamed both — so a green
    sweep was never evidence that the boundary still fit."""
    document = scoped(_ARCHIVED.read_bytes())
    text = _ARCHIVED.read_text(encoding="utf-8")
    assert document.is_complete
    assert document.zones_unmatched == (".b-similar", ".broker")
    assert "grid-similar-offers" in text and "b-detail-contact" in text
    assert document.css_first("div.grid-similar-offers") is None
    assert document.css_first(".b-detail-contact") is None
    # Node grain for the rail, value grain for the contact block, and the difference is the
    # point: a neighbour's town reaches the page TWICE — through the rail and through the
    # map features, which are a deferred zone that must survive — so only "is the block
    # standing" can tell the two apart. The agency's street has no second carrier.
    assert "Josefův Důl - Dolní Maxov" in text
    assert not document.contains("Arbesova")
    # …and what survives is the subject's own line, the one the address entries read.
    assert document.css_first(".b-detail__info").text().strip() == (
        "Na Balkáně, Tanvald - Šumburk nad Desnou, okres Jablonec nad Nisou")


# ------------------------------------------------------------ the two refusals on the pin

@pytest.mark.parametrize("lat,lon,admitted", [
    (49.19186, 16.61109, False),   # 119 active rows, Brno centre, street NULL [live-B §1.3]
    (49.19752, 16.65812, False),   # 113 active rows, the same block
    (49.81150, 15.61824, False),   # 71 rows, the CZ geographic centroid, 56 municipalities
    (49.19286, 16.61109, True),    # 0.001° away: the veto is a pin, not a neighbourhood
    (50.12413, 14.12853, True),    # 58 rows and DELIBERATELY not on the list — a real cluster
])
def test_the_contract_rejects_only_the_pins_it_enumerates(
    lat: float, lon: float, admitted: bool,
) -> None:
    """Junk-pin calibration is contract data, never a code constant, and it is ENUMERATED
    rather than inferred from pin-sharing: the Kladno cluster shares a pin 58 ways and is a
    legitimate development, so "many listings share this pin" is a corpus statistic for a
    different lane."""
    assert entry("id.det.subject_feature").locator["reject_points"] == [
        "49.19186,16.61109", "49.19752,16.65812", "49.81150,15.61824"]
    body = page(json.dumps([feature(NATIVE, lon, lat)]))
    assert bool(read("id.det.subject_feature", scoped(body))) is admitted


def test_the_cz_envelope_is_genuinely_evaluated_on_the_shipped_entry() -> None:
    """16,833 active idnes rows sit outside the CZ bbox with obec/okres/region/ku_id all NULL
    [db-cov §4.2]. `guards: [reject_outside_cz_bbox]` is only worth declaring because
    `json_point` calls `guard_admits` — a guard the runtime ignores is a rail that reads as
    protection and is not."""
    assert entry("id.det.subject_feature").guards == ("reject_outside_cz_bbox",)
    body = page(json.dumps([feature(NATIVE, -3.70379, 40.41678)]))  # Madrid
    assert read("id.det.subject_feature", scoped(body)) == []


def test_the_pin_branch_is_licensed_portal_by_the_ladder_not_by_the_reader() -> None:
    """C6: which branch of the portal's map produced a position IS its licence class, and the
    LADDER stamps it from `ARCHIVED_COORDINATE_RULES` rather than the reader stamping itself.
    idnes' rule names this exact entry id, so a renamed entry would be refused as
    `unrecognised_archived_coordinate_locator` — the ladder working, not a bug."""
    document = scoped(_PINNED.read_bytes())
    reads = read("id.det.subject_feature", document)
    assert reads[0].position_branch == "portal_pin"
    stamped = stamp_page_claim(reads[0].claim, payload(_PINNED.read_bytes()),
                               scope_version=document.scope_version)
    licensed, reason = _licensed_coordinate(
        stamped, row(), entry("id.det.subject_feature"), reads[0].position_branch)
    assert licensed is not None
    assert licensed.licence_class == "portal"
    assert reason == "archived_id.det.subject_feature"


def test_an_unruled_coordinate_locator_gets_a_counted_refusal_and_no_claim() -> None:
    """`ARCHIVED_COORDINATE_RULES` names ONE locator per portal, and since W4-b deleted the
    Mapy veto above it that name is the whole licence. The refusal is COUNTED: one that left
    no trace would be indistinguishable from a page that carried no pin."""
    impostor = replace(entry("id.det.subject_feature"), entry_id="id.det.not_the_rule")
    result = extract_page(payload(_PINNED.read_bytes()), row(), [impostor],
                          register=register())
    assert result.claims == []
    assert dict(result.refusals) == {"unrecognised_archived_coordinate_locator": 1}


# ------------------------------------ the real archived page: an unparseable blob

def test_the_repos_real_idnes_capture_no_longer_carries_parseable_map_json() -> None:
    """States WHY the body below is the regression used, and it is a PII rail, not a nit:
    `anonymize()`'s `_PHONE_RE` sweeps every 9-digit run outside a URL and rewrote this
    page's coordinate arrays to `[15.+420 XXX XXX XXX7,50.+420 XXX XXX XXX8]`. The digits are
    gone, so the file cannot be repaired — anyone 'fixing' it by inventing coordinates would
    be fabricating a fixture. A future re-capture must use
    `scripts/fetch_and_anonymize_fixtures.py --scrub-contacts`."""
    node = LexborHTMLParser(_ARCHIVED.read_text(encoding="utf-8")).css_first(
        "script[data-maptiler-json]")
    assert node is not None
    assert "+420 XXX XXX XXX" in node.text()
    with pytest.raises(ValueError):
        json.loads(node.text())


def test_an_unparseable_blob_costs_the_pin_and_nothing_else() -> None:
    """A malformed PAGE is not in the document list; only a malformed CONTRACT raises. One
    portal changing shape must not abort a batch of thousands, and an archived body is
    immutable, so a raise here would be a permanently failing row rather than a retryable
    one. The dataLayer block is a DIFFERENT script node, so the town survives."""
    document = scoped(_ARCHIVED.read_bytes())
    with pytest.raises(SubjectNotFound):
        read("id.det.subject_feature", document)
    result = extract_page(payload(_ARCHIVED.read_bytes()), row(),
                          fx.entries_for("idnes"), register=register())
    assert sorted(c.extractor_id for c in result.claims) == [
        "id.det.cast_obce", "id.det.kraj", "id.det.obec", "id.det.okres", "id.det.street"]
    assert dict(result.refusals) == {"subject_not_found:idnes": 1}


def test_the_real_page_carries_the_other_sentence_and_declares_no_blur() -> None:
    """`html_marker` is presence-only and its literal is the disclaimer, so the exact-address
    page — whose `infoText` is 'Na mapě zobrazujeme jen nemovitosti s přesnou adresou.' —
    matches nothing. The absent branch is a different entry, never a false value on this
    one."""
    text = _ARCHIVED.read_text(encoding="utf-8")
    assert "Na mapě zobrazujeme jen nemovitosti s přesnou adresou." in text
    assert DISCLAIMER not in text
    assert read("id.det.no_exact_disclaimer", scoped(_ARCHIVED.read_bytes())) == []


def test_the_real_pages_address_line_is_split_and_never_claimed_whole() -> None:
    """idnes@2 kept a `street_name` entry over the WHOLE `.b-detail__info` line inert for
    exactly this reason. idnes@3 claims the line's parts through the shared address
    transforms, so the guarantee survives as a positive: the street is the leading segment,
    the okres comes off its own qualifier, and no claim carries the line itself."""
    document = scoped(_ARCHIVED.read_bytes())
    assert document.is_complete
    line = document.css_first(".b-detail__info").text().strip()
    assert one_claim("id.det.street", document).value_text == "Na Balkáně"
    assert one_claim("id.det.okres", document).value_text == "Jablonec nad Nisou"
    result = extract_page(payload(_ARCHIVED.read_bytes()), row(),
                          fx.entries_for("idnes"), register=register())
    assert all(c.value_text != line for c in result.claims)


# ------------------------------------------------------------ the address-line transforms

def test_the_house_number_splits_into_cp_and_co_when_the_line_carries_one() -> None:
    """`Krkonošská 512/3` — the čp/čo pair the portal glues to the street segment. Both
    halves are typed claims because RÚIAN addresses them separately, and the split is the
    shared `split_cp_co`, never a second regex in this contract."""
    document = scoped(page(address="Krkonošská 512/3, Desná - Desná I, okres Jablonec nad Nisou"))
    assert one_claim("id.det.street", document).value_text == "Krkonošská"
    assert one_claim("id.det.cp", document).value_text == "512"
    assert one_claim("id.det.co", document).value_text == "3"


def test_a_line_with_only_a_cp_claims_no_co() -> None:
    document = scoped(page(address="Krkonošská 512, Desná - Desná I, okres Jablonec nad Nisou"))
    assert one_claim("id.det.cp", document).value_text == "512"
    assert read("id.det.co", document) == []


def test_a_town_only_line_claims_no_street_and_no_number() -> None:
    """`address_part_street`'s gates are the shared scraper ones, so a leading segment that
    is a town claims nothing — and the house-number entry is gated on the same test, which is
    what stops a number being lifted off a segment this vocabulary refuses to call a
    street."""
    document = scoped(page(address="Tanvald, okres Jablonec nad Nisou"))
    assert read("id.det.street", document) == []
    assert read("id.det.cp", document) == []


def test_the_country_is_claimed_only_where_the_line_names_one() -> None:
    """"Foreign is a determination, never a default" (rule 25). The trailing segment is an
    obec far more often than a country, so `address_part_country` answers off a closed table
    and yields the ISO code rather than the spelling — `listing_localityState` is not the
    carrier because "CZ" on every domestic row is the default this claim type exists to
    avoid."""
    assert one_claim("id.det.country",
                     scoped(page(address="Ulica 5, Split, Chorvatsko"))).value_text == "HR"
    assert read("id.det.country", scoped(page(address="Tanvald, okres Jablonec"))) == []
    for path, native, _ in TOWN_BODIES:
        assert read("id.det.country", scoped(path.read_bytes()), native=native) == []
