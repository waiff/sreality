"""ceskereality@5 — the W2 activation, run against this portal's REAL archived bodies.

`test_archive_reader_canon` proves each canonical reader does what its name says. This file
proves the CONTRACT: the three entries `contracts/portals/ceskereality.yaml` turns on at v5
(`cr.det.title_line`, `cr.det.title_okres`, `cr.det.data_city`), executed exactly as the lane
executes them — the real entries out of the real YAML, through the real exclusion register,
over the two anonymized bodies the W2a refetch probe captured:

  * `ceskereality_b1.html` — listing 3861311, Ostrov / ulice Májová / okres Karlovy Vary.
    A street-tier title.
  * `ceskereality_a1.html` — listing 3680359, Špindlerův Mlýn / okres Trutnov. A title with
    NO street at all, which is this portal's declared-granularity axis rather than a miss.

Nothing here is hand-written markup. The one synthetic string in the file is the negative
control for the v5 exclusion zone, and it is built from the two bodies' own values.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from location_data import contracts
from location_data.claims_intake import (
    SUBSTRATE_ARCHIVED_HTML,
    Entry,
    coordinate_verdict,
)
from location_data.claims_remine_archive import (
    ARCHIVE_READERS,
    ArchivedPayload,
    IntakeResult,
    archive_entries,
    extract_payload,
)
from location_data.html_scope import ScopeRegister, ScopedDocument, scope_html
from tests.location_data import claim_intake_fixtures as fx

_ROOT = Path(__file__).resolve().parents[2]
_REFETCH = _ROOT / "tests" / "fixtures" / "location_w2a_refetch"
_PINNED = _ROOT / "tests" / "fixtures" / "location_w2"

SOURCE = "ceskereality"
CONTRACT = {c.source: c for c in contracts.load_all()}[SOURCE]

# The lane copies the payload's `first_observed_at` onto every claim, so a wall clock here
# would make the assertions depend on when the suite ran.
OBSERVED_AT = datetime(2026, 1, 1, tzinfo=UTC)

# The activation. Three ids, and the set is asserted rather than assumed: an entry silently
# joining or leaving this set is the whole failure mode a per-portal file exists to catch.
ACTIVATED = ("cr.det.data_city", "cr.det.title_line", "cr.det.title_okres")

BODIES = {"3861311": "ceskereality_b1.html", "3680359": "ceskereality_a1.html"}


def entries() -> list[Entry]:
    return fx.entries_for(SOURCE)


def register(*, with_v5_zone: bool = True) -> ScopeRegister:
    zones = CONTRACT.exclusion_zones
    if not with_v5_zone:
        zones = [z for z in zones
                 if "s-estates-slide" not in str(z.get("locator", {}).get("css", ""))]
    return ScopeRegister.from_zones(SOURCE, zones)


def document(native: str, *, with_v5_zone: bool = True) -> ScopedDocument:
    return scope_html(body(native), register=register(with_v5_zone=with_v5_zone))


def body(native: str) -> bytes:
    return (_REFETCH / BODIES[native]).read_bytes()


def mined(native: str, *, source_body: bytes | None = None) -> IntakeResult:
    """One archived body through the real lane — the same call `run_archive_batch` makes."""
    raw = body(native) if source_body is None else source_body
    payload = ArchivedPayload(
        id=1, source=SOURCE, source_id_native=native, page_kind="detail",
        payload_sha256="0" * 64, first_observed_at=OBSERVED_AT, body=raw)
    row = fx.listing(SOURCE, {}, native=native)
    return extract_payload(payload, row, entries(), register=register())


def by_id(result: IntakeResult) -> dict[str, object]:
    return {claim.extractor_id: claim for claim in result.claims}


# ------------------------------------------------------------------ the activation itself

def test_v5_turns_on_exactly_three_detail_entries_and_leaves_the_rest_inert() -> None:
    """The archived lane executes an entry only if it names a reader THIS lane implements,
    is declared for the body's page_kind, and is not a legacy column. Everything else in
    this contract stays declared-ahead: the map surface is a live-endpoint harvest with no
    archived body, `cr.idx.locality` is an index entry the fixture gate cannot score, and
    `cr.det.perex` is permanently typed `street_name` while its live value is a whole
    address line."""
    assert CONTRACT.version == 5
    assert [e.entry_id for e in archive_entries(entries(), "detail")] == list(ACTIVATED)
    inert = {e.entry_id for e in CONTRACT.entries if e.reader is None}
    assert {"cr.det.perex", "cr.map.exact", "cr.map.coordinate", "cr.idx.locality",
            "cr.det.og_title", "cr.det.slug_street"} <= inert


def test_the_activated_entries_name_canonical_readers_and_the_shared_transform() -> None:
    """No portal-private reader and no portal-private normaliser: the accented street and
    the declared okres are two patterns over one node, and the obec is the shared
    `split_paren_okres` half of a string the portal publishes whole."""
    declared = {e.entry_id: e for e in CONTRACT.entries}
    assert declared["cr.det.title_line"].locator["reader"] == "html_regex"
    assert declared["cr.det.title_okres"].locator["reader"] == "html_regex"
    assert declared["cr.det.data_city"].locator["reader"] == "html_attr"
    assert declared["cr.det.data_city"].transform == ["split_paren_okres"]
    for entry_id in ACTIVATED:
        entry = declared[entry_id]
        assert entry.locator["reader"] in ARCHIVE_READERS
        for name in entry.transform:
            assert name in contracts.IMPLEMENTED_TRANSFORMS, name


# ------------------------------------------------------------------ the street-tier body

def test_a_street_tier_body_yields_the_accented_street_the_okres_and_the_obec() -> None:
    """Listing 3861311. The three claims v5 exists for, from the bytes the portal served.

    `Májová` is the point of the whole bump: `listings.street` is ASCII-folded on this portal
    (862 of 40,147 values carry a diacritic), and the `<title>` is where the accent survives.
    """
    result = mined("3861311")
    claims = by_id(result)
    assert set(claims) == set(ACTIVATED)
    assert claims["cr.det.title_line"].value_text == "Májová" != "Majova"
    assert claims["cr.det.title_okres"].value_text == "Karlovy Vary"
    assert claims["cr.det.data_city"].value_text == "Ostrov"
    for claim in claims.values():
        assert claim.surface == "archived_html"
        assert claim.page_kind == "detail"
        assert claim.licence_class == "portal"
        assert claim.blur_evidence == "none"
        assert claim.subject_scoped is True
        assert claim.value_geom_wkt is None


def test_every_claim_off_a_real_body_cites_a_span_that_holds_its_own_quote() -> None:
    """Migration 382's `loc_claim_text_evidence` in advance: a `regex_text` claim without a
    locatable span aborts the batch, so the span is asserted to SLICE BACK to the quote
    rather than merely to exist."""
    document_ = document("3861311")
    for claim in mined("3861311").claims:
        assert claim.span_start is not None and claim.span_end > claim.span_start
        assert document_.html[claim.span_start:claim.span_end] == claim.evidence_quote


def test_the_evidence_quotes_are_the_literals_the_page_states() -> None:
    """Two different quoting rules, both load-bearing on this portal.

    The regex entries quote the WHOLE MATCH, not the captured value — a bare `Májová` occurs
    in several places on the page and `find_span` takes the first occurrence inside the node.
    The transformed attribute quotes the RAW attribute: quoting the normalised `Ostrov` let
    the span resolve into the node's own `value="Májová 843, Ostrov"` attribute instead of
    into `data-city`, which is a span pointing at a different fact."""
    claims = by_id(mined("3861311"))
    assert claims["cr.det.title_line"].evidence_quote == ", ulice Májová,"
    assert claims["cr.det.title_okres"].evidence_quote == (
        ", okres Karlovy Vary - ČESKÉREALITY.cz")
    assert claims["cr.det.data_city"].evidence_quote == "Ostrov (okres Karlovy Vary)"


# ------------------------------------------------------------------ the town-tier body

def test_a_town_tier_body_yields_the_okres_and_the_obec_but_no_street() -> None:
    """Listing 3680359. A title without `, ulice X,` is the portal DECLARING town
    granularity, not a parse failure — asserted rather than assumed, because the difference
    between "no street on this page" and "the street locator broke" is the whole reason this
    portal's `<title>` is read at all."""
    claims = by_id(mined("3680359"))
    assert set(claims) == {"cr.det.title_okres", "cr.det.data_city"}
    assert claims["cr.det.title_okres"].value_text == "Trutnov"
    assert claims["cr.det.data_city"].value_text == "Špindlerův Mlýn"
    assert claims["cr.det.data_city"].evidence_quote == "Špindlerův Mlýn (okres Trutnov)"


@pytest.mark.parametrize("native", sorted(BODIES))
def test_a_real_body_records_no_absence_at_all(native: str) -> None:
    """Absences here would mean the scoper failed closed or an id-matched reader missed its
    subject. This portal declares neither: no entry is subject-matched, so a zero-claim
    entry (the street on a town-tier page) is silence, not a recorded miss."""
    assert mined(native).absences == []


# ------------------------------------------------------------------ the v5 exclusion zone

def test_the_live_neighbour_carousel_is_out_of_reach_of_every_reader() -> None:
    """The D7 hole v5 closes. `.similar, .podobne` match ZERO nodes on both real bodies while
    the live "Podobné nemovitosti" block renders as `section.s-estates-slide` and carries up
    to 20 other listings' obec+street."""
    scoped = document("3861311")
    assert scoped.is_complete
    assert dict(scoped.zone_matches)["section.s-estates-slide"] == 1
    for neighbour in ("Štúrova", "Masarykova", "Podobné nemovitosti"):
        assert not scoped.contains(neighbour), neighbour
    assert scoped.contains("Májová") and scoped.contains("Ostrov")


def test_the_retired_carousel_selectors_matched_nothing_on_either_real_body() -> None:
    """Kept rather than deleted — they cost nothing and they record which markup the block
    used to have — but recorded here as measured-dead, so nobody reads the v1 zone list as
    evidence that this carousel was ever excluded on 2026 markup."""
    for native in BODIES:
        matches = dict(document(native).zone_matches)
        assert matches[".similar"] == 0 and matches[".podobne"] == 0
        assert matches["select[name*='region']"] == 0
        assert matches["nav a[href*='zahranicni']"] == 0


def test_without_the_v5_zone_a_neighbour_street_would_be_reachable() -> None:
    """The negative control. If ceskereality ever drops the carousel this test says the zone
    stopped being load-bearing, instead of the register silently guarding nothing."""
    assert document("3861311", with_v5_zone=False).contains("Štúrova")


# ------------------------------------------------------------------ refusals

def test_this_portal_licenses_no_archived_coordinate() -> None:
    """`cr.det.legacy_pin` already carries this portal's pin from `listings.geom` (admitted
    for `coords.source='page'`, read off this very `input#driving_calculator_from`), so
    re-mining the same position off the archived body would be a second fingerprint for one
    fact under a locator nobody licensed. ceskereality is deliberately absent from
    `ARCHIVED_COORDINATE_RULES`, and the ladder refuses BY PORTAL — before it ever looks at
    which entry asked."""
    verdict = coordinate_verdict(
        SOURCE, "page", in_mapy_inventory=False, substrate=SUBSTRATE_ARCHIVED_HTML,
        entry_id="cr.det.legacy_pin")
    assert not verdict.admitted
    assert verdict.reason == "no_archived_coordinate_locator_on_this_portal"


def test_the_pin_the_page_carries_is_never_claimed_off_the_archived_body() -> None:
    """The mechanical half of the rule above: both real bodies stamp `data-coord-lat` /
    `data-coord-lng` on the subject input, and neither yields a coordinate claim, because no
    v5 entry reads them."""
    for native in BODIES:
        assert b"data-coord-lat" in body(native)
        assert [c for c in mined(native).claims if c.claim_type == "coordinate"] == []


@pytest.mark.parametrize("native", sorted(BODIES))
def test_this_contract_bounds_a_real_body_without_a_hole(native: str) -> None:
    """`extract_payload` fails CLOSED — an incomplete scope admits NOTHING and records one
    `not_attempted` absence per entry — so every claim asserted above depends on this
    portal's five zones all being APPLICABLE, not merely well-spelled. A zone that compiled
    and matched nothing is not a hole (three of these match nothing on 2026 markup); a zone
    the scoper cannot honour is."""
    scoped = document(native)
    assert scoped.is_complete
    assert not scoped.unsupported_selectors and not scoped.strip_failures


def test_a_body_carrying_none_of_this_portals_locators_claims_nothing() -> None:
    """Silence, not invention. No v5 entry has a positional or best-guess fallback, so a body
    without the `<title>` shape and without the driving-calculator input yields zero claims —
    which is what makes a zero-claim sweep a signal W2-13's tripwire can read."""
    result = mined("3861311", source_body=b"<html><head></head><body></body></html>")
    assert result.claims == [] and result.absences == []


# ------------------------------------------------------------------ the pinned fixture

def test_the_pinned_fixture_still_yields_the_required_always_entry() -> None:
    """`required: always` on `cr.det.data_city` is DECLARATIVE — no lane implements a
    raise-on-miss for it — so this is the mechanical half of "raise, not shrug" until W2-13's
    per-source zero-claim tripwire lands. The pinned body is the one the golden gate scores.

    It also records an HONEST coverage gap: the pinned fixture's `<title>` predates v5 and
    carries no ` - ČESKÉREALITY.cz inzerce realit` suffix, so `cr.det.title_okres` produces
    nothing there and is gated by the real-body tests above instead of by the golden."""
    pinned = (_PINNED / f"{SOURCE}_detail.html").read_bytes()
    payload = ArchivedPayload(
        id=1, source=SOURCE, source_id_native="fixture", page_kind="detail",
        payload_sha256="0" * 64, first_observed_at=OBSERVED_AT, body=pinned)
    result = extract_payload(payload, fx.listing(SOURCE, {}, native="fixture"),
                             entries(), register=register())
    claims = by_id(result)
    assert claims["cr.det.data_city"].value_text == "České Budějovice"
    assert claims["cr.det.title_line"].value_text == "Nádražní"
    assert "cr.det.title_okres" not in claims
