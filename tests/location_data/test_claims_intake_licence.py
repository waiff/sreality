"""The licence gate — 06 §6.1.2, the coordinate-provenance ladder.

These are the tests that must never be relaxed. The gate they encode is not a preference:
Mapy.com's terms prohibit "storing or caching … API function results", and a migration is
precisely the event that would re-import such a value under a better-looking provenance
(06 §6.1.1 class E).

The rule set after W4-b deleted the geocoder, stated once:
  * A coordinate claim comes ONLY from the portal's own pin — the payload path its rule
    names, or the one archived locator `ARCHIVED_COORDINATE_RULES` names. There is no
    third source, so there is nothing left to veto row-by-row.
  * Every other stamp is class E and produces NO coordinate claim: `geocode`, bazos'
    `street`/`locality`, the `carry_forward` a refetch laundered a Mapy geocode through,
    and an absent stamp.
  * NO fixture, however adversarial, may produce a claim with
    `licence_class = 'ephemeral_display_only'` — a non-storable signal produces no row at
    all (06 §6.6 rule 6), so the value can never reach an append-only table.
"""

from __future__ import annotations

import pytest

from location_data.claims_intake import (
    COORDINATE_RULES,
    EMITTABLE_LICENCE_CLASSES,
    coordinate_verdict,
    extract_listing,
    payload_entries,
)
from location_data.contracts import CONTRACT_LICENCE_CLASSES, load_all
from tests.location_data.claim_intake_fixtures import (
    BAZOS_LOCALITY_GEOCODE,
    BAZOS_STREET_GEOCODE,
    BEZREALITKY,
    IDNES_CARRY_FORWARD,
    IDNES_UNSTAMPED,
    MMREALITY_ACCURATE,
    REALITYMIX_GEOCODE,
    REMAX,
    SREALITY_POST_CUTOVER,
    claims_by_type,
    entries_for,
    listing,
)

# Every stamp a deleted producer used to mint. None of them may ever license a value
# again, and `carry_forward` is on this list BECAUSE the geocoder is gone: it only ever
# meant "we re-used the coordinate already stored", which on these portals was a Mapy
# geocode wearing a refetch as a disguise.
DEAD_STAMPS = ("geocode", "street", "locality", "carry_forward")

CLASS_E_CASES = (
    # (source, payload, why)
    ("bazos", BAZOS_STREET_GEOCODE, "bazos' own in-parser Mapy street geocode"),
    ("bazos", BAZOS_LOCALITY_GEOCODE, "bazos' coarse Mapy locality geocode"),
    ("idnes", IDNES_CARRY_FORWARD, "carry_forward: a geocode laundered through a refetch"),
    ("realitymix", REALITYMIX_GEOCODE, "coords.source='geocode' — the Bílovec failure"),
    ("idnes", IDNES_UNSTAMPED, "no provenance stamp: unestablished"),
    ("remax", REMAX, "remax stamps no coords key at all"),
)


def test_only_the_three_payload_portals_lift_a_pin_out_of_raw_json():
    """W1-c left exactly three contracts naming a `raw_json` pointer, and each one's rule
    is `payload` — the value is re-derived from the pointer, so no provenance stamp is
    consulted. On the other six the pin lives on the stored page body and the stamp is the
    whole licence, which is what `STAMPED_SOURCES` below covers."""
    reads = {c.source for c in load_all()
             if any(e.claim_type == "coordinate" for e in payload_entries(entries_for(c.source)))}
    assert reads == {"sreality", "bezrealitky", "mmreality"}
    assert all(COORDINATE_RULES[s].substrate == "payload" for s in reads)


@pytest.mark.parametrize("source,payload,why", CLASS_E_CASES)
def test_class_e_rows_never_produce_a_coordinate_claim(source, payload, why):
    row = listing(source, payload, lat=49.5, lon=15.5)
    assert "coordinate" not in claims_by_type(extract_listing(row, entries_for(source))), why


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


# The six portals whose stored coordinate is licensed by its PROVENANCE STAMP. The other
# three name a `raw_json` pointer in their contract, so the value is re-derived from the
# payload and the stamp is never consulted (`rule.substrate == "payload"`).
STAMPED_SOURCES = sorted(
    s for s, r in COORDINATE_RULES.items() if r.substrate == "geom_column")


@pytest.mark.parametrize("source", STAMPED_SOURCES)
@pytest.mark.parametrize("stamp", DEAD_STAMPS)
def test_no_portal_admits_a_stamp_the_deleted_geocoder_minted(source, stamp):
    """The W4-b rail, pinned on the LADDER rather than on one portal's extraction.

    `carry_forward` had a conditional rung here until the geocoder went: it was admitted
    whenever the listing was absent from a Mapy inventory we no longer build. Nothing can
    re-open it without failing this, on all six stamped portals at once."""
    verdict = coordinate_verdict(source, stamp)
    assert verdict.admitted is False, (source, stamp)
    assert verdict.licence_class is None, (source, stamp)


def test_the_stamped_portals_are_the_six_the_geocoder_used_to_write():
    """If a portal ever leaves this list the rail above silently stops covering it."""
    assert STAMPED_SOURCES == [
        "bazos", "ceskereality", "idnes", "maxima", "realitymix", "remax"]


@pytest.mark.parametrize("source,payload", (
    ("sreality", SREALITY_POST_CUTOVER),
    ("bezrealitky", BEZREALITKY),
    ("mmreality", MMREALITY_ACCURATE),
))
def test_the_three_payload_portals_still_publish_their_own_pin(source, payload):
    """The other half of the rail: deleting the veto must not have deleted the claim.

    These three name a `raw_json` pointer in their contract, so a first-party pin still
    becomes a coordinate claim — the ladder refuses class E, not everything."""
    row = listing(source, payload, lat=50.0, lon=14.4)
    assert "coordinate" in claims_by_type(extract_listing(row, entries_for(source)))


def test_verdict_reasons_are_stable_and_never_leak_a_licence_class():
    # remax joined the geom_column portals on 2026-09-11: the subject-map pin is stamped
    # `page` by the parser; a row drained before the stamp existed carries None and stays
    # refused until its next drain — never admitted on faith.
    assert coordinate_verdict("remax", "page").admitted is True
    assert coordinate_verdict("remax", None).reason == "coordinate_provenance_unestablished"
    assert coordinate_verdict("bazos", "link").admitted is True
    assert coordinate_verdict("idnes", "link").admitted is False
    assert coordinate_verdict("bazos", "geocode").reason == (
        "unrecognised_coordinate_provenance")


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
