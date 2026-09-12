"""The licence gate — 06 §6.1.2 (the coordinate-provenance ladder) and §6.4's W1 gate.

These are the tests that must never be relaxed. The gate they encode is not a preference:
Mapy.com's terms prohibit "storing or caching … API function results", the current
`geocode_cache` + `listings.geom` writes do exactly that, and a migration is precisely the
event that would re-import it under a better-looking provenance (06 §6.1.1 class E).

The rule set, stated once:
  * NO fixture, however adversarial, may produce a claim with
    `licence_class = 'ephemeral_display_only'` — a non-storable signal produces no row at
    all (06 §6.6 rule 6), so the value can never reach an append-only table.
  * NO class-E row may produce a coordinate claim: `geocode`, bazos `street`/`locality`,
    and an absent stamp are all class E.
  * `carry_forward` is provenance-laundering — admitted only when the listing is ABSENT
    from the C7.2 R2 inventory (`mapy_affected`).
  * Presence in that inventory vetoes a coordinate on EVERY substrate, because §6.4's
    blocking gate is `claims JOIN <R2 inventory> WHERE claim_type='coordinate'` = 0.
"""

from __future__ import annotations

import pytest

from location_data.claims_intake import (
    COORDINATE_RULES,
    EMITTABLE_LICENCE_CLASSES,
    MAPY_COORDS_SOURCES,
    coordinate_verdict,
    extract_listing,
    payload_entries,
)
from location_data.contracts import CONTRACT_LICENCE_CLASSES, load_all
from tests.location_data.claim_intake_fixtures import (
    BAZOS_LINK,
    BAZOS_LOCALITY_GEOCODE,
    BAZOS_STREET_GEOCODE,
    BEZREALITKY,
    IDNES_CARRY_FORWARD,
    IDNES_PAGE,
    IDNES_UNSTAMPED,
    MMREALITY_ACCURATE,
    REALITYMIX_GEOCODE,
    REMAX,
    SREALITY_POST_CUTOVER,
    claims_by_type,
    entries_for,
    listing,
)

CLASS_E_CASES = (
    # (source, payload, why)
    ("bazos", BAZOS_STREET_GEOCODE, "bazos' own in-parser Mapy street geocode"),
    ("bazos", BAZOS_LOCALITY_GEOCODE, "bazos' coarse Mapy locality geocode"),
    ("realitymix", REALITYMIX_GEOCODE, "coords.source='geocode' — the Bílovec failure"),
    ("idnes", IDNES_UNSTAMPED, "no provenance stamp: unestablished"),
    ("remax", REMAX, "remax stamps no coords key at all"),
)


def _reads_a_payload_coordinate(source: str) -> bool:
    """Does this contract still lift a pin out of `raw_json` at all?

    W1-c left exactly three that do — sreality, bezrealitky and mmreality. On the other six
    the pin moved onto the stored page body, so `extract_listing` has no payload coordinate
    entry and therefore nothing to WITHHOLD; the ladder that refuses a class-E pin is the
    same function either way (`_licensed_coordinate` -> `coordinate_verdict`), and it is
    pinned below."""
    return any(e.claim_type == "coordinate" for e in payload_entries(entries_for(source)))


@pytest.mark.parametrize("source,payload,why", CLASS_E_CASES)
def test_class_e_rows_never_produce_a_coordinate_claim(source, payload, why):
    row = listing(source, payload, lat=49.5, lon=15.5)
    result = extract_listing(row, entries_for(source))
    assert "coordinate" not in claims_by_type(result), why
    # The withholding is counted rather than silent: one reason, one tally, one log line —
    # on the portals that have a payload coordinate to withhold.
    if _reads_a_payload_coordinate(source):
        assert any(r.startswith("coordinate_withheld:") for r in result.refusals), why


@pytest.mark.parametrize("source,payload,why", CLASS_E_CASES)
def test_class_e_rows_never_produce_an_ephemeral_claim(source, payload, why):
    row = listing(source, payload, lat=49.5, lon=15.5)
    result = extract_listing(row, entries_for(source))
    for claim in result.claims:
        assert claim.licence_class in EMITTABLE_LICENCE_CLASSES, (why, claim.extractor_id)


def test_no_payload_can_make_the_extractor_emit_ephemeral_display_only():
    """The adversarial case: a payload that ASKS for the forbidden class.

    On sreality rather than idnes since W1-c: the class a claim carries is the CONTRACT's
    (`entry.default_licence_class`), never the payload's, so this has to run on a portal the
    payload lane still extracts anything from at all — idnes@3 reads only the page body and
    would pass vacuously."""
    hostile = dict(SREALITY_POST_CUTOVER)
    hostile["coords"] = {"source": "page", "licence_class": "ephemeral_display_only",
                         "confidence": "ephemeral_display_only"}
    result = extract_listing(listing("sreality", hostile, lat=50.0, lon=14.0),
                             entries_for("sreality"))
    assert result.claims
    assert {c.licence_class for c in result.claims} == {"portal"}


def test_carry_forward_is_admitted_only_when_absent_from_the_inventory():
    """Provenance laundering, pinned on the LADDER rather than on one portal's extraction.

    Every `carry_forward_admissible` portal reads its pin off the page since W1-c, so there
    is no payload arm left to drive this through `extract_listing` — and the archived arm
    calls exactly this function (`_licensed_coordinate`). Asserting it here covers all six
    instead of whichever one still happened to have a raw_json entry."""
    admissible = [s for s, r in COORDINATE_RULES.items() if r.carry_forward_admissible]
    assert admissible, "carry_forward stopped being admissible anywhere — rail is dead"
    for source in admissible:
        absent = coordinate_verdict(source, "carry_forward", in_mapy_inventory=False)
        assert absent.admitted is True, source
        assert absent.licence_class == "portal", source
        assert absent.reason == "carry_forward_absent_from_mapy_inventory", source
        present = coordinate_verdict(source, "carry_forward", in_mapy_inventory=True)
        assert present.admitted is False, source
        assert present.licence_class is None, source
    # The payload fixture that used to drive this still yields no idnes coordinate at all.
    row = listing("idnes", IDNES_CARRY_FORWARD, lat=50.0, lon=14.4)
    assert "coordinate" not in claims_by_type(extract_listing(row, entries_for("idnes")))


# The three portals whose pin is still in `raw_json`. bazos and idnes left this list at
# W1-c, not because the veto narrowed — it is a JOIN on listing_id and applies to the
# archived read too (`_licensed_coordinate` -> `coordinate_verdict(..., in_mapy_inventory=)`)
# — but because their pin moved onto the page, so the payload lane has nothing to veto.
@pytest.mark.parametrize("source,payload", (
    ("sreality", SREALITY_POST_CUTOVER),
    ("bezrealitky", BEZREALITKY),
    ("mmreality", MMREALITY_ACCURATE),
))
def test_inventory_membership_vetoes_a_coordinate_on_every_substrate(source, payload):
    """§6.4's W1 gate is a JOIN on listing_id, not on the coordinate's substrate: a listing
    can enter the inventory through arm 2 (a geocode was attempted) or arm 3 (its geom
    matches a cached Mapy coordinate) while its payload coordinate looks first-party."""
    clean = listing(source, payload, lat=50.0, lon=14.4)
    flagged = listing(source, payload, lat=50.0, lon=14.4, in_mapy_inventory=True)

    assert "coordinate" in claims_by_type(extract_listing(clean, entries_for(source)))
    assert "coordinate" not in claims_by_type(extract_listing(flagged, entries_for(source)))


def test_verdict_reasons_are_stable_and_never_leak_a_licence_class():
    for stamp in sorted(MAPY_COORDS_SOURCES):
        verdict = coordinate_verdict("bazos", stamp, in_mapy_inventory=False)
        assert verdict.admitted is False
        assert verdict.licence_class is None
    # remax joined the geom_column portals on 2026-09-11: the subject-map pin is stamped
    # `page` by the parser; a row drained before the stamp existed carries None and stays
    # refused until its next drain — never admitted on faith.
    assert coordinate_verdict("remax", "page", in_mapy_inventory=False).admitted is True
    assert coordinate_verdict("remax", None, in_mapy_inventory=False).reason == (
        "coordinate_provenance_unestablished")
    assert coordinate_verdict("remax", "geocode", in_mapy_inventory=False).admitted is False
    assert coordinate_verdict("bazos", "link", in_mapy_inventory=False).admitted is True
    assert coordinate_verdict("idnes", "link", in_mapy_inventory=False).admitted is False


def test_every_portal_has_an_explicit_coordinate_rule():
    contracts = {c.source for c in load_all()}
    assert contracts == set(COORDINATE_RULES)


def test_no_contract_may_declare_the_forbidden_licence_class():
    """02 §2.1.9: `ephemeral_display_only` is reserved for live third-party geocoder calls
    and is NEVER emitted by a contract."""
    assert "ephemeral_display_only" not in CONTRACT_LICENCE_CLASSES
    for contract in load_all():
        for entry in contract.entries:
            assert entry.default_licence_class != "ephemeral_display_only"


def test_bazos_link_is_the_only_first_party_stamp_on_that_portal():
    assert COORDINATE_RULES["bazos"].first_party_sources == frozenset({"link"})
    for stamp in ("street", "locality", "geocode"):
        assert coordinate_verdict("bazos", stamp, in_mapy_inventory=False).admitted is False
