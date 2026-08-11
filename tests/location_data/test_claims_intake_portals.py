"""Per-portal W1 extraction, against payload shapes taken from recon/db-raw-samples.md §3.

Every assertion here is a statement about what the CONTRACT plus the payload produce — the
extractor has no per-portal branches of its own beyond the readers the contract names.
"""

from __future__ import annotations

from dataclasses import replace

from location_data.claims_intake import extract_listing, sreality_payload_shape
from tests.location_data.claim_intake_fixtures import (
    BAZOS_LINK,
    BEZREALITKY,
    CESKEREALITY_NULL_LOCALITY,
    CESKEREALITY_PAGE,
    IDNES_PAGE,
    MAXIMA_PAGE,
    MMREALITY_ACCURATE,
    MMREALITY_NOT_ACCURATE,
    REALITYMIX_NULL_LOCALITY,
    REALITYMIX_PAGE,
    REMAX,
    REMAX_BOTH_ADDRESS_KEYS,
    REMAX_DISPLAY_ADDRESS,
    SREALITY_LEGACY,
    SREALITY_POST_CUTOVER,
    SREALITY_TRUNCATED,
    SREALITY_ZIP_SENTINEL,
    claim_by_extractor,
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
    # All five populated portal admin ids plus country_id, namespaced in the value.
    admin_ids = {c.value_text for c in by_type["portal_admin_id"]}
    assert "sreality.municipality_id=3468" in admin_ids
    assert "sreality.country_id=112" in admin_ids
    assert by_type["portal_street_id"][0].value_text == "sreality.street_id=122964"
    assert result.enrichment == []


def test_sreality_declared_precision_is_typed_on_the_blur_axis():
    row = listing("sreality", SREALITY_POST_CUTOVER, lat=50.078, lon=14.450)
    result = extract_listing(row, entries_for("sreality"))

    inaccuracy = claim_by_extractor(result, "sr.det.inaccuracy_type")
    entity = claim_by_extractor(result, "sr.det.entity_type")
    assert inaccuracy.declared_precision_label == "street"
    # `street` names a blurred class -> declared; `address` names a precise one -> none,
    # written EXPLICITLY either way (06 §6.6 rule 7).
    assert inaccuracy.blur_evidence == "declared"
    assert entity.declared_precision_label == "address"
    assert entity.blur_evidence == "none"
    assert inaccuracy.extraction_method == "portal_declared_quality"


def test_sreality_bounding_box_becomes_the_uncertainty_geometry():
    row = listing("sreality", SREALITY_POST_CUTOVER, lat=50.078, lon=14.450)
    result = extract_listing(row, entries_for("sreality"))
    shape = claim_by_extractor(result, "sr.det.geometry")
    assert shape.claim_type == "uncertainty_geometry"
    assert shape.value_shape_wkt.startswith("POLYGON((14.4485223 50.077147")
    assert shape.value_jsonb["geometry_type"] == "linestring"


def test_sreality_zip_and_street_id_sentinels_are_dropped():
    row = listing("sreality", SREALITY_ZIP_SENTINEL, lat=49.3955, lon=13.2951)
    result = extract_listing(row, entries_for("sreality"))
    by_type = claims_by_type(result)
    assert "psc" not in by_type          # 31,046 rows store the literal '-1'
    assert "portal_street_id" not in by_type


def test_sreality_premise_office_is_never_a_claim():
    """`premise.locality` is the AGENCY OFFICE, present as a decoy in 11 of 12 files."""
    row = listing("sreality", SREALITY_POST_CUTOVER, lat=50.078, lon=14.450)
    result = extract_listing(row, entries_for("sreality"))
    assert all("Vinohradská" != c.value_text for c in result.claims)
    assert all(c.value_geom_wkt != "POINT(14.4402 50.0781)" for c in result.claims)


def test_sreality_legacy_shape_yields_no_coordinate_and_routes_to_refetch():
    assert sreality_payload_shape(SREALITY_LEGACY) == "legacy"
    row = listing("sreality", SREALITY_LEGACY, lat=49.3955, lon=13.2951)
    result = extract_listing(row, entries_for("sreality"))

    assert "coordinate" not in claims_by_type(result)
    # Not a silent no-claim: the display string survives AND the row joins the refetch
    # cohort, because a legacy-shape row can never gain entity_type/zip/housenumber.
    assert claim_by_extractor(result, "sr.det.legacy_locality_value").value_text == (
        "Klatovy, okres Klatovy")
    assert [(t.lane, t.outcome) for t in result.enrichment] == [
        ("sreality_detail_refetch", "skipped")]


def test_sreality_truncated_payload_routes_to_refetch_with_an_absence():
    """The 80 KB-truncation cohort: the locality object is gone entirely."""
    assert sreality_payload_shape(SREALITY_TRUNCATED) == "absent"
    row = listing("sreality", SREALITY_TRUNCATED, lat=50.0, lon=14.0)
    result = extract_listing(row, entries_for("sreality"))

    assert result.claims == []
    task = result.enrichment[0]
    assert task.lane == "sreality_detail_refetch"
    assert task.outcome == "error"
    assert "truncation" in task.error
    assert any(a.field_ == "coordinate" and a.reason == "not_attempted"
               for a in result.absences)


# ------------------------------------------------------------------------ bezrealitky

def test_bezrealitky_gps_ruian_and_city_district():
    row = listing("bezrealitky", BEZREALITKY, lat=50.1092, lon=14.4749)
    result = extract_listing(row, entries_for("bezrealitky"))
    by_type = claims_by_type(result)

    assert by_type["coordinate"][0].value_geom_wkt == "POINT(14.4749 50.1092)"
    assert by_type["address_point_id"][0].value_text == "22698884"
    # cityDistrict survives today only concatenated inside `locality`; the two are never
    # composed ('Praha' + 'Praha - Libeň' -> 'Praha - Praha - Libeň' on 41.2% of rows).
    assert by_type["obec_name"][0].value_text == "Praha"
    assert by_type["cast_obce_name"][0].value_text == "Praha - Libeň"
    assert by_type["house_number_cp"][0].value_text == "655"
    assert by_type["house_number_co"][0].value_text == "31"
    assert by_type["psc"][0].value_text == "15400"   # stored '154 00' vs '19000'
    assert by_type["coordinate"][0].surface == "graphql"


def test_bezrealitky_empty_house_unit_is_not_a_claim():
    row = listing("bezrealitky", BEZREALITKY, lat=50.1, lon=14.4)
    result = extract_listing(row, entries_for("bezrealitky"))
    assert "house_unit" not in claims_by_type(result)


# -------------------------------------------------------------------------- mmreality

def test_mmreality_point_accurate_true_and_municipality_id():
    row = listing("mmreality", MMREALITY_ACCURATE, lat=50.0296, lon=15.7712)
    result = extract_listing(row, entries_for("mmreality"))
    by_type = claims_by_type(result)

    assert by_type["coordinate"][0].value_geom_wkt == "POINT(15.7712123456 50.0296123456)"
    accurate = claim_by_extractor(result, "mm.det.accurate")
    assert accurate.declared_precision_label == "accurate"
    assert accurate.blur_evidence == "none"
    # municipalityId is the RÚIAN obec CODE, not a portal id — typing it portal_admin_id
    # would make it non-queryable under 01 §12.
    assert by_type["obec_code"][0].value_text == "533165"
    assert by_type["obec_code"][0].value_num == 533165.0
    assert by_type["portal_admin_id"][0].value_text == "mmreality.districtId=3403"


def test_mmreality_accurate_false_declares_blur():
    row = listing("mmreality", MMREALITY_NOT_ACCURATE, lat=50.1414, lon=12.9061)
    result = extract_listing(row, entries_for("mmreality"))

    accurate = claim_by_extractor(result, "mm.det.accurate")
    assert accurate.declared_precision_label == "not_accurate"
    assert accurate.blur_evidence == "declared"
    # The coordinate is still stored — the cap is the resolver's business, not the
    # extractor's (02 §2.1.2 rule 2).
    assert claims_by_type(result)["coordinate"][0].value_geom_wkt.startswith("POINT(")
    assert "cast_obce_name" not in claims_by_type(result)   # municipalityPart is null


def test_which_declared_bool_label_is_blurred_comes_from_the_contract():
    """Which of the two labels means "blurred" is a PORTAL fact, so it is data on the
    entry (`precision_map.blurred_labels`) exactly as it is for `declared_quality` — a
    recalibration is a contract version bump, not a code change. Proved by inverting the
    entry: the same payload then declares blur on `accurate`, not on `not_accurate`."""
    entries = entries_for("mmreality")
    inverted = [
        replace(e, precision_map={**e.precision_map, "blurred_labels": ["accurate"]})
        if e.entry_id == "mm.det.accurate" else e
        for e in entries
    ]
    assert any(e.precision_map.get("blurred_labels") == ["not_accurate"]
               for e in entries), "the shipped contract blurs `not_accurate`"

    truthy = listing("mmreality", MMREALITY_ACCURATE, lat=50.0296, lon=15.7712)
    falsy = listing("mmreality", MMREALITY_NOT_ACCURATE, lat=50.1414, lon=12.9061)

    assert claim_by_extractor(
        extract_listing(truthy, inverted), "mm.det.accurate").blur_evidence == "declared"
    assert claim_by_extractor(
        extract_listing(falsy, inverted), "mm.det.accurate").blur_evidence == "none"


def test_mmreality_original_title_street_is_not_mined_in_w1():
    """`mm.det.original_title_street` is regex_text: evidence-bearing, so it waits for the
    content-addressed payload store (W2a) rather than writing an unverifiable span."""
    row = listing("mmreality", MMREALITY_ACCURATE, lat=50.0, lon=15.0)
    result = extract_listing(row, entries_for("mmreality"))
    assert all(c.extraction_method not in ("regex_text", "llm_text") for c in result.claims)
    assert all(c.value_text != "Kutnohorská" or c.extractor_id == "mm.det.street"
               for c in result.claims)


# ------------------------------------------------------------------------------ bazos

def test_bazos_link_coordinate_and_psc():
    row = listing("bazos", BAZOS_LINK, lat=48.8489, lon=17.1325)
    result = extract_listing(row, entries_for("bazos"))
    by_type = claims_by_type(result)

    assert by_type["coordinate"][0].value_geom_wkt == "POINT(17.1325 48.8489)"
    assert by_type["coordinate"][0].extraction_method == "legacy_column"
    assert by_type["coordinate"][0].legacy_source_column == "listings.geom"
    assert by_type["coordinate"][0].snapshot_anchor == "unanchored_legacy"
    # 100% of active bazos rows carry raw_json.psc and 0 of 29,546 have listings.zip.
    assert by_type["psc"][0].value_text == "69681"
    # The post-office town is NOT the obec (57.0% disagree), so it is typed postal_town.
    assert by_type["postal_town"][0].value_text == "Hodonín"
    assert "obec_name" not in by_type


def test_bazos_geocode_quality_stamp_rides_with_an_admitted_coordinate():
    row = listing("bazos", BAZOS_LINK, lat=48.8489, lon=17.1325)
    result = extract_listing(row, entries_for("bazos"))
    stamp = claim_by_extractor(result, "bzs.det.geocode_quality")
    assert stamp.claim_type == "precision_declaration"
    assert stamp.value_text == "link"
    assert stamp.value_jsonb["notes"] == ["no geocoder; used CZ-guarded maps link"]


# ------------------------------------------------------------ idnes / realitymix / …

def test_idnes_page_coordinate_is_admitted_as_a_legacy_column():
    row = listing("idnes", IDNES_PAGE, lat=50.4585, lon=13.4177)
    result = extract_listing(row, entries_for("idnes"))
    by_type = claims_by_type(result)
    assert by_type["coordinate"][0].value_geom_wkt == "POINT(13.4177 50.4585)"
    assert by_type["coordinate"][0].value_jsonb["coords_source"] == "page"
    assert by_type["address_line_verbatim"][0].value_text == (
        "Březno - Nechranice, okres Chomutov")


def test_realitymix_and_ceskereality_and_maxima_page_coordinates():
    for source, payload, lat, lon in (
        ("realitymix", REALITYMIX_PAGE, 50.3611, 13.6667),
        ("ceskereality", CESKEREALITY_PAGE, 50.0446, 14.3204),
        ("maxima", MAXIMA_PAGE, 50.7663, 15.0562),
    ):
        result = extract_listing(listing(source, payload, lat=lat, lon=lon),
                                 entries_for(source))
        by_type = claims_by_type(result)
        assert by_type["coordinate"][0].licence_class == "portal", source
        assert by_type["address_line_verbatim"], source
        assert result.absences == [], source


def test_remax_address_is_stored_only_as_a_conflict_signal():
    """The carousel address reached listings.street on 2 rows; 43.6% of street-bearing
    rows carry an address that does not contain their own locality."""
    row = listing("remax", REMAX, lat=50.0810, lon=14.4508)
    result = extract_listing(row, entries_for("remax"))
    by_type = claims_by_type(result)

    conflict = claim_by_extractor(result, "rx.det.raw_address_conflict")
    assert conflict.claim_type == "address_line_verbatim"
    assert conflict.subject_scoped is False          # inadmissible to survivorship
    assert conflict.legacy_source_column == "raw_json.address"
    assert "street_name" not in by_type              # never a fill
    assert "coordinate" not in by_type               # remax stamps no provenance at all


# -------------------------------------- the zero-claim cohorts measured on 2026-08-11
#
# W1's gate is ">=99% of active listings carry >=1 claim"; production sat at 97.66%, and
# three portals owned 8,720 of the ~9,000 missing rows. Each test below is one of those
# cohorts, keyed off the payload keyset actually sampled from it.

def test_remax_display_address_is_the_subject_claim_v1_could_not_read():
    """W0 0d renamed the subject's own `h2.pd-header__address` into
    `raw_json.display_address` and the carousel value into `carousel_address`. v1 read only
    `/address`, so every re-drained row lost its single claim — 1,763 active rows with
    none at all."""
    row = listing("remax", REMAX_DISPLAY_ADDRESS, lat=50.0810, lon=14.4508,
                  locality="Praha 3 - Žižkov")
    result = extract_listing(row, entries_for("remax"))

    claim = claim_by_extractor(result, "rx.det.legacy_display_address")
    assert claim.value_text == "ulice Roháčova, Praha 3 - Žižkov"
    assert claim.claim_type == "address_line_verbatim"
    assert claim.subject_scoped is True            # admissible to survivorship
    assert claim.legacy_source_column == "raw_json.display_address"
    assert claim.snapshot_anchor == "unanchored_legacy"
    # The carousel value is not read on this shape at all — no entry points at it.
    assert all("V Horní Stromce" not in (c.value_text or "") for c in result.claims)


def test_remax_reads_display_address_without_promoting_the_banned_address_key():
    """The mixed row: both keys present. `raw_json.address` stays what 02 §2.2.6 made it —
    a conflict signal, `subject_scoped=false`, inadmissible to survivorship — even with the
    subject's own line sitting beside it."""
    row = listing("remax", REMAX_BOTH_ADDRESS_KEYS, lat=50.0810, lon=14.4508)
    result = extract_listing(row, entries_for("remax"))

    banned = [c for c in result.claims
              if (c.value_text or "").startswith("V Horní Stromce")]
    assert [c.extractor_id for c in banned] == ["rx.det.raw_address_conflict"]
    assert banned[0].subject_scoped is False
    # Exactly one subject-scoped location claim, and it is the header line.
    subject = [c for c in result.claims if c.subject_scoped]
    assert [c.extractor_id for c in subject] == ["rx.det.legacy_display_address"]
    assert subject[0].value_text == "ulice Roháčova, Praha 3 - Žižkov"


def test_the_legacy_locality_column_reaches_a_null_payload_locality():
    """ceskereality/realitymix zero-claim rows have `locality_text` PRESENT and NULL — a
    keyset sample cannot tell that from a populated one. 06 §6.1.3 class B: the column is
    then the only surviving copy, migrated with the method, the cap and the write-path
    flag all stated."""
    cases = (
        ("ceskereality", CESKEREALITY_NULL_LOCALITY, "cr.det.legacy_locality",
         "České Budějovice 4, U Smaltovny"),
        ("realitymix", REALITYMIX_NULL_LOCALITY, "rm.det.legacy_locality",
         "Hranicka, Prerov"),
        ("remax", REMAX_DISPLAY_ADDRESS, "rx.det.legacy_locality", "Praha 3 - Žižkov"),
    )
    for source, payload, extractor_id, value in cases:
        result = extract_listing(listing(source, payload, locality=value),
                                 entries_for(source))
        claim = claim_by_extractor(result, extractor_id)
        assert claim is not None, source
        assert claim.value_text == value, source
        assert claim.claim_type == "address_line_verbatim", source
        assert claim.extraction_method == "legacy_column", source
        assert claim.surface == "legacy_column", source
        assert claim.legacy_source_column == "listings.locality", source
        assert claim.snapshot_anchor == "unanchored_legacy", source
        assert claim.licence_class == "portal", source
        assert claim.blur_evidence == "none", source
        # 06 §6.1.1 caps class B at `medium`, and §6.6 rule 3 makes an unnameable writer
        # say so — both are contract data, not constants in the reader.
        assert claim.claim_confidence == "medium", source
        assert claim.legacy_write_path_unknown is True, source
        # `payload_only`/`full` history is a per-source constant and is unaffected.
        assert claim.history_completeness == "locality_text_only", source


def test_a_null_legacy_column_invents_nothing():
    """The column is absent as often as it is present; a missing legacy value must produce
    no claim, not an empty one (and no absence either — W1 records only the two negatives
    of 06 §6.1.5)."""
    for source, payload in (("ceskereality", CESKEREALITY_NULL_LOCALITY),
                            ("realitymix", REALITYMIX_NULL_LOCALITY)):
        result = extract_listing(listing(source, payload, locality=None),
                                 entries_for(source))
        assert result.claims == [], source


def test_the_payload_claim_and_the_legacy_claim_coexist_when_both_have_a_value():
    """Not a fill: where the payload string survives, both claims are emitted (evidence
    that agrees), with distinct extractor ids so their provenance stays readable."""
    result = extract_listing(
        listing("ceskereality", CESKEREALITY_PAGE, lat=50.0446, lon=14.3204,
                locality="Praha Stodůlky"),
        entries_for("ceskereality"))
    lines = {c.extractor_id: c for c in claims_by_type(result)["address_line_verbatim"]}
    assert set(lines) == {"cr.det.locality_text", "cr.det.legacy_locality"}
    assert lines["cr.det.locality_text"].legacy_source_column == "raw_json.locality_text"
    assert lines["cr.det.locality_text"].claim_confidence is None
    assert lines["cr.det.legacy_locality"].legacy_source_column == "listings.locality"


def test_every_claim_writes_blur_evidence_and_history_completeness_explicitly():
    cases = (
        ("sreality", SREALITY_POST_CUTOVER, 50.0, 14.4),
        ("bezrealitky", BEZREALITKY, 50.1, 14.4),
        ("mmreality", MMREALITY_NOT_ACCURATE, 50.1, 12.9),
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
    # The three portals whose contract was bumped to close the 2026-08-11 coverage gap;
    # every other portal is still on its original version.
    expected_version = {"remax": 2, "ceskereality": 2, "realitymix": 2}
    for source, payload, lat, lon in cases:
        result = extract_listing(listing(source, payload, lat=lat, lon=lon),
                                 entries_for(source))
        assert result.claims, source
        for claim in result.claims:
            assert claim.blur_evidence in ("none", "declared"), (source, claim.extractor_id)
            # loc_claim_legacy: a legacy claim must name its column, and no other claim
            # may claim to be one.
            if claim.extraction_method == "legacy_column":
                assert claim.legacy_source_column, claim.extractor_id
            else:
                assert claim.legacy_source_column is None, claim.extractor_id
            assert claim.history_completeness == expected_history.get(
                source, "locality_text_only")
            assert claim.extractor_version == (
                f"contract:{source}@{expected_version.get(source, 1)}")
            assert claim.first_observed_at == result.claims[0].first_observed_at
