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


@pytest.mark.parametrize("source,payload,why", CLASS_E_CASES)
def test_class_e_rows_never_produce_a_coordinate_claim(source, payload, why):
    row = listing(source, payload, lat=49.5, lon=15.5)
    result = extract_listing(row, entries_for(source))
    assert "coordinate" not in claims_by_type(result), why
    # The withholding is recorded rather than silent: a negative artefact with no value.
    assert any(a.field_ == "coordinate" and a.reason == "not_attempted"
               for a in result.absences), why


@pytest.mark.parametrize("source,payload,why", CLASS_E_CASES)
def test_class_e_rows_never_produce_an_ephemeral_claim(source, payload, why):
    row = listing(source, payload, lat=49.5, lon=15.5)
    result = extract_listing(row, entries_for(source))
    for claim in result.claims:
        assert claim.licence_class in EMITTABLE_LICENCE_CLASSES, (why, claim.extractor_id)


def test_no_payload_can_make_the_extractor_emit_ephemeral_display_only():
    """The adversarial case: a payload that ASKS for the forbidden class."""
    hostile = dict(IDNES_PAGE)
    hostile["coords"] = {"source": "page", "licence_class": "ephemeral_display_only",
                         "confidence": "ephemeral_display_only"}
    result = extract_listing(listing("idnes", hostile, lat=50.0, lon=14.0),
                             entries_for("idnes"))
    assert result.claims
    assert {c.licence_class for c in result.claims} == {"portal"}


def test_carry_forward_is_admitted_only_when_absent_from_the_inventory():
    present = listing("idnes", IDNES_CARRY_FORWARD, lat=50.0, lon=14.4,
                      in_mapy_inventory=True)
    absent = listing("idnes", IDNES_CARRY_FORWARD, lat=50.0, lon=14.4,
                     in_mapy_inventory=False)

    assert "coordinate" not in claims_by_type(extract_listing(present, entries_for("idnes")))
    admitted = claims_by_type(extract_listing(absent, entries_for("idnes")))["coordinate"]
    assert admitted[0].licence_class == "portal"
    assert admitted[0].value_jsonb["ladder"] == "carry_forward_absent_from_mapy_inventory"


# mmreality left this list at mmreality@2: `mm.det.point` moved onto the archived lane
# (`json_point`), so W1 emits no mmreality coordinate to veto. The veto itself did not
# narrow — it is a JOIN on listing_id and applies to the archived read too
# (`_licensed_coordinate` -> `coordinate_verdict(..., in_mapy_inventory=...)`); what
# changed is only which lane produces the portal's coordinate.
@pytest.mark.parametrize("source,payload", (
    ("sreality", SREALITY_POST_CUTOVER),
    ("bezrealitky", BEZREALITKY),
    ("bazos", BAZOS_LINK),
    ("idnes", IDNES_PAGE),
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
    assert coordinate_verdict("remax", "page", in_mapy_inventory=False).reason == (
        "no_first_party_coordinate_on_this_portal")
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
