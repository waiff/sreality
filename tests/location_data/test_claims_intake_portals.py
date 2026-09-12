"""Per-portal W1 extraction, against payload shapes taken from recon/db-raw-samples.md §3.

Every assertion here is a statement about what the CONTRACT plus the payload produce — the
extractor has no per-portal branches of its own beyond the readers the contract names.
"""

from __future__ import annotations

from location_data.claims_intake import (
    extract_listing,
    sreality_payload_shape,
)
from tests.location_data.claim_intake_fixtures import (
    BAZOS_LINK,
    BEZREALITKY,
    CESKEREALITY_PAGE,
    IDNES_PAGE,
    MAXIMA_PAGE,
    REALITYMIX_PAGE,
    REMAX,
    SREALITY_LEGACY,
    SREALITY_POST_CUTOVER,
    SREALITY_TRUNCATED,
    SREALITY_ZIP_SENTINEL,
    claims_by_type,
    entries_for,
    listing,
)


# --------------------------------------------------------------------------- sreality

def test_sreality_post_cutover_yields_the_whole_locality_record():
    row = listing("sreality", SREALITY_POST_CUTOVER, lat=50.0784977, lon=14.4501973)
    result = extract_listing(row, entries_for("sreality"))
    by_type = claims_by_type(result)

    assert by_type["coordinate"][0].value_geom_wkt == "POINT(14.4501973 50.0784977)"
    assert by_type["street_name"][0].value_text == "náměstí Jiřího z Poděbrad"
    assert by_type["house_number_cp"][0].value_text == "1558"
    # čp and čo are a PAIR — the orientation number is dropped on 4 of 5 rows today.
    assert by_type["house_number_co"][0].value_text == "7"
    assert by_type["psc"][0].value_text == "13000"
    assert by_type["obec_name"][0].value_text == "Praha"
    assert by_type["cast_obce_name"][0].value_text == "Vinohrady"
    assert by_type["okres_name"][0].value_text == "Praha 3"
    assert by_type["kraj_name"][0].value_text == "Hlavní město Praha"
    assert not result.refusals


def test_sreality_zip_sentinel_is_dropped():
    row = listing("sreality", SREALITY_ZIP_SENTINEL, lat=49.3955, lon=13.2951)
    result = extract_listing(row, entries_for("sreality"))
    assert "psc" not in claims_by_type(result)   # 31,046 rows store the literal '-1'


def test_sreality_premise_office_is_never_a_claim():
    """`premise.locality` is the AGENCY OFFICE, present as a decoy in 11 of 12 files."""
    row = listing("sreality", SREALITY_POST_CUTOVER, lat=50.078, lon=14.450)
    result = extract_listing(row, entries_for("sreality"))
    assert all("Vinohradská" != c.value_text for c in result.claims)
    assert all(c.value_geom_wkt != "POINT(14.4402 50.0781)" for c in result.claims)


def test_sreality_legacy_shape_yields_no_coordinate_and_is_counted():
    assert sreality_payload_shape(SREALITY_LEGACY) == "legacy"
    row = listing("sreality", SREALITY_LEGACY, lat=49.3955, lon=13.2951)
    result = extract_listing(row, entries_for("sreality"))

    assert "coordinate" not in claims_by_type(result)
    # Not a silent no-claim: the shape is COUNTED, because a legacy-shape row can never
    # gain entity_type/zip/housenumber and the fix is a detail refetch, not a contract
    # entry (W1-c R11).
    assert result.refusals["sreality_payload_shape:legacy"] == 1


def test_sreality_truncated_payload_is_counted_under_its_own_reason():
    """The 80 KB-truncation cohort: the locality object is gone entirely."""
    assert sreality_payload_shape(SREALITY_TRUNCATED) == "absent"
    row = listing("sreality", SREALITY_TRUNCATED, lat=50.0, lon=14.0)
    result = extract_listing(row, entries_for("sreality"))

    assert result.claims == []
    assert dict(result.refusals) == {"sreality_payload_shape:absent": 1}


# ------------------------------------------------------------------------ bezrealitky

def test_bezrealitky_gps_and_city_district():
    row = listing("bezrealitky", BEZREALITKY, lat=50.1092, lon=14.4749)
    result = extract_listing(row, entries_for("bezrealitky"))
    by_type = claims_by_type(result)

    assert by_type["coordinate"][0].value_geom_wkt == "POINT(14.4749 50.1092)"
    # cityDistrict survives today only concatenated inside `locality`; the two are never
    # composed ('Praha' + 'Praha - Libeň' -> 'Praha - Praha - Libeň' on 41.2% of rows).
    assert by_type["obec_name"][0].value_text == "Praha"
    assert by_type["cast_obce_name"][0].value_text == "Praha - Libeň"
    assert by_type["house_number_cp"][0].value_text == "655"
    assert by_type["house_number_co"][0].value_text == "31"
    assert by_type["psc"][0].value_text == "15400"   # stored '154 00' vs '19000'
    assert by_type["coordinate"][0].surface == "graphql"


def test_every_claim_writes_blur_evidence_and_history_completeness_explicitly():
    """The fleet mechanic, and the one thing this file still asserts for all nine: whatever
    a portal's contract reads, every claim it produces writes the two axes 06 §6.6 rules 6
    and 7 forbid this lane to default, and stamps the contract version that produced it.

    NO pinned version table any more. A version is the record of one portal's extraction
    changes, and pinning nine of them here made every contract bump a diff in a file about
    lane mechanics — the versions are asserted per portal, in the per-portal contract
    tests (W1-c R14). A portal whose contract reads only the stored page body yields no
    PAYLOAD claim at all, which is why the loop asserts over what it gets rather than
    demanding a claim from each one."""
    cases = (
        ("sreality", SREALITY_POST_CUTOVER, 50.0, 14.4),
        ("bezrealitky", BEZREALITKY, 50.1, 14.4),
        ("bazos", BAZOS_LINK, 48.8, 17.1),
        ("idnes", IDNES_PAGE, 50.4, 13.4),
        ("remax", REMAX, 50.0, 14.4),
        ("ceskereality", CESKEREALITY_PAGE, 50.0, 14.3),
        ("realitymix", REALITYMIX_PAGE, 50.3, 13.6),
        ("maxima", MAXIMA_PAGE, 50.7, 15.0),
    )
    expected_history = {
        "sreality": "full", "bezrealitky": "payload_only", "mmreality": "payload_only",
    }
    seen = 0
    for source, payload, lat, lon in cases:
        entries = entries_for(source)
        version = {e.contract_version for e in entries}
        result = extract_listing(listing(source, payload, lat=lat, lon=lon), entries)
        for claim in result.claims:
            seen += 1
            assert claim.blur_evidence in ("none", "declared"), (source, claim.extractor_id)
            assert claim.history_completeness == expected_history.get(
                source, "locality_text_only")
            assert claim.extractor_version == f"contract:{source}@{version.pop()}"
            assert claim.first_observed_at == result.claims[0].first_observed_at
    assert seen, "no portal yielded a payload claim — the loop proves nothing"
