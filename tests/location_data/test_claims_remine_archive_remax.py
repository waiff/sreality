"""remax@3 — the W2-6 activation, entry by entry, over the REAL archived body.

The canon suite (`test_archive_reader_canon.py`) proves what each READER does; this file
proves what remax's own CONTRACT says, by running the entries `contracts/portals/remax.yaml`
actually ships — not hand-built lookalikes — through the real scoper and the real C6 licence
ladder. The two are different questions, and the second is the one a selector typo, a dropped
`position_branch` or a re-pointed entry id breaks.

The substrate is `tests/fixtures/portal_html/remax_detail.html`, a genuinely archived page.
The pinned `tests/fixtures/location_w2/remax_detail.html` is hand-written apart from the h2
block W2-6 copied into it, so a selector that works there proves the shape and not the
population — the golden gate scores that one, and these tests score the real one.

Three entries are activated and all three are asserted here:
  * `rx.det.header_address` — `html_own_text`, because the header nests a `mapa` jump-link.
  * `rx.det.gps`            — `html_point_dms` + `position_branch: portal_pin`, the entry id
                              `ARCHIVED_COORDINATE_RULES["remax"]` names.
  * `rx.det.map_address`    — `html_attr` on the SUBJECT's map, which reads nothing on any
                              body in this repo. Its correctness is gated here precisely
                              because it contributes no golden row (planting the attribute in
                              a fixture to make one would be fabricating a claim).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any

import pytest

from location_data import claims_remine_archive as archive
from location_data import contracts
from location_data.claims_intake import (
    ARCHIVED_COORDINATE_RULES,
    Entry,
    ListingRow,
)
from location_data.claims_remine_archive import (
    ARCHIVE_READERS,
    ArchivedPayload,
    _licensed_coordinate,
    stamp_archive_claim,
)
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from scraper.remax_parser import parse_dms_pair

_ROOT = Path(__file__).resolve().parent.parent.parent
_ARCHIVED_BODY = _ROOT / "tests" / "fixtures" / "portal_html" / "remax_detail.html"
_PINNED_BODY = _ROOT / "tests" / "fixtures" / "location_w2" / "remax_detail.html"

FETCHED_AT = datetime(2026, 8, 13, 4, 30, tzinfo=UTC)
CONTRACT = {c.source: c for c in contracts.load_all()}["remax"]

# The five neighbour cards on the archived page. The exclusion register strips them, and
# `rx.det.map_address` must be unable to reach any of them — that is the whole reason the
# entry is scoped to two element ids instead of reading `[data-address]`.
CAROUSEL_ADDRESSES = (
    "Oleška, okres Praha-východ",
    "Velké Popovice, okres Praha-východ",
    "Havlíčkova, Stará Boleslav, Brandýs nad Labem-Stará Boleslav, okres Praha-východ",
    "Ořechová, Ondřejov, okres Praha-východ",
    "náměstí Smiřických, Kostelec nad Černými lesy, okres Praha-východ",
)


# ------------------------------------------------------------------ harness

def entry_named(entry_id: str) -> Entry:
    """The SHIPPED entry, projected as the deploy projects it (claim_intake_fixtures does
    the same for W1). A test that built its own locator would go green against a contract
    that names the wrong selector."""
    from tests.location_data import claim_intake_fixtures as fx
    for item in fx.entries_for("remax"):
        if item.entry_id == entry_id:
            return item
    raise AssertionError(f"remax@{CONTRACT.version} declares no entry {entry_id!r}")


def listing_row(native: str = "fixture", **overrides: Any) -> ListingRow:
    kwargs: dict[str, Any] = {
        "listing_id": 4242, "source": "remax", "source_id_native": native, "raw_json": {},
        "lat": None, "lon": None, "observed_at": FETCHED_AT, "in_mapy_inventory": False,
        "legacy_columns": dict(archive._DUMMY_LEGACY_COLUMNS),
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


def archived() -> ScopedDocument:
    return scoped(_ARCHIVED_BODY.read_bytes())


def run_entry(
    entry: Entry, document: ScopedDocument, *, row: ListingRow | None = None,
) -> list[archive.ArchiveRead]:
    return ARCHIVE_READERS[str(entry.reader)](
        entry, row or listing_row(), payload(), document)


def only(reads: list[archive.ArchiveRead]) -> archive.ArchiveRead:
    assert len(reads) == 1, f"expected exactly one read, got {len(reads)}"
    return reads[0]


def span_text(document: ScopedDocument, claim: Any) -> str | None:
    if claim.span_start is None or claim.span_end is None:
        return None
    return document.html[claim.span_start:claim.span_end]


# --------------------------------------------------------- what v3 turned on, and only that

def test_the_bump_activates_exactly_three_detail_entries():
    """The census of the activation. An entry gaining a reader is what re-inserts the
    portal's claim corpus (`location_claim_fingerprint` hashes `contract_entry_id`), so
    which entries execute is a decision, not an implementation detail — and the two index
    entries staying inert is the structural finding that an index payload is archived under
    the index page's key and can never join a listing."""
    assert CONTRACT.version == 3
    executable = {e.entry_id: e.locator.get("reader") for e in CONTRACT.entries
                  if e.locator.get("reader") in ARCHIVE_READERS}
    assert executable == {
        "rx.det.header_address": "html_own_text",
        "rx.det.gps": "html_point_dms",
        "rx.det.map_address": "html_attr",
    }
    inert = {e.entry_id for e in CONTRACT.entries
             if e.surface == "html_selector" and not e.locator.get("reader")}
    assert inert == {"rx.idx.display_address", "rx.idx.gps", "rx.det.breadcrumbs",
                     "rx.det.location_line", "rx.det.h1_tail", "rx.det.params_umisteni"}


def test_the_contract_ships_shadowed():
    """W2 sequencing: the seven DOM contracts activate SHADOWED. `shadow` is header-grain
    and `project()` deactivates the previous version, so remax's four already-live W1
    entries go dark with it — a freeze, ruled for deliberately, and reversed by
    `python -m location_data.contracts --unshadow remax@3`, never by editing the YAML of a
    version already projected."""
    assert CONTRACT.shadow is True


def test_the_coordinate_entry_keeps_the_id_the_licence_ladder_names():
    """`ARCHIVED_COORDINATE_RULES` keys the licence on the ENTRY ID. Renaming `rx.det.gps`
    would not fail a selector test — it would silently refuse every remax coordinate as
    `unrecognised_archived_coordinate_locator` and write one absence per row."""
    rule = ARCHIVED_COORDINATE_RULES["remax"]
    assert rule.entry_id == "rx.det.gps"
    assert rule.licence_class == "portal"
    assert {e.entry_id for e in CONTRACT.entries} >= {rule.entry_id}


# ------------------------------------------------------------ rx.det.header_address

def test_the_header_entry_reads_the_subject_line_without_the_jump_link():
    """The defect this activation exists to avoid, asserted through the shipped entry. On
    12/12 mined pages `h2.pd-header__address` nests `<a …>mapa <i></i></a>` and breaks the
    line across source lines, so a deep read states the address as
    "ulice Pod Slovany,<15 tabs>Úvaly mapa"."""
    document = archived()
    claim = only(run_entry(entry_named("rx.det.header_address"), document)).claim
    assert claim.value_text == "ulice Pod Slovany, Úvaly"
    assert "mapa" not in claim.value_text
    assert "\t" not in claim.value_text and "\n" not in claim.value_text


def test_the_header_entrys_evidence_resolves_to_the_uncollapsed_source():
    """The quote is the COLLAPSED value and the span indexes the UNCOLLAPSED source, so the
    two lengths differ on purpose (24 vs 39 here). `_span_pattern` matches a whitespace run
    entity- and NBSP-tolerantly, which is why no `node.html` fallback is needed — an
    unnecessarily wide quote would be a worse span, not a safer one."""
    document = archived()
    claim = only(run_entry(entry_named("rx.det.header_address"), document)).claim
    assert claim.evidence_quote == "ulice Pod Slovany, Úvaly"
    assert claim.span_start is not None and claim.span_end is not None
    quoted = span_text(document, claim)
    assert quoted.startswith("ulice Pod Slovany,") and quoted.endswith("Úvaly")
    assert len(quoted) > len(claim.evidence_quote)


def test_the_header_entry_beats_a_deep_read_of_its_own_selector():
    """A negative control on the contract's own css. If remax ever drops the jump-link this
    says the reader stopped being load-bearing, rather than the two silently agreeing."""
    document = archived()
    entry = entry_named("rx.det.header_address")
    deep = only(ARCHIVE_READERS["html_text"](
        entry, listing_row(), payload(), document)).claim
    own = only(run_entry(entry, document)).claim
    assert "mapa" in deep.value_text and "mapa" not in own.value_text


def test_the_header_entry_captures_the_non_ulice_form_too():
    """7 of the 12 mined pages carry `<Obec> – část obce <X>` / `, okres <X>` rather than
    `ulice <Street>`, and both forms are the SAME element — which is why
    `rx.det.location_line` describes this string rather than a second one. The single
    archived body can only show the `ulice` form, so the other one is built here."""
    body = ('<html><body><div class="pd-header"><h2 class="pd-header__address">\n'
            '\t\t\tBílovec – část obce Ohrada '
            '<a href="#" data-scroll-to-anchor="#map" class="link link--ar">mapa '
            '<i class="icon-arrow-right"></i></a>\n\t\t</h2></div></body></html>')
    document = scoped(body)
    claim = only(run_entry(entry_named("rx.det.header_address"), document)).claim
    assert claim.value_text == "Bílovec – část obce Ohrada"
    assert claim.span_start is not None


def test_the_header_entry_is_admissible_to_survivorship():
    """`subject_scoped: true` is what separates this entry from
    `rx.det.raw_address_conflict`, which states the carousel's line with
    `subject_scoped: false` so S7 can never rank it (03 §3.2 rule 4)."""
    claim = only(run_entry(entry_named("rx.det.header_address"), archived())).claim
    conflict = next(e for e in CONTRACT.entries
                    if e.entry_id == "rx.det.raw_address_conflict")
    assert claim.subject_scoped is True
    assert conflict.subject_scope["subject_scoped"] is False


# ------------------------------------------------------------------- rx.det.gps

def test_the_pin_entry_is_licensed_portal_from_its_own_declared_branch():
    """The ladder, applied exactly as the lane applies it. `position_branch: portal_pin` is
    contract DATA — the reader declares the branch, the ladder stamps the class, and the
    reader's own `licence_class` is discarded on the way through."""
    document = archived()
    entry = entry_named("rx.det.gps")
    read = only(run_entry(entry, document))
    assert read.position_branch == "portal_pin"
    stamped = stamp_archive_claim(read.claim, payload(),
                                  scope_version=document.scope_version)
    licensed, reason = _licensed_coordinate(stamped, listing_row(), entry,
                                            read.position_branch)
    assert licensed is not None
    assert licensed.licence_class == "portal"
    assert reason == "archived_rx.det.gps"


def test_the_pins_geometry_round_trips_the_portals_own_dms_string():
    """The WKT is not a second parse of the page: it is `parse_dms_pair` on the same raw
    attribute the claim quotes, so a drift in the reader's arithmetic shows up here rather
    than as a plausible pin a few hundred metres away."""
    document = archived()
    read = only(run_entry(entry_named("rx.det.gps"), document))
    lat, lon = parse_dms_pair(read.claim.value_text)
    assert lat is not None and lon is not None
    assert read.claim.value_geom_wkt == archive.point_wkt(lat, lon)
    assert read.claim.span_start is not None
    # The quote is the DECODED attribute value and the span indexes the serialised source,
    # where the seconds mark is `&quot;` — `_span_pattern`'s entity tolerance is what ties
    # the two together, so the round-trip is asserted through `unescape`, not by equality.
    assert unescape(span_text(document, read.claim)) == read.claim.evidence_quote


def test_the_mapy_inventory_veto_outranks_the_pin():
    """§6.4's gate joins on `listing_id`, not on `surface` — re-reading the same position
    out of an archived page is the same position, so inventory membership refuses it here
    exactly as it refuses the payload copy."""
    document = archived()
    entry = entry_named("rx.det.gps")
    read = only(run_entry(entry, document))
    row = listing_row(in_mapy_inventory=True)
    stamped = stamp_archive_claim(read.claim, payload(),
                                  scope_version=document.scope_version)
    licensed, reason = _licensed_coordinate(stamped, row, entry, read.position_branch)
    assert licensed is None
    assert reason == "listing_in_mapy_affected_inventory"


def test_an_index_coordinate_would_be_refused_by_the_ladder():
    """`rx.idx.gps` stays inert, and the note says one of the three reasons is the ladder:
    its id is not the one `ARCHIVED_COORDINATE_RULES` names. Asserted rather than asserted
    in prose, so activating it later cannot be a quiet decision."""
    entry = entry_named("rx.idx.gps")
    document = archived()
    read = only(run_entry(entry_named("rx.det.gps"), document))
    stamped = stamp_archive_claim(read.claim, payload(),
                                  scope_version=document.scope_version)
    licensed, reason = _licensed_coordinate(stamped, listing_row(), entry, "portal_pin")
    assert licensed is None
    assert reason == "unrecognised_archived_coordinate_locator"


def test_a_pin_outside_the_cz_envelope_is_dropped_by_the_reader_itself():
    """v3 drops `guards: [reject_outside_cz_bbox]` because `html_point_dms` never evaluates
    guards — the envelope is INTRINSIC to `parse_dms_pair`, which returns (None, None)
    outside it. This is the test that the removed declaration removed nothing."""
    body = ('<html><body><div id="printMap" data-gps='
            '"48°51\'29.6&quot;N,2°17\'40.2&quot;E"></div></body></html>')
    assert run_entry(entry_named("rx.det.gps"), scoped(body)) == []


def test_the_restated_pin_entry_declares_no_transform_and_no_guard():
    """Both declarations were inert (`dms_to_decimal` was never implemented; the reader
    consults neither axis), and keeping either would now be a projection-time
    ContractError. The removal is of a declaration, never of a check."""
    entry = next(e for e in CONTRACT.entries if e.entry_id == "rx.det.gps")
    assert entry.transform == [] and entry.guards == []
    assert entry.locator["position_branch"] == "portal_pin"


# --------------------------------------------------------------- rx.det.map_address

def test_the_subject_map_entry_reads_nothing_on_any_body_in_this_repo():
    """MEASURED ABSENCE, and the expected steady state: `#printMap`/`#listingMap` carry
    `data-gps` and no `data-address` on the archived body, on the pinned body, and on both
    live recon samples (recon §5.7). `required: when_present`, so the entry claims nothing
    until the portal renders it."""
    entry = entry_named("rx.det.map_address")
    assert run_entry(entry, archived()) == []
    assert run_entry(entry, scoped(_PINNED_BODY.read_bytes())) == []


def test_the_subject_map_entry_can_never_reach_a_carousel_card():
    """The whole point of the entry, and the only place it is gated. The decoy is
    unreachable twice over: the register strips every `.area-listings__item[data-address]`
    before the reader sees the tree, and the selector names two element ids anyway."""
    document = archived()
    for value in CAROUSEL_ADDRESSES:
        assert document.contains(value) is False, value
    assert document.css("[data-address]") == []


def test_the_subject_map_entry_reads_the_subject_the_day_the_portal_renders_it():
    """The ruled shape, proven without fabricating a claim in the golden. The subject map
    carries the attribute; a carousel card carries a different one; the read is the
    subject's, with a span that resolves."""
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
    assert claim.value_text == "Roháčova, Praha 3 - Žižkov, Praha,"
    assert claim.subject_scoped is True
    assert claim.span_start is not None
    assert span_text(document, claim) == claim.evidence_quote
    assert document.contains(
        "V Horní Stromce, Praha 3, Vinohrady, okres Hlavní město Praha") is False


def test_the_subject_map_entry_ignores_the_neighbourhood_map():
    """`#areaMap` is the neighbourhood map, not the subject's. It is excluded by id rather
    than by a zone, which is why an id-scoped selector is the narrowest expressible form of
    02 §2.2.6's permission to read `data-address` outside `.area-listings__item`."""
    body = ('<html><body><div id="areaMap" '
            'data-address="Úvaly, okres Praha-východ"></div></body></html>')
    assert run_entry(entry_named("rx.det.map_address"), scoped(body)) == []


# --------------------------------------------------------------- the inert declarations

@pytest.mark.parametrize("entry_id, css", [
    ("rx.det.breadcrumbs", ".breadcrumbs a"),
    ("rx.det.location_line", ".pd-header__location"),
])
def test_v3_corrects_what_the_archived_body_measures_about_the_inert_selectors(
    entry_id: str, css: str,
) -> None:
    """Two findings v3 records rather than acts on. `.breadcrumb a` matched 0 nodes and
    `.breadcrumbs a` matches 5, so the selector is corrected even though the entry stays
    inert — a selector known to be wrong must not be what a later wave activates. And
    `.pd-header__location` matches 0 nodes on real markup: the string it describes is the
    non-`ulice` form of the h2 `rx.det.header_address` already reads."""
    entry = next(e for e in CONTRACT.entries if e.entry_id == entry_id)
    assert entry.locator["css"] == css
    assert entry.locator.get("reader") is None


def test_the_measured_node_counts_behind_those_two_corrections():
    document = archived()
    assert document.css(".breadcrumb a") == []
    assert len(document.css(".breadcrumbs a")) == 5
    assert document.css(".pd-header__location") == []


# ----------------------------------------------------- the captured regression body

def test_the_captured_control_listing_is_a_real_body_and_carries_no_pii():
    """437234 is the operator's control (the pre-W2 resolver said 'Bukovická 297' and a
    human confirmed it). Its body under `regressions/remax/` was CAPTURED from the live
    page through `scraper.remax_parser.parse_detail`, not written by hand — and the two
    keys that parse produces which have no business in a committed fixture (`broker`, an
    agent's name and e-mail; `image_urls`) are removed rather than blanked."""
    doc = json.loads(
        (_ROOT / "tests" / "fixtures" / "location_w2" / "regressions" / "remax"
         / "437234.json").read_text(encoding="utf-8"))
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
    doc = json.loads(
        (_ROOT / "tests" / "fixtures" / "location_w2" / "regressions" / "remax"
         / "437234.json").read_text(encoding="utf-8"))
    raw = doc["raw_json"]
    assert raw["carousel_address"] == "Petrov nad Desnou, okres Šumperk"
    assert raw["carousel_address"] != raw["display_address"]
    # And the banned key is simply GONE from a post-W0-0d payload, which is why
    # `rx.det.raw_address_conflict` (locator `/address`) claims nothing on this body: the
    # conflict signal is fed by rows drained before the rename, not by new ones.
    assert "address" not in raw
