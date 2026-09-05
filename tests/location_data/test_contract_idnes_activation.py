"""idnes@2 — the five entries W2-9 activated, exercised as the CONTRACT ships them.

`test_archive_reader_canon` proves the READERS. This file proves the idnes CONTRACT: every
assertion below loads `contracts/portals/idnes.yaml` through `fx.entries_for` and runs the
shipped locator — the real selector, the real pointer, the real `reject_points` list — over a
real body. A reader test that builds its own entry cannot catch a contract that names the
wrong pointer, declares the wrong branch, or forgets an exclusion; those are the mistakes a
portal activation actually makes.

Two bodies, and the difference between them is the point:
  * `tests/fixtures/location_w2/idnes_detail.html` — the pinned page the golden scores. Small,
    modelled on this contract, and the only body here that carries a subject feature.
  * `tests/fixtures/portal_html/idnes_detail.html` — a REAL archived idnes page whose map JSON
    was destroyed by the fixture anonymiser's phone sweep. It is not repairable (the digits
    are gone) and inventing coordinates for it would be fabricating a fixture, so it serves as
    the honest regression for "a body whose blob will not parse yields no claim and no
    exception".

Bodies a single archived page cannot carry — a junk pin, a foreign pin, a subject hidden
behind `isSimilar: true` — are built here as small HTML strings from the values [live-B §1.3]
measured, never as new fixture files, since nothing scores those.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from selectolax.lexbor import LexborHTMLParser

from location_data import contracts
from location_data.claims_intake import Entry, ListingRow
from location_data.claims_remine_archive import (
    ARCHIVE_READERS,
    ArchivedPayload,
    SubjectNotFound,
    _licensed_coordinate,
    extract_payload,
    stamp_archive_claim,
)
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
_PINNED = _ROOT / "tests" / "fixtures" / "location_w2" / "idnes_detail.html"
_ARCHIVED = _ROOT / "tests" / "fixtures" / "portal_html" / "idnes_detail.html"

CONTRACT = {c.source: c for c in contracts.load_all()}["idnes"]
CLOCK = datetime(2026, 1, 1, tzinfo=UTC)

# The id the pinned body's own subject feature is keyed by. Scoring under anything else makes
# every subject-scoped entry miss, which reads as "this contract extracts nothing".
NATIVE = "6a71888887e5da33ca081ad8"
NEIGHBOUR = "68badb8de7b021a4470fb87d"

# What v2 turned on, and what it deliberately did not. The second half is the load-bearing
# one: `id.det.locality_line` claims a whole address line as a `street_name`, so leaving it
# inert is a decision this file has to be able to notice being reversed.
ACTIVATED = {
    "id.det.subject_feature", "id.det.subject_address", "id.det.info_text",
    "id.det.zoom", "id.det.no_exact_disclaimer",
}
STILL_INERT = {
    "id.det.locality_line", "id.det.polygon", "id.det.neighbours",
    "id.det.og_url_slug", "id.det.og_description", "id.desc.country",
}

DISCLAIMER = "Nemovitost nemá přesnou adresu, nachází se ve vyznačené oblasti."


# ------------------------------------------------------------------ harness

def entries() -> dict[str, Entry]:
    return {e.entry_id: e for e in fx.entries_for("idnes")}


def entry(entry_id: str) -> Entry:
    return entries()[entry_id]


def row(native: str = NATIVE, *, in_mapy_inventory: bool = False) -> ListingRow:
    return fx.listing("idnes", {}, native=native, in_mapy_inventory=in_mapy_inventory)


def payload(body: bytes, native: str = NATIVE) -> ArchivedPayload:
    return ArchivedPayload(
        id=9001, source="idnes", source_id_native=native, page_kind="detail",
        payload_sha256="ab" * 32, first_observed_at=CLOCK, body=body)


def register() -> ScopeRegister:
    """This portal's OWN shipped exclusion register — the decoys a reader must not reach are
    the ones the contract declares, not ones a test invents."""
    return ScopeRegister.from_zones("idnes", CONTRACT.exclusion_zones)


def scoped(body: bytes | str) -> ScopedDocument:
    if isinstance(body, str):
        body = body.encode("utf-8")
    return scope_html(body, register=register())


def read(entry_id: str, document: ScopedDocument, *, native: str = NATIVE) -> list[Any]:
    item = entry(entry_id)
    return ARCHIVE_READERS[str(item.reader)](
        item, row(native), payload(b"", native), document)


def one_claim(entry_id: str, document: ScopedDocument, *, native: str = NATIVE) -> Any:
    reads = read(entry_id, document, native=native)
    assert len(reads) == 1, f"{entry_id} produced {len(reads)} reads"
    return reads[0].claim


def page(features: str, *, info: str = DISCLAIMER, zoom: int = 14,
         disclaimer: bool = True) -> str:
    """A minimal idnes detail page in the SHAPE the contract's selectors address."""
    body = f"<p class='b-detail__disclaimer'>{DISCLAIMER}</p>" if disclaimer else ""
    return (
        "<!DOCTYPE html><html lang='cs'><body>"
        + body
        + '<script type="application/json" data-maptiler-json>'
        + json.dumps({"mtMapOptions": {"zoom": zoom}, "infoText": info,
                      "geojson": {"type": "FeatureCollection",
                                  "features": json.loads(features)}},
                     ensure_ascii=False)
        + "</script></body></html>"
    )


def feature(native: str, lon: float, lat: float, *, address: str = "Na Balkáně 1, Tanvald",
            similar: bool = False) -> dict[str, Any]:
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"id": native, "address": address, "isSimilar": similar}}


# ------------------------------------------------------------------ what v2 activated

def test_v2_activates_exactly_five_entries_and_leaves_the_line_parser_inert() -> None:
    """The activation's own census. `id.det.locality_line` reads `.b-detail__info`, which is
    'Street, City - Quarter[, okres X]', and claims it as a `street_name`: the only reader that
    could run it takes the node's WHOLE text, so turning it on would claim an address line as
    a street on every idnes listing. The same text is captured correctly typed by
    `id.det.subject_address`."""
    assert CONTRACT.version == 2
    executable = {e.entry_id for e in CONTRACT.entries if e.reader}
    declared_ahead = {e.entry_id for e in CONTRACT.entries if not e.reader}
    assert ACTIVATED <= executable
    assert STILL_INERT <= declared_ahead
    assert not (ACTIVATED & declared_ahead)


def test_the_activation_ships_shadowed() -> None:
    """`shadow` is HEADER-grain and `project()` deactivates v1 when it activates v2, so this
    freezes idnes' already-live W1 claims until an operator runs `--unshadow idnes@2`. That is
    the sequencing ruling, and it belongs in a test because a dropped `shadow:` line is a
    one-character diff that ships a portal live."""
    assert CONTRACT.shadow is True


def test_every_activated_entry_names_a_reader_this_lane_implements() -> None:
    """Not a tautology: an entry may name a reader the CONTRACT gate knows (`READER_CONTRACTS`)
    that the archived lane does not register, and W1 would then skip it while nothing else ran
    it — a silent coverage hole with no error anywhere."""
    for entry_id in sorted(ACTIVATED):
        assert str(entry(entry_id).reader) in ARCHIVE_READERS, entry_id


# ------------------------------------------- the pinned body, entry by entry

def test_the_pinned_body_scopes_complete_and_keeps_the_map_script() -> None:
    """The second exclusion-zone shape: `script[data-maptiler-json]` is excluded only at
    `/geojson/features[isSimilar=true]`, a PREDICATE no RFC 6901 pointer can pop, so the
    script node itself must SURVIVE scoping and the exclusion is honoured by the reader."""
    document = scoped(_PINNED.read_bytes())
    assert document.is_complete
    assert document.zones_unmatched == ()
    assert document.css_first("script[data-maptiler-json]") is not None


@pytest.mark.parametrize("entry_id,value", [
    ("id.det.subject_feature", "50.74437214,15.31331632"),
    ("id.det.subject_address",
     "Na Balkáně, Tanvald - Šumburk nad Desnou, okres Jablonec nad Nisou"),
    ("id.det.info_text", DISCLAIMER),
    ("id.det.zoom", "14"),
    ("id.det.no_exact_disclaimer", "no_exact_address"),
])
def test_each_activated_entry_reads_its_value_off_the_pinned_body(
    entry_id: str, value: str,
) -> None:
    assert one_claim(entry_id, scoped(_PINNED.read_bytes())).value_text == value


@pytest.mark.parametrize("entry_id", sorted(ACTIVATED))
def test_every_evidence_quote_resolves_to_a_real_span(entry_id: str) -> None:
    """An `evidence_quote` is a promise the payload contains that text (01 §4.2 pairs it with
    `payload_sha256`). Migration 382's CHECK only tests that the quote is a substring, so a
    span pointing at the WRONG occurrence still passes — asserting the slice back is what
    makes the promise real."""
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


def test_the_zoom_is_stored_as_a_number_and_quoted_with_its_own_key() -> None:
    """`14` occurs a dozen times in a map config and `find_span` returns the first hit, so the
    quote carries the key. `value_num` is what OQ3 will be answered from."""
    claim = one_claim("id.det.zoom", scoped(_PINNED.read_bytes()))
    assert (claim.value_text, claim.value_num) == ("14", 14.0)
    assert claim.evidence_quote == '"zoom": 14'


def test_the_disclaimer_claims_the_contracts_label_and_declares_blur() -> None:
    """The claim's VALUE is this contract's canonical label and its EVIDENCE is the portal's
    verbatim sentence — two different fields for exactly this case. Blur comes from the
    label's membership of `precision_cap.blurred_labels`, so recalibrating it is a version
    bump rather than a code change (06 §6.6 rule 7)."""
    claim = one_claim("id.det.no_exact_disclaimer", scoped(_PINNED.read_bytes()))
    assert claim.value_text == "no_exact_address"
    assert claim.declared_precision_label == "no_exact_address"
    assert claim.blur_evidence == "declared"
    assert claim.evidence_quote == DISCLAIMER


def test_the_info_text_is_stored_verbatim_and_labels_nothing() -> None:
    """idnes writes two different sentences into `infoText`, and mapping a sentence onto a
    label is calibration that belongs on the disclaimer entry, which owns the blurred-label
    set. A scalar reader inventing a label here would let a Czech sentence become
    `DeclaredPrecision.label`."""
    claim = one_claim("id.det.info_text", scoped(_PINNED.read_bytes()))
    assert claim.value_text == DISCLAIMER
    assert claim.declared_precision_label is None
    assert claim.blur_evidence == "none"


# ------------------------------------------------- subject selection is id-driven

def test_the_address_read_is_the_subjects_and_never_the_neighbours() -> None:
    """idnes ships up to 20 neighbour features per page, each with a complete address, so a
    positional pick is precisely how a neighbour's address becomes this listing's street.
    Here the SUBJECT is written second, so `features[0]` would be the wrong answer."""
    body = page(json.dumps([
        feature(NEIGHBOUR, 15.31840, 50.75120, address="Krkonošská 512, Desná",
                similar=True),
        feature(NATIVE, 15.31331632, 50.74437214,
                address="Na Balkáně, Tanvald - Šumburk nad Desnou"),
    ]))
    document = scoped(body)
    assert one_claim("id.det.subject_address", document).value_text == (
        "Na Balkáně, Tanvald - Šumburk nad Desnou")
    assert one_claim("id.det.subject_feature", document).value_text == (
        "50.74437214,15.31331632")


def test_a_page_carrying_no_feature_for_this_listing_claims_nothing() -> None:
    """`on_miss: fail` is the only mode implemented and it means NO CLAIM — specifically not
    `features[0]`'s coordinate. Raised rather than returned empty so the lane can count the
    cohort instead of reading a fleet-wide id-scheme change as a green zero-claim sweep."""
    body = page(json.dumps([feature(NEIGHBOUR, 15.31840, 50.75120)]))
    for entry_id in ("id.det.subject_feature", "id.det.subject_address"):
        with pytest.raises(SubjectNotFound):
            read(entry_id, scoped(body), native=NATIVE)


def test_two_features_carrying_this_listings_id_are_not_evidence_either() -> None:
    body = page(json.dumps([
        feature(NATIVE, 15.31331632, 50.74437214),
        feature(NATIVE, 14.42076, 50.08804),
    ]))
    with pytest.raises(SubjectNotFound):
        read("id.det.subject_feature", scoped(body))


def test_exclude_where_is_load_bearing_on_the_shipped_locator() -> None:
    """The contract's own exclusion zone is `features[isSimilar=true]`, a predicate `html_scope`
    cannot execute and therefore defers. A feature carrying the subject's id AND
    `isSimilar: true` is a neighbour card the portal keyed wrong; admitting it would import
    exactly what the zone exists to strip."""
    body = page(json.dumps([feature(NATIVE, 15.31331632, 50.74437214, similar=True)]))
    with pytest.raises(SubjectNotFound):
        read("id.det.subject_feature", scoped(body))


def test_a_subject_whose_geometry_is_not_a_point_yields_no_coordinate() -> None:
    """A marked area is a different claim type (`id.det.polygon`, still inert) and reading its
    first vertex as a pin would be a fabrication."""
    subject = feature(NATIVE, 15.31331632, 50.74437214)
    subject["geometry"] = {"type": "Polygon",
                           "coordinates": [[[15.3, 50.7], [15.4, 50.7], [15.4, 50.8],
                                            [15.3, 50.7]]]}
    body = page(json.dumps([subject]))
    assert read("id.det.subject_feature", scoped(body)) == []
    # The address on the same feature is unaffected: a shape-less pin is not a shape-less page.
    assert one_claim("id.det.subject_address", scoped(body)).value_text == (
        "Na Balkáně 1, Tanvald")


# ------------------------------------------------- the two refusals on the pin

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
    declared = entry("id.det.subject_feature").locator["reject_points"]
    assert declared == ["49.19186,16.61109", "49.19752,16.65812", "49.81150,15.61824"]
    body = page(json.dumps([feature(NATIVE, lon, lat)]))
    reads = read("id.det.subject_feature", scoped(body))
    assert bool(reads) is admitted


def test_the_cz_envelope_is_genuinely_evaluated_on_the_shipped_entry() -> None:
    """16,833 active idnes rows sit outside the CZ bbox with obec/okres/region/ku_id all NULL
    [db-cov §4.2]. `guards: [reject_outside_cz_bbox]` is only worth declaring because
    `json_point` calls `guard_admits` — a guard the runtime ignores is a rail that reads as
    protection and is not."""
    assert entry("id.det.subject_feature").guards == ("reject_outside_cz_bbox",)
    body = page(json.dumps([feature(NATIVE, -3.70379, 40.41678)]))  # Madrid
    assert read("id.det.subject_feature", scoped(body)) == []


# ------------------------------------------------------------------ the licence ladder

def test_the_pin_branch_is_licensed_portal_by_the_ladder_not_by_the_reader() -> None:
    """C6: which branch of the portal's map produced a position IS its licence class, and the
    LADDER stamps it from `ARCHIVED_COORDINATE_RULES` rather than the reader stamping itself.
    idnes' rule names this exact entry id, so a renamed entry would be refused as
    `unrecognised_archived_coordinate_locator` — the ladder working, not a bug."""
    document = scoped(_PINNED.read_bytes())
    reads = read("id.det.subject_feature", document)
    assert reads[0].position_branch == "portal_pin"
    stamped = stamp_archive_claim(reads[0].claim, payload(_PINNED.read_bytes()),
                                  scope_version=document.scope_version)
    licensed, reason = _licensed_coordinate(
        stamped, row(), entry("id.det.subject_feature"), reads[0].position_branch)
    assert licensed is not None
    assert licensed.licence_class == "portal"
    assert reason == "archived_id.det.subject_feature"


def test_a_listing_in_the_mapy_inventory_gets_an_absence_and_no_coordinate() -> None:
    """The Mapy veto applies on the archived substrate exactly as it does on the payload one,
    and it is RECORDED: a refused coordinate that left no absence would be indistinguishable
    from a page that carried no pin."""
    result = extract_payload(
        payload(_PINNED.read_bytes()), row(in_mapy_inventory=True),
        [entry("id.det.subject_feature")], register=register())
    assert result.claims == []
    assert [(a.field_, a.reason, a.detail) for a in result.absences] == [
        ("coordinate", "not_attempted", "listing_in_mapy_affected_inventory")]


def test_the_whole_contract_over_the_pinned_body_produces_the_five_claims() -> None:
    """The lane's own entry point, not a per-reader call: `extract_payload` applies the page
    kind filter, the scoper, the licence ladder and both evidence validators, so this is the
    only assertion here that proves the five claims survive everything between a reader and
    the INSERT."""
    result = extract_payload(
        payload(_PINNED.read_bytes()), row(), fx.entries_for("idnes"),
        register=register())
    assert sorted(c.extractor_id for c in result.claims) == sorted(ACTIVATED)
    assert result.absences == []
    assert {c.surface for c in result.claims} == {"archived_html"}
    assert {c.page_kind for c in result.claims} == {"detail"}


def test_a_subject_miss_becomes_one_absence_per_subject_scoped_entry() -> None:
    """The lane's half of `on_miss: fail`: a per-row portal fact (a re-id, a redirect, an
    interstitial saved under the wrong key) must never roll back a batch of thousands, and it
    must never be silent. The three entries that do NOT select a subject still claim."""
    result = extract_payload(
        payload(_PINNED.read_bytes(), native="999999"), row("999999"),
        fx.entries_for("idnes"), register=register())
    assert sorted(c.extractor_id for c in result.claims) == [
        "id.det.info_text", "id.det.no_exact_disclaimer", "id.det.zoom"]
    assert sorted(a.field_ for a in result.absences) == [
        "address_line_verbatim", "coordinate"]
    assert all("on_miss=fail" in (a.detail or "") for a in result.absences)


# ------------------------------------ the real archived page: an unparseable blob

def test_the_repos_real_idnes_capture_no_longer_carries_parseable_map_json() -> None:
    """States WHY the body below is the regression used. `anonymize()`'s `_PHONE_RE` sweeps
    every 9-digit run outside a URL and rewrote this page's coordinate arrays to
    `[15.+420 XXX XXX XXX7,50.+420 XXX XXX XXX8]`. The digits are gone, so the file cannot be
    repaired — anyone 'fixing' it by inventing coordinates would be fabricating a fixture. A
    future re-capture must use `scripts/fetch_and_anonymize_fixtures.py --scrub-contacts`."""
    node = LexborHTMLParser(_ARCHIVED.read_text(encoding="utf-8")).css_first(
        "script[data-maptiler-json]")
    assert node is not None
    with pytest.raises(ValueError):
        json.loads(node.text())


def test_an_unparseable_blob_yields_no_claim_and_no_exception() -> None:
    """A malformed PAGE is not in the document list; only a malformed CONTRACT raises. One
    portal changing shape must not abort a batch of thousands, and an archived body is
    immutable, so a raise here would be a permanently failing row rather than a retryable one.
    `json_scalar` returns empty; the two subject-scoped entries record a miss."""
    document = scoped(_ARCHIVED.read_bytes())
    assert read("id.det.zoom", document) == []
    assert read("id.det.info_text", document) == []
    for entry_id in ("id.det.subject_feature", "id.det.subject_address"):
        with pytest.raises(SubjectNotFound):
            read(entry_id, document)


def test_the_real_page_carries_the_other_sentence_and_declares_no_blur() -> None:
    """`html_marker` is presence-only and its literal is the disclaimer, so the exact-address
    page — whose `infoText` is 'Na mapě zobrazujeme jen nemovitosti s přesnou adresou.' —
    matches nothing. The absent branch is a different entry, never a false value on this one."""
    text = _ARCHIVED.read_text(encoding="utf-8")
    assert "Na mapě zobrazujeme jen nemovitosti s přesnou adresou." in text
    assert DISCLAIMER not in text
    assert read("id.det.no_exact_disclaimer", scoped(_ARCHIVED.read_bytes())) == []


def test_the_real_pages_address_line_produces_no_street_claim() -> None:
    """The non-vacuity check on leaving `id.det.locality_line` inert: this page's
    `.b-detail__info` is a whole address line, and no entry of idnes@2 claims it as a
    `street_name`."""
    document = scoped(_ARCHIVED.read_bytes())
    # Non-vacuity: `extract_payload` returns nothing but absences on an incomplete scope, so
    # a hole in the boundary would make the assertion below true for the wrong reason.
    assert document.is_complete
    assert document.css_first(".b-detail__info") is not None
    result = extract_payload(payload(_ARCHIVED.read_bytes()), row(),
                             fx.entries_for("idnes"), register=register())
    assert [c for c in result.claims if c.claim_type == "street_name"] == []


def test_the_shipped_exclusion_register_already_names_markup_this_portal_dropped() -> None:
    """Recorded because nothing else records it: `claims_remine_archive` never reads
    `zones_unmatched`, so a corpus-wide register miss is invisible and every batch still
    stamps 'ok'. On the repo's real archived page BOTH html zones match nothing — the portal
    renamed the 'Podobné' and broker blocks — while the predicate zone on the map config is
    the one this contract actually leans on. A green sweep is therefore not evidence that the
    exclusion boundary still fits live markup; the W2-13 gate report has to count it."""
    document = scoped(_ARCHIVED.read_bytes())
    assert document.zones_unmatched == (".b-similar", ".broker")


# ------------------------------------------------------------------ absent marker

def test_a_page_without_the_sentence_declares_nothing() -> None:
    """The negative control for the `body` scope: `html_marker` fires on the map config's
    `infoText` too (both occurrences ARE the disclaimer), so the only way to show the reader
    is not simply always-true is a page carrying neither."""
    body = page(json.dumps([feature(NATIVE, 15.31331632, 50.74437214)]),
                info="Na mapě zobrazujeme jen nemovitosti s přesnou adresou.",
                disclaimer=False)
    assert read("id.det.no_exact_disclaimer", scoped(body)) == []


def test_the_marker_fires_off_the_map_config_alone() -> None:
    """Documented behaviour of the `body` scope, asserted rather than assumed: with the
    visible paragraph gone the sentence still reaches the page inside `infoText`, and the
    claim is right either way — but it does NOT prove the sentence was rendered to a human.
    Narrowing the selector needs an archived page carrying the blurred case, and this repo
    holds none."""
    body = page(json.dumps([feature(NATIVE, 15.31331632, 50.74437214)]),
                disclaimer=False)
    claim = one_claim("id.det.no_exact_disclaimer", scoped(body))
    assert claim.value_text == "no_exact_address"
    assert claim.blur_evidence == "declared"
