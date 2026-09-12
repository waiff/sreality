"""ceskereality@6 — the slim contract, run against this portal's REAL archived bodies.

Rule 25: one lane, eleven claim types, at most one entry each, the TOWN entry mandatory and
live. This file is the portal's whole gate — the version, the entry-id set, the one-per-type
property, the town entry and its reader, one extraction assertion per entry, and the PII
rails `test_portal_ceskereality.py` carried before W1-c replaced it (W1-c R14).

The bodies:

  * `location_w2a_refetch/ceskereality_b1.html` — listing 3861311, Ostrov / Májová 843 /
    okres Karlovy Vary. A STREET-TIER page: all five detail entries fire.
  * `location_w2a_refetch/ceskereality_a1.html` — listing 3680359, Špindlerův Mlýn /
    okres Trutnov. A TOWN-TIER page: the address line is a bare town, so the street and the
    house number are silent — the portal DECLARING granularity, not a parse failure.
  * `location_w2/ceskereality_detail.html` — the modelled body the golden gate scores.
  * `location_w2/ceskereality_map.html` — a modelled `page_kind: map` body for the one
    entry that reads the /mapa/ marker JSON. The repo holds no capture of that surface; the
    fixture's own header records what is copied from the real bodies and what is modelled.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from location_data import contracts
from location_data.claims_common import apply_transforms
from location_data.claims_intake import (
    ARCHIVED_COORDINATE_RULES,
    Claim,
    Entry,
    extract_listing,
)
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from location_data.page_readers import (
    PAGE_READERS,
    ArchivedPayload,
    IntakeResult,
    extract_page,
    page_entries,
)
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
_REFETCH = _ROOT / "tests" / "fixtures" / "location_w2a_refetch"
_PINNED = _ROOT / "tests" / "fixtures" / "location_w2"

SOURCE = "ceskereality"
VERSION = 6
CONTRACT = {c.source: c for c in contracts.load_all()}[SOURCE]

# The lane copies the payload's `first_observed_at` onto every claim, so a wall clock here
# would make the assertions depend on when the suite ran.
OBSERVED_AT = datetime(2026, 1, 1, tzinfo=UTC)

# The TOWN entry. A constant rather than a literal spelled five times, because rule 25 makes
# this one entry mandatory: a rename would otherwise read as an ordinary set change.
TOWN_ENTRY = "cr.det.data_city"
TOWN_READER = "html_attr"

# The whole contract, in file order. Asserted as a mapping rather than assumed: an entry
# silently joining, leaving or changing type is the failure a per-portal file exists to catch.
ENTRIES: dict[str, str] = {
    "cr.det.data_city": "obec_name",
    "cr.det.city_okres": "okres_name",
    "cr.det.page_pin": "coordinate",
    "cr.det.address_street": "street_name",
    "cr.det.address_cp": "house_number_cp",
    "cr.map.exact": "precision_declaration",
}
DETAIL_ENTRIES = tuple(k for k in ENTRIES if k.startswith("cr.det."))

BODIES: dict[str, tuple[Path, str]] = {
    "3861311": (_REFETCH / "ceskereality_b1.html", "detail"),
    "3680359": (_REFETCH / "ceskereality_a1.html", "detail"),
    "3680359-a2": (_REFETCH / "ceskereality_a2.html", "detail"),
    "fixture": (_PINNED / f"{SOURCE}_detail.html", "detail"),
}
MAP_BODY = _PINNED / f"{SOURCE}_map.html"


def entries() -> list[Entry]:
    return fx.entries_for(SOURCE)


def register(*, with_carousel_zone: bool = True) -> ScopeRegister:
    zones = CONTRACT.exclusion_zones
    if not with_carousel_zone:
        zones = [z for z in zones
                 if "s-estates-slide" not in str(z.get("locator", {}).get("css", ""))]
    return ScopeRegister.from_zones(SOURCE, zones)


def document(key: str, *, with_carousel_zone: bool = True) -> ScopedDocument:
    return scope_html(BODIES[key][0].read_bytes(),
                      register=register(with_carousel_zone=with_carousel_zone))


def mined(key: str, *, source_body: bytes | None = None,
          native: str | None = None) -> IntakeResult:
    """One archived body through the real lane — the same call the drain makes."""
    path, page_kind = BODIES[key]
    raw = path.read_bytes() if source_body is None else source_body
    listing_native = native or key.split("-")[0]
    payload = ArchivedPayload(
        id=1, source=SOURCE, source_id_native=listing_native, page_kind=page_kind,
        payload_sha256="0" * 64, first_observed_at=OBSERVED_AT, body=raw)
    return extract_page(payload, fx.listing(SOURCE, {}, native=listing_native), entries(),
                        register=register())


def mined_map(native: str) -> IntakeResult:
    payload = ArchivedPayload(
        id=2, source=SOURCE, source_id_native=native, page_kind="map",
        payload_sha256="0" * 64, first_observed_at=OBSERVED_AT,
        body=MAP_BODY.read_bytes())
    return extract_page(payload, fx.listing(SOURCE, {}, native=native), entries(),
                        register=register())


def by_id(result: IntakeResult) -> dict[str, Claim]:
    return {claim.extractor_id: claim for claim in result.claims}


# ------------------------------------------------------ the shape rule 25 asks for

def test_the_contract_is_at_version_6_and_declares_exactly_these_six_entries() -> None:
    assert CONTRACT.version == VERSION
    assert {e.entry_id: e.claim_type for e in CONTRACT.entries} == ENTRIES


def test_every_claim_type_is_declared_at_most_once() -> None:
    """Two entries of one type make the contract a vote the survivorship policy never asked
    for — the shape v5 had (three `street_name` locators, two `obec_name` ones), and the
    reason nobody could say which reader a claim came from."""
    repeated = [t for t, n in Counter(e.claim_type for e in CONTRACT.entries).items() if n > 1]
    assert repeated == []


def test_the_town_entry_is_present_live_and_names_its_reader() -> None:
    """The mandatory one. `location_town_coverage` is red until every active Czech listing
    has a town, and this entry is where this portal enters the claims spine. The
    `statutory_city_obec` chain is R4: RÚIAN has no obec called "Praha 8", so a town claim
    carrying an obvod resolves to nothing at all — a coverage hole that reads as a portal
    publishing no town. Which Prague spellings the chain actually folds is NOT asserted here
    (no committed body carries one): it is measured, gap included, in
    `test_the_declared_town_chain_over_every_data_city_form_this_portal_emits`."""
    town = {e.entry_id: e for e in CONTRACT.entries}[TOWN_ENTRY]
    assert town.claim_type == "obec_name"
    assert town.reader == TOWN_READER and town.reader in PAGE_READERS
    assert town.page_kind == "detail"
    assert town.locator["css"] == "input#driving_calculator_from"
    assert town.locator["attr"] == "data-city"
    assert town.transform == ["split_paren_okres", "statutory_city_obec"]


# Every `data-city` spelling this portal is known to write, with where the repo proves it,
# and what the DECLARED chain (`split_paren_okres` -> `statutory_city_obec`) makes of it.
# `folded` says whether the value that leaves the chain is an obec RÚIAN can bind.
DATA_CITY_FORMS: tuple[tuple[str, str, str, bool], ...] = (
    ("České Budějovice (okres České Budějovice)", "České Budějovice",
     "location_w2/ceskereality_detail.html", True),
    ("Ostrov (okres Karlovy Vary)", "Ostrov",
     "location_w2a_refetch/ceskereality_b1.html", True),
    ("Špindlerův Mlýn (okres Trutnov)", "Špindlerův Mlýn",
     "location_w2a_refetch/ceskereality_a1.html + a2", True),
    # R4's own worked example, wrapped in this portal's `(okres …)` envelope. No committed
    # ceskereality body carries a Prague row, so this is modelled on the ruling, not observed.
    ("Praha 13 (okres Hlavní město Praha)", "Praha", "modelled on R4", True),
    # THE RECORDED GAP. `claim_intake_fixtures.CESKEREALITY_PAGE` (listing 3849899) is the one
    # Prague artefact this repo commits and it spells the pair SPACE-GLUED — "Praha Stodůlky"
    # — which is neither of the two shapes `statutory_city_obec` folds (a number / Roman
    # numeral, or a hyphen in one of five cities that do not include Praha). The chain leaves
    # it whole, so the town claim is a non-obec string. Pinned, not asserted away: closing it
    # takes a shared transform or a gazetteer, both out of this wave.
    ("Praha Stodůlky (okres Hlavní město Praha)", "Praha Stodůlky",
     "claim_intake_fixtures.CESKEREALITY_PAGE, space-glued", False),
    # The same gap on the hyphenated spelling, for the same reason.
    ("Praha 5-Smíchov (okres Hlavní město Praha)", "Praha 5-Smíchov",
     "modelled on the gap above", False),
)


@pytest.mark.parametrize(("raw", "town", "provenance", "folded"), DATA_CITY_FORMS)
def test_the_declared_town_chain_over_every_data_city_form_this_portal_emits(
        raw: str, town: str, provenance: str, folded: bool) -> None:
    """R4 measured rather than claimed. The chain this entry declares is run over each
    spelling, and the rows with `folded=False` are the residual: a value that leaves the
    chain still carrying a Prague quarter is not an obec, so it binds to nothing — the same
    coverage hole R4 exists to close, surviving on the one spelling this portal's own
    committed payload proves. It fails the moment the fold widens, which is when this table
    (and the report's gap paragraph) must be re-typed."""
    entry = {e.entry_id: e for e in CONTRACT.entries}[TOWN_ENTRY]
    assert apply_transforms(raw, tuple(entry.transform)) == town, provenance
    # `folded` is about BINDABILITY, not about whether the string moved — on a plain town the
    # fold is a no-op and the value was an obec already. The only rows that can leave the
    # chain unbindable are the statutory-city ones, so that is what the flag is checked on.
    assert ((town == "Praha") if raw.startswith("Praha ") else True) is folded, provenance


def test_the_gap_rows_are_the_only_unfolded_ones_and_none_is_a_committed_body() -> None:
    """The table above is only honest while its `folded=False` rows are exactly the Prague
    quarters — a captured body drifting into that half would mean this portal stopped stating
    a town, which is rule 25's red line and not a row to quietly add here."""
    unfolded = {raw for raw, _, _, folded in DATA_CITY_FORMS if not folded}
    assert all(raw.startswith("Praha ") for raw in unfolded)
    for key in BODIES:
        attr = document(key).css_first("#driving_calculator_from").attributes["data-city"]
        assert attr not in unfolded, key


def test_no_entry_is_readerless_and_none_reads_a_legacy_column() -> None:
    """Two rules at once. A readerless entry executes nowhere, so it is documentation the
    loader charges a hash for; a `legacy_column` entry reads `listings` columns this sprint
    deletes. Every entry here reads a stored page body."""
    for entry in CONTRACT.entries:
        assert entry.reader in PAGE_READERS, entry.entry_id
        assert entry.surface != "legacy_column", entry.entry_id
        assert entry.extraction_method != "legacy_column", entry.entry_id
        assert "legacy_source_column" not in entry.locator, entry.entry_id
    assert [e.entry_id for e in page_entries(entries(), "detail")] == list(DETAIL_ENTRIES)
    assert [e.entry_id for e in page_entries(entries(), "map")] == ["cr.map.exact"]


def test_every_declared_transform_and_guard_is_one_the_extractor_implements() -> None:
    for entry in CONTRACT.entries:
        for name in entry.transform:
            assert name.partition(":")[0] in contracts.IMPLEMENTED_TRANSFORMS, name
        for guard in entry.guards:
            assert guard in contracts.IMPLEMENTED_GUARDS, guard


# ------------------------------------------- one extraction assertion per entry

def test_a_street_tier_body_yields_all_five_detail_claims() -> None:
    """Listing 3861311, the whole detail contract off one element: the driving-calculator
    input stamps the town, its declared okres, the portal's own pin and the subject's
    address line, and the page's map embed echoes that same pair as
    `q=50.31081,12.953306388889`."""
    claims = by_id(mined("3861311"))
    assert set(claims) == set(DETAIL_ENTRIES)
    assert claims["cr.det.data_city"].value_text == "Ostrov"
    assert claims["cr.det.city_okres"].value_text == "Karlovy Vary"
    assert claims["cr.det.address_street"].value_text == "Májová" != "Majova"
    assert claims["cr.det.address_cp"].value_text == "843"
    assert claims["cr.det.page_pin"].value_geom_wkt == "POINT(12.953306388889 50.31081)"
    for claim in claims.values():
        assert claim.surface == "archived_html"
        assert claim.page_kind == "detail"
        assert claim.licence_class == "portal"
        assert claim.blur_evidence == "none"
        assert claim.subject_scoped is True


def test_a_town_tier_body_yields_the_town_the_okres_and_the_pin_but_no_street() -> None:
    """Listing 3680359. `Špindlerův Mlýn Bedřichov` is a bare town line — ONE comma segment,
    so the shared street tests refuse to call its leading token a street and the house number
    is gated on the same tests. That silence IS this portal's granularity declaration, which
    is why it is asserted rather than tolerated."""
    claims = by_id(mined("3680359"))
    assert set(claims) == {"cr.det.data_city", "cr.det.city_okres", "cr.det.page_pin"}
    assert claims["cr.det.data_city"].value_text == "Špindlerův Mlýn"
    assert claims["cr.det.city_okres"].value_text == "Trutnov"
    assert claims["cr.det.page_pin"].value_geom_wkt == (
        "POINT(15.597127147728 50.727776839866)")


def test_the_quarter_glued_to_the_town_is_never_claimed_as_the_town() -> None:
    """`value` is `Špindlerův Mlýn Bedřichov` — the obec and its část obce joined by a SPACE,
    not a comma. `data-city` is the town's own carrier and states `Špindlerův Mlýn` alone, so
    the town claim is unpolluted; the quarter is claimed by no entry (no shared transform can
    split a space-glued pair, and a gazetteer split would be a per-portal reader)."""
    claims = by_id(mined("3680359"))
    assert claims["cr.det.data_city"].value_text == "Špindlerův Mlýn"
    assert "Bedřichov" not in {c.value_text for c in mined("3680359").claims}


@pytest.mark.parametrize("key", ["3680359", "3680359-a2"])
def test_both_refetch_rounds_of_one_listing_claim_the_same_facts(key: str) -> None:
    """a1 and a2 are two fetches of listing 3680359 minutes apart: the churn the archive
    measures is page chrome, never location. A claim that moved between them would mean the
    contract reads a volatile node."""
    claims = by_id(mined(key))
    assert {i: c.value_text for i, c in claims.items()} == {
        "cr.det.data_city": "Špindlerův Mlýn",
        "cr.det.city_okres": "Trutnov",
        "cr.det.page_pin": "50.727776839866,15.597127147728",
    }


def test_the_pinned_fixture_the_golden_scores_yields_every_detail_entry() -> None:
    """The permanent gate's non-vacuity floor: scoring the town alone would leave the okres,
    the street, the čp and the pin resting on this file, so a future extractor change could
    move them with the golden green."""
    claims = by_id(mined("fixture"))
    assert set(claims) == set(DETAIL_ENTRIES)
    assert claims["cr.det.data_city"].value_text == "České Budějovice"
    assert claims["cr.det.city_okres"].value_text == "České Budějovice"
    assert claims["cr.det.address_street"].value_text == "Nádražní"
    assert claims["cr.det.address_cp"].value_text == "1067"
    assert claims["cr.det.page_pin"].value_geom_wkt == "POINT(14.4744 48.9745)"


def test_the_modelled_fixtures_pin_is_the_pair_its_own_map_embed_carries() -> None:
    """The modelled body states the pin twice, exactly as the captured ones do — the input's
    decimal attributes and the Google embed's `q=lat,lng` — so the two facts on the page
    cannot drift apart under a later edit."""
    body = BODIES["fixture"][0].read_text(encoding="utf-8")
    assert 'data-coord-lat="48.9745" data-coord-lng="14.4744"' in body
    assert "q=48.9745,14.4744" in body


def test_the_pin_is_licensed_as_this_portals_own_rather_than_stamped_by_its_reader() -> None:
    """C6: the reader declares the BRANCH (`position_branch: portal_pin`) and the ladder
    stamps the class. ceskereality's row in `ARCHIVED_COORDINATE_RULES` names this entry and
    only this entry, so a second coordinate locator could not smuggle a class in. The row is
    W1-c R10's integrator half — without it the ladder refuses every pin this portal has."""
    rule = ARCHIVED_COORDINATE_RULES[SOURCE]
    assert rule.entry_id == "cr.det.page_pin" and rule.licence_class == "portal"
    pin = {e.entry_id: e for e in CONTRACT.entries}["cr.det.page_pin"]
    assert pin.locator["position_branch"] == "portal_pin"
    assert pin.locator["attr"] == ["data-coord-lat", "data-coord-lng"]
    assert pin.precision_map["precision_cap"]["granularity_max"] == "address_point"
    assert pin.guards == ["reject_outside_cz_bbox"]


# --------------------------------------------------- the map surface's precision flag

def test_the_map_marker_flag_is_read_per_branch_and_only_the_false_one_declares_blur() -> None:
    """R5 + R10: the portal's own `exact` flag IS the precision declaration, and the LABEL is
    the value — `stamp_page_claim` stamps `declared_precision_label` for this claim type
    whatever the reader, so a boolean carries the signal without a bespoke reader. Which
    label means blurred is contract calibration (`blurred_labels: [exact_false]`), never a
    code constant: on one 500-marker request 436 markers were exact and 64 were not."""
    exact = by_id(mined_map("3861311"))["cr.map.exact"]
    assert exact.value_text == "exact_true" and exact.value_num == 1.0
    assert exact.declared_precision_label == "exact_true"
    assert exact.blur_evidence == "none"
    assert exact.page_kind == "map"

    blurred = by_id(mined_map("3680359"))["cr.map.exact"]
    assert blurred.value_text == "exact_false" and blurred.value_num == 0.0
    assert blurred.declared_precision_label == "exact_false"
    assert blurred.blur_evidence == "declared"


def test_a_marker_set_without_this_listing_is_a_counted_miss_not_a_neighbours_flag() -> None:
    """`subject_scope: {kind: id_match, on_miss: fail}` over `nid == source_id_native`. One
    /mapa/ response carries up to 500 markers, so "the first marker" would be another
    listing's precision on 499 of them; a miss is counted instead."""
    result = mined_map("9999999")
    assert result.claims == []
    assert dict(result.refusals) == {"subject_not_found:ceskereality": 1}


def test_the_map_flags_evidence_is_broken_three_ways_and_every_way_is_pinned() -> None:
    """A RECORDED DEFECT in full, pinned so each half fails the moment it is fixed — not a
    sanctioned answer. `_json_quote` resolves `/exact` against the WHOLE captured document,
    and this portal's document is a LIST of markers (mmreality, the reader's design case,
    serves one subject per DOM node, where whole-document and subject coincide). Three
    consequences, all measured on the modelled map body:

      1. the quote is the FIRST marker's flag, not the matched subject's;
      2. the span is byte-identical on both branches, so it does not distinguish the subject
         at all — it cannot be read as "the neighbour's span" either;
      3. the span does not slice back to the quote, because it lands in the HTML-escaped
         attribute (`&quot;exact&quot;:true`) while the quote is the DECODED JSON text.

    (3) is the invariant `test_every_claim_cites_a_span_that_slices_back_to_its_own_quote`
    asserts for the detail entries; that test is parametrized over BODIES, all `page_kind:
    detail`, so this entry is outside its reach and would otherwise ship unmeasured. The
    claim's VALUE is the subject's and correct on both branches. Fixing it means `_json_quote`
    handing back an HTML-SCOPED slice of the MATCHED marker — a shared-reader change, and an
    integrator to-do on this contract; re-type this test with it."""
    html = scope_html(MAP_BODY.read_bytes(), register=register()).html
    exact = by_id(mined_map("3861311"))["cr.map.exact"]
    blurred = by_id(mined_map("3680359"))["cr.map.exact"]

    assert (exact.value_text, blurred.value_text) == ("exact_true", "exact_false")
    # 1 — the false branch quotes the true branch's marker.
    assert blurred.evidence_quote == exact.evidence_quote == '"exact":true'
    # 2 — and cites the same bytes for both subjects.
    assert (blurred.span_start, blurred.span_end) == (exact.span_start, exact.span_end)
    # 3 — which are not the quote's bytes: the document is escaped, the quote is not.
    for claim in (exact, blurred):
        assert html[claim.span_start:claim.span_end] != claim.evidence_quote
        assert html[claim.span_start:claim.span_end] == "&quot;exact&quot;:true"


# ------------------------------------------------------------- evidence and spans

@pytest.mark.parametrize("key", sorted(BODIES))
def test_every_claim_cites_a_span_that_slices_back_to_its_own_quote(key: str) -> None:
    """Migration 382's `loc_claim_text_evidence`. A transformed value is a NORMALISED form,
    so each reader quotes the RAW attribute it read from — quoting `Ostrov` resolved the span
    into the node's own `value=` attribute instead of into `data-city`, and a span pointing
    at a different fact is worse than no span."""
    html = document(key).html
    for claim in mined(key).claims:
        assert claim.span_start is not None and claim.span_end > claim.span_start
        assert html[claim.span_start:claim.span_end] == claim.evidence_quote


def test_the_two_reads_of_each_attribute_quote_the_whole_attribute() -> None:
    claims = by_id(mined("3861311"))
    for entry_id in ("cr.det.data_city", "cr.det.city_okres"):
        assert claims[entry_id].evidence_quote == "Ostrov (okres Karlovy Vary)"
    for entry_id in ("cr.det.address_street", "cr.det.address_cp"):
        assert claims[entry_id].evidence_quote == "Májová 843, Ostrov"


# ---------------------------------------------- refusals, zones and the PII rails

@pytest.mark.parametrize("key", sorted(BODIES))
def test_a_real_body_records_no_refusal_at_all(key: str) -> None:
    """A refusal would mean the scoper failed closed or the licence ladder rejected the pin.
    Neither is this portal's state on a detail body: no detail entry is subject-matched, so a
    zero-claim entry on a town-tier page is silence, not a counted miss."""
    assert dict(mined(key).refusals) == {}


@pytest.mark.parametrize("key", sorted(BODIES))
def test_this_contract_bounds_a_real_body_without_a_hole(key: str) -> None:
    """`extract_page` fails CLOSED — an incomplete scope admits NOTHING — so every claim
    above depends on all five zones being APPLICABLE, not merely well-spelled."""
    scoped = document(key)
    assert scoped.is_complete
    assert not scoped.unsupported_selectors and not scoped.strip_failures


def test_the_live_neighbour_carousel_is_out_of_reach_of_every_reader() -> None:
    """The D7 hole v5 closed and this contract inherits. `section.s-estates-slide` carries up
    to 20 OTHER listings' obec+street, and `css_first` on any of these selectors would be one
    node away from claiming one of them."""
    scoped = document("3861311")
    assert dict(scoped.zone_matches)["section.s-estates-slide"] == 1
    for neighbour in ("Štúrova", "Masarykova", "Podobné nemovitosti"):
        assert not scoped.contains(neighbour), neighbour
    assert scoped.contains("Májová") and scoped.contains("Ostrov")


def test_without_that_zone_a_neighbour_street_would_be_reachable() -> None:
    """The negative control: if ceskereality drops the carousel this says the zone stopped
    being load-bearing, instead of the register silently guarding nothing."""
    assert document("3861311", with_carousel_zone=False).contains("Štúrova")


@pytest.mark.parametrize("key", ["3861311", "3680359"])
def test_the_retired_carousel_selectors_matched_nothing_on_either_real_body(key: str) -> None:
    """Kept rather than deleted — they cost nothing and record which markup the block used to
    have — but recorded as measured-dead, so nobody reads the v1 zone list as evidence that
    this carousel was ever excluded on 2026 markup."""
    matches = dict(document(key).zone_matches)
    assert matches[".similar"] == 0 and matches[".podobne"] == 0
    assert matches["select[name*='region']"] == 0
    assert matches["nav a[href*='zahranicni']"] == 0


def test_the_operator_footer_is_the_only_psc_on_the_page_and_it_is_excluded() -> None:
    """[mine-ceskereality]: on 12 of 12 pages the ONLY PSČ and the only street+house-number
    pair belong to ČESKÝ INTERNET s.r.o. That is why this contract declares no `psc` entry —
    and the zone is what stops a future one from claiming the operator's own address."""
    scoped = document("fixture")
    for decoy in ("Kostelní 942/46", "370 04", "Nová 118",
                  "Rudolfovská", "Puklicova", "Zahraniční nemovitosti"):
        assert not scoped.contains(decoy), decoy
    assert "psc" not in {e.claim_type for e in CONTRACT.entries}


def test_the_agency_address_block_is_never_the_subject() -> None:
    """Both captured bodies publish a JSON-LD `streetAddress` — Jaltská 1107/14,
    Seifertova 823/9 — and BOTH are the agency's office, in a different town from the listing
    on b1. No entry reads JSON-LD, and the office block is an exclusion zone besides."""
    for key in ("3861311", "3680359"):
        values = {c.value_text for c in mined(key).claims}
        assert "Jaltská" not in values and "Seifertova" not in values
        assert not values & {"1107/14", "823/9"}


def test_a_body_carrying_none_of_this_portals_locators_claims_nothing() -> None:
    """Silence, not invention. No entry has a positional or best-guess fallback, so a body
    without the driving-calculator input yields zero claims — which is what makes a zero-claim
    sweep a signal the town-coverage tripwire can read."""
    result = mined("3861311", source_body=b"<html><head></head><body></body></html>")
    assert result.claims == [] and dict(result.refusals) == {}


def test_this_portals_raw_json_yields_no_claim_until_a_body_is_stored() -> None:
    """The measured consequence of reading the page instead of the payload. All three
    committed `raw_json` keysets carry `locality_text` — "Praha Stodůlky" on one, NULL on the
    other two — and no entry reads it, so the hourly payload pass mints nothing for this
    portal. The town arrives with the stored detail body, not with the payload; the tripwire
    that must stay green is `location_town_coverage`, not this call."""
    for payload in (fx.CESKEREALITY_PAGE, fx.CESKEREALITY_NULL_LOCALITY,
                    fx.CESKEREALITY_STREET_ONLY):
        result = extract_listing(
            fx.listing(SOURCE, payload, native=str(payload["id"])), entries())
        assert result.claims == []
